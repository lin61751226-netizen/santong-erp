"""Classify mixed-site LINE photos using explicit, same-sender dated captions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.core.local_time import as_local, local_today
from app.models import AdminAuditLog, GroupTextLog, PhotoUploadLog, Worksite
from app.services.google_drive import GoogleDriveWorklogError, google_drive_worklog_service as drive

GROUP_NAME = "47&76工地照片"
SITE_NAMES = {"47": "善捷47", "76": "金駿76"}
BEFORE_WINDOW = timedelta(hours=2)
AFTER_WINDOW = timedelta(minutes=10)
# app.py runs one worker. Serialize classification and retry reconciliation.
classification_lock = asyncio.Lock()


def is_mixed_photo_group(name: str | None) -> bool:
    return "".join(unicodedata.normalize("NFKC", name or "").split()) in {GROUP_NAME, "47&76工地相片"}


def parse_caption(content: str) -> tuple[str, date] | None:
    text = unicodedata.normalize("NFKC", content).strip()
    match = re.fullmatch(r"(47|76)-(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if not match:
        return None
    try:
        day = date(*map(int, match.groups()[1:]))
    except ValueError:
        return None
    return (match[1], day) if day <= local_today() else None


def choose_caption(labels: list[GroupTextLog], *, user_id: str, moment: datetime,
                   allow_following: bool = False) -> GroupTextLog | None:
    # Casual conversation never resets a caption; malformed caption-like text does.
    candidates = [x for x in labels if x.line_user_id == user_id
                  and re.match(r"^(47|76)-", unicodedata.normalize("NFKC", x.content).strip())]
    before = [x for x in candidates if moment - BEFORE_WINDOW <= x.sent_at <= moment]
    if before:
        nearest = max(x.sent_at for x in before)
        choices = [x for x in before if x.sent_at == nearest]
    elif allow_following:
        after = [x for x in candidates if moment < x.sent_at <= moment + AFTER_WINDOW]
        if not after:
            return None
        nearest = min(x.sent_at for x in after)
        choices = [x for x in after if x.sent_at == nearest]
    else:
        return None
    parsed = {parse_caption(x.content) for x in choices}
    return choices[0] if len(parsed) == 1 and None not in parsed else None


def group_labels(session: Session, group_id: str) -> list[GroupTextLog]:
    return list(session.exec(select(GroupTextLog).where(GroupTextLog.source_id == group_id,
                                                      GroupTextLog.source_type == "group")).all())


def photo_moment(photo: PhotoUploadLog) -> datetime | None:
    if photo.source_event_at:
        return photo.source_event_at
    # Old LINE filenames retain the message timestamp in Taiwan time. Never use export dates.
    match = re.search(r"_(\d{8}_\d{6})\.[A-Za-z0-9]+$", photo.file_name)
    if not match or not photo.source_message_id or photo.import_key:
        return None
    try:
        stamp = datetime.strptime(match[1], "%Y%m%d_%H%M%S").replace(tzinfo=ZoneInfo("Asia/Taipei"))
    except ValueError:
        return None
    moment = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return moment if abs(photo.uploaded_at - moment) <= timedelta(hours=1) else None


def classification_plan(session: Session, *, group_id: str, date_from: date, date_to: date,
                        allow_legacy: bool = False) -> dict:
    labels = group_labels(session, group_id)
    sites = {x.name: x for x in session.exec(select(Worksite).where(Worksite.is_active.is_(True))).all()}
    rows = []
    for photo in session.exec(select(PhotoUploadLog).where(PhotoUploadLog.site_id.is_(None)).order_by(PhotoUploadLog.id)).all():
        if not date_from <= as_local(photo.uploaded_at).date() <= date_to:
            continue
        legacy = not photo.source_group_id
        if photo.source_group_id != group_id and not (legacy and allow_legacy):
            continue
        moment = photo_moment(photo)
        if moment is None:
            continue
        label = choose_caption(labels, user_id=photo.line_user_id or "", moment=moment, allow_following=True)
        if not label:
            continue
        code, day = parse_caption(label.content)
        site = sites.get(SITE_NAMES[code])
        if not site:
            continue
        rows.append({"id": photo.id, "file_name": photo.file_name, "file_id": photo.drive_file_id,
                     "parent": photo.drive_folder_id, "uploaded_at": photo.uploaded_at.isoformat(),
                     "old_work_date": str(photo.work_date or ""), "work_date": day.isoformat(),
                     "site_id": site.id, "site_name": site.name, "text_id": label.id,
                     "caption": label.content, "moment": moment.isoformat(), "legacy": legacy})
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    counts = Counter((r["site_name"], r["work_date"]) for r in rows)
    return {"digest": digest, "group_id": group_id, "rows": rows, "count": len(rows),
            "legacy_count": sum(r["legacy"] for r in rows),
            "summary": [{"site_name": s, "work_date": d, "count": n} for (s, d), n in sorted(counts.items())]}


async def apply_classification(session: Session, plan: dict, *, actor=None) -> dict:
    results = []
    for row in plan["rows"]:
        photo = session.get(PhotoUploadLog, row["id"])
        if not photo or photo.site_id is not None or photo.drive_folder_id != row["parent"]:
            results.append({"id": row["id"], "status": "skipped", "detail": "紀錄已異動，未覆寫"})
            continue
        try:
            parent = await drive.move_classified_photo(
                file_id=photo.drive_file_id, expected_parent=photo.drive_folder_id,
                original_day=as_local(datetime.fromisoformat(row["moment"])).date(), site_name=row["site_name"],
                work_date=date.fromisoformat(row["work_date"]))
            before = {"site_id": photo.site_id, "work_date": str(photo.work_date or ""),
                      "drive_folder_id": photo.drive_folder_id, "note": photo.note}
            photo.site_id = row["site_id"]
            photo.work_date = date.fromisoformat(row["work_date"])
            photo.source_event_at = datetime.fromisoformat(row["moment"])
            photo.source_group_id = plan["group_id"]
            photo.source_group = GROUP_NAME
            photo.classification_text_id = row["text_id"]
            photo.drive_folder_id = parent
            photo.note = (photo.note or "") + f"；文字分類#{row['text_id']}：{row['caption']}"
            session.add(photo)
            session.add(AdminAuditLog(
                actor_id=actor.id if actor else None, actor_code=actor.employee_code if actor else "LINE",
                actor_name=actor.name if actor else "LINE文字分類", action="photo_text_classify",
                entity_type="photo_upload", entity_id=photo.id,
                summary=json.dumps({"before": before, "after": {"site_id": photo.site_id,
                    "work_date": str(photo.work_date), "drive_folder_id": parent},
                    "text_id": row["text_id"], "caption": row["caption"], "legacy_match": row["legacy"]}, ensure_ascii=False)))
            session.commit()
            results.append({"id": row["id"], "status": "saved"})
        except Exception as exc:
            session.rollback()
            detail = str(exc) if isinstance(exc, GoogleDriveWorklogError) else "紀錄保存未完成，請重新預覽接續；不需重傳"
            results.append({"id": row["id"], "status": "failed", "detail": detail})
    return {"saved": sum(x["status"] == "saved" for x in results), "results": results}


async def reconcile_pending_classification(session: Session) -> dict:
    """Recover delayed/out-of-order webhooks and failed moves; never infer legacy group IDs."""
    async with classification_lock:
        pending = session.exec(select(PhotoUploadLog).where(
            PhotoUploadLog.site_id.is_(None), PhotoUploadLog.source_group_id.is_not(None))).all()
        groups = {p.source_group_id for p in pending if is_mixed_photo_group(p.source_group)}
        plans = []
        remaining = 20
        for group_id in sorted(groups):
            first_day = min(as_local(p.uploaded_at).date() for p in pending if p.source_group_id == group_id)
            plan = classification_plan(session, group_id=group_id, date_from=first_day, date_to=local_today())
            plan["rows"] = plan["rows"][:remaining]
            if plan["rows"]:
                plans.append(plan)
                remaining -= len(plan["rows"])
            if not remaining:
                break
        if not plans:
            return {"saved": 0, "backup_status": "not_needed"}
        if (await drive.backup_database()).get("status") != "saved":
            return {"saved": 0, "backup_status": "failed"}
        results = [await apply_classification(session, plan) for plan in plans]
        backup = await drive.backup_database()
        return {"saved": sum(r["saved"] for r in results), "backup_status": backup.get("status", "failed")}
