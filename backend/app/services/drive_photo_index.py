"""Index existing Drive images without uploading, moving, or renaming them."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from sqlmodel import Session, select

from app.core.local_time import as_local
from app.models import AdminAuditLog, Employee, PhotoUploadLog, Worksite
from app.services.photo_import import PhotoImportError


def folder_id_from_url(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        return value
    parsed = urlparse(value)
    match = re.search(r"/folders/([A-Za-z0-9_-]{10,200})(?:/|$)", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "drive.google.com" or not match:
        raise PhotoImportError("請貼上完整 Google Drive 資料夾連結")
    return match.group(1)


def filename_work_date(name: str, code: str, today: date) -> date:
    match = re.match(r"^LINE_ALBUM_([^_]+)-(\d{8})_", name)
    if not match or match[1] != code:
        raise PhotoImportError("檔名工地不符，或沒有完整八位施工日期")
    try:
        day = datetime.strptime(match[2], "%Y%m%d").date()
    except ValueError as exc:
        raise PhotoImportError("檔名施工日期無效") from exc
    if day > today:
        raise PhotoImportError("檔名施工日期晚於今天")
    return day


def plan_drive_index(session: Session, *, tree: dict, site: Worksite, filename_code: str, today: date) -> dict:
    existing = session.exec(select(PhotoUploadLog)).all()
    by_id = {row.drive_file_id: row for row in existing}
    keys = {row.import_key for row in existing if row.import_key}
    seen_ids, seen_keys = set(), set()
    rows = []
    files = sorted(tree["files"], key=lambda item: (item["id"], item["name"]))
    digest_data = {"folder_id": tree["folder_id"], "site_id": site.id, "filename_code": filename_code, "files": files}
    digest = hashlib.sha256(json.dumps(digest_data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    for item in files:
        row = {**item, "status": "skipped", "reason": "", "work_date": None}
        rows.append(row)
        if item["id"] in seen_ids:
            row.update(status="duplicate", reason="同一 Drive 檔案重複出現")
            continue
        seen_ids.add(item["id"])
        if item.get("mimeType") not in {"image/jpeg", "image/png", "image/webp"}:
            row["reason"] = "不是支援的 JPG／PNG／WebP 照片"
            continue
        try:
            day = filename_work_date(item["name"], filename_code, today)
        except PhotoImportError as exc:
            row["reason"] = str(exc)
            continue
        row["work_date"] = day.isoformat()
        previous = by_id.get(item["id"])
        if previous:
            original_day = previous.work_date or as_local(previous.uploaded_at).date()
            if previous.site_id != site.id or original_day != day:
                row.update(status="conflict", reason="此 Drive 檔案已登錄於其他工地或日期，不覆寫")
            else:
                row.update(status="duplicate", reason="此 Drive 檔案已有紀錄")
            continue
        checksum = item.get("md5Checksum") or ""
        basis = f"{site.id}:{day}:{checksum.lower()}" if re.fullmatch(r"[a-fA-F0-9]{32}", checksum) else item["id"]
        prefix = "drive-md5:" if re.fullmatch(r"[a-fA-F0-9]{32}", checksum) else "drive-id:"
        key = prefix + hashlib.sha256(basis.encode()).hexdigest()
        row["import_key"] = key
        if key in keys or key in seen_keys:
            row.update(status="duplicate", reason="同工地同日相同內容已存在（Drive MD5／ID）")
        else:
            row["status"] = "new"
        seen_keys.add(key)
    counts = Counter(row["status"] for row in rows)
    date_counts = Counter(row["work_date"] for row in rows if row["status"] == "new")
    return {"digest": digest, "folder_id": tree["folder_id"], "folder_name": tree["folder_name"],
            "folder_count": tree["folder_count"], "site_id": site.id, "site_name": site.name,
            "counts": {key: counts[key] for key in ("new", "duplicate", "skipped", "conflict")},
            "dates": [{"work_date": day, "count": count} for day, count in sorted(date_counts.items())], "rows": rows}


def public_plan(plan: dict) -> dict:
    return {**{key: value for key, value in plan.items() if key != "rows"},
            "exceptions": [{key: row.get(key) for key in ("name", "album_name", "status", "reason")}
                           for row in plan["rows"] if row["status"] in {"skipped", "conflict"}][:50],
            "exception_count": sum(row["status"] in {"skipped", "conflict"} for row in plan["rows"])}


def apply_drive_index(session: Session, *, plan: dict, actor: Employee, source_group: str) -> int:
    added = 0
    try:
        for row in plan["rows"]:
            if row["status"] != "new":
                continue
            uploaded_at = datetime.utcnow()
            if row.get("createdTime"):
                uploaded_at = datetime.fromisoformat(row["createdTime"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            session.add(PhotoUploadLog(
                employee_id=actor.id, site_id=plan["site_id"], work_date=date.fromisoformat(row["work_date"]),
                file_name=row["name"], original_file_name=row["name"], drive_file_id=row["id"],
                drive_folder_id=row["parent_id"], drive_url=f"https://drive.google.com/file/d/{row['id']}/view",
                uploaded_at=uploaded_at, source_group=source_group or None,
                album_name=row["album_name"], import_key=row["import_key"],
                note="既有 Drive 相簿照片建檔；施工日依檔名，原檔未搬移／重傳；員工欄為匯入人，非原拍攝者",
            ))
            added += 1
        if added:
            session.add(AdminAuditLog(
                actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
                action="drive_photo_index", entity_type="photo_upload", entity_id=None,
                summary=f"既有 Drive 照片建檔：{plan['site_name']}｜{added}張｜{plan['folder_id']}｜預覽摘要 {plan['digest']}",
            ))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return added
