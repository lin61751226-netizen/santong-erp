"""工作日誌草稿、簽單預填與計價核對。

模型只負責文字草稿與差異說明。工時、台數與金額一律由既有規則計算。
草稿不會核准日誌、不會改已存簽單，也不會寫入計價檔。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    AdminAuditLog,
    AiBillingCheck,
    AiJournalDraft,
    AiSignSlipDraft,
    AssignmentMember,
    AttendanceEvent,
    Employee,
    Forklift,
    ForkliftInspection,
    GroupTextLog,
    ManagedDocument,
    PhotoUploadLog,
    SignSlipRecord,
    WorkAssignment,
    WorkHourImportLog,
    WorkReportEvent,
    Worksite,
    WorksiteJournalHours,
)
from app.services.cost_workbook import (
    CostWorkbookError,
    count_forklift_units,
    day_cost,
    is_canonical_holiday,
    match_label_for_site,
    read_month_cost_data,
    read_pricing_parameters,
    roc_period,
    vehicle_quantity,
)
from app.services.google_drive import google_drive_worklog_service

logger = logging.getLogger(__name__)

SIGN_SLIP_DRIVERS = ("林育弘", "林建成", "朱勝忠", "林金生", "林金谷")
CUSTOMER_RULES = (("齊裕", "齊裕營造"),)
TRUCK_PATTERN = r"貨車"
HOUR_LIMIT = 10000.0
BLOCKING_CODES = {"missing_driver", "unknown_customer", "missing_amount"}
STATUS_LABELS = {
    "draft": "草稿",
    "approved": "已核准",
    "rejected": "已退回",
    "confirmed": "已確認",
    "superseded": "已被新草稿取代",
}


class AiOpsUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OpsGuard(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class JournalNarrative(BaseModel):
    work_summary: str = ""
    work_items: list[str] = Field(default_factory=list)
    quantities: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    cited_group_text_ids: list[int] = Field(default_factory=list)
    cited_photo_ids: list[int] = Field(default_factory=list)
    cited_attendance_ids: list[int] = Field(default_factory=list)
    cited_assignment_ids: list[int] = Field(default_factory=list)
    cited_report_ids: list[int] = Field(default_factory=list)
    cited_inspection_ids: list[int] = Field(default_factory=list)


class BillingExplanation(BaseModel):
    index: int
    explanation: str = ""
    suggested_fix: str = ""


class BillingExplanations(BaseModel):
    items: list[BillingExplanation] = Field(default_factory=list)


def local_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    local_tz = ZoneInfo(settings.timezone)
    start = datetime.combine(target_date, time.min, tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = (
        datetime.combine(target_date, time.min, tzinfo=local_tz) + timedelta(days=1)
    ).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def month_bounds(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month or ""):
        raise OpsGuard(400, "月份請用 YYYY-MM")
    year, mon = (int(part) for part in month.split("-"))
    first = date(year, mon, 1)
    last = date(year + 1, 1, 1) - timedelta(days=1) if mon == 12 else date(year, mon + 1, 1) - timedelta(days=1)
    return first, last


def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def explanation_grounded(text: str, allowed_blob: str) -> bool:
    return numbers_in(text) <= numbers_in(allowed_blob)


def _clean(value: Any, limit: int) -> str:
    text = "".join(ch for ch in str(value or "") if ch == "\n" or ord(ch) >= 32)
    return text.strip()[:limit]


def _local_hhmm(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")


def _minutes(value: str | None) -> int | None:
    if not value or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _is_work_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    commands = {
        "開始綁定", "我的行程", "我的打卡", "我的請假", "出勤打卡",
        "上班打卡", "下班打卡", "到達工地", "離開工地", "外出", "返回",
        "加班開始", "加班結束", "已收到", "已到場", "工作開始", "工作完成",
        "點檢", "堆高機點檢", "開始點檢", "取消點檢", "正常", "異常",
    }
    if text in commands or re.match(r"^(綁定|請假|點檢工地|點檢堆高機)[:：\s]", text):
        return False
    return re.match(r"^(好|好的|收到|了解|ok|okay|辛苦了|謝謝|早安|午安|晚安)[!！。.]?$", text, re.I) is None


def _enum_text(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _audit(session: Session, actor: Employee | None, action: str, entity_type: str, entity_id: int | None, summary: str) -> None:
    session.add(AdminAuditLog(
        actor_id=actor.id if actor else None,
        actor_code=actor.employee_code if actor else "SYSTEM",
        actor_name=actor.name if actor else "每日草稿排程",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary[:1000],
    ))
    session.commit()


def _worksite(session: Session, worksite_id: int) -> Worksite:
    site = session.get(Worksite, worksite_id)
    if site is None or not site.is_active:
        raise OpsGuard(404, "找不到啟用中的工地")
    return site


def _latest(session: Session, model, work_date: date, worksite_id: int):
    return session.exec(
        select(model).where(model.work_date == work_date, model.worksite_id == worksite_id).order_by(model.id.desc())
    ).first()


def _customer_from_rule(site_name: str) -> str | None:
    for needle, customer in CUSTOMER_RULES:
        if needle in (site_name or ""):
            return customer
    return None


def _historical_customer(session: Session, worksite_id: int) -> str | None:
    names = {
        (row.customer_name or "").strip()
        for row in session.exec(
            select(SignSlipRecord).where(
                SignSlipRecord.worksite_id == worksite_id,
                SignSlipRecord.is_active.is_(True),
            )
        ).all()
        if (row.customer_name or "").strip()
    }
    if len(names) == 1:
        return next(iter(names))
    return None


def gather_site_day(session: Session, work_date: date, worksite_id: int) -> dict[str, Any]:
    site = _worksite(session, worksite_id)
    start_at, end_at = local_day_bounds(work_date)
    employees = {row.id: row for row in session.exec(select(Employee)).all()}
    forklifts = {row.id: row for row in session.exec(select(Forklift)).all()}

    def person(employee_id: int | None) -> str:
        employee = employees.get(employee_id)
        return employee.name if employee else ""

    assignments = []
    for assignment in session.exec(
        select(WorkAssignment).where(WorkAssignment.status != "cancelled", WorkAssignment.work_date == work_date, WorkAssignment.site_id == worksite_id)
        .order_by(WorkAssignment.id)
    ).all():
        members = session.exec(
            select(AssignmentMember).where(
                AssignmentMember.assignment_id == assignment.id,
                AssignmentMember.is_active.is_(True),
            )
        ).all()
        assignments.append({
            "id": assignment.id,
            "work_item": _clean(assignment.work_item, 200),
            "notes": _clean(assignment.notes, 300),
            "vehicle": _clean(assignment.vehicle, 80),
            "equipment": _clean(assignment.equipment, 80),
            "start_time": assignment.start_time.strftime("%H:%M") if assignment.start_time else None,
            "end_time": assignment.end_time.strftime("%H:%M") if assignment.end_time else None,
            "members": [person(row.employee_id) for row in members if person(row.employee_id)],
            "status": _enum_text(assignment.status),
        })

    group_texts = []
    for log in session.exec(
        select(GroupTextLog).where(
            GroupTextLog.site_id == worksite_id,
            GroupTextLog.sent_at >= start_at,
            GroupTextLog.sent_at < end_at,
        ).order_by(GroupTextLog.sent_at, GroupTextLog.id)
    ).all():
        group_texts.append({
            "id": log.id,
            "employee_name": person(log.employee_id) or "未綁定",
            "content": _clean(log.content, 240),
            "sent_at": _local_hhmm(log.sent_at),
            "is_included": bool(log.is_included),
            "is_work_text": _is_work_text(log.content or ""),
        })

    attendance = []
    for event in session.exec(
        select(AttendanceEvent).where(
            AttendanceEvent.site_id == worksite_id,
            AttendanceEvent.happened_at >= start_at,
            AttendanceEvent.happened_at < end_at,
        ).order_by(AttendanceEvent.happened_at, AttendanceEvent.id)
    ).all():
        attendance.append({
            "id": event.id,
            "employee_name": person(event.employee_id) or "未知員工",
            "event_type": event.event_type,
            "happened_at": event.happened_at,
            "local_time": _local_hhmm(event.happened_at),
            "note": _clean(event.note, 120),
        })

    photos = []
    for photo in session.exec(
        select(PhotoUploadLog).where(
            PhotoUploadLog.site_id == worksite_id,
            PhotoUploadLog.uploaded_at >= start_at,
            PhotoUploadLog.uploaded_at < end_at,
        ).order_by(PhotoUploadLog.uploaded_at, PhotoUploadLog.id)
    ).all():
        photos.append({
            "id": photo.id,
            "employee_name": person(photo.employee_id) or "未綁定",
            "file_name": _clean(photo.file_name, 120),
            "note": _clean(photo.note, 160),
            "drive_url": photo.drive_url,
            "uploaded_at": _local_hhmm(photo.uploaded_at),
        })

    reports = []
    for report in session.exec(
        select(WorkReportEvent).where(
            WorkReportEvent.site_id == worksite_id,
            WorkReportEvent.reported_at >= start_at,
            WorkReportEvent.reported_at < end_at,
        ).order_by(WorkReportEvent.reported_at, WorkReportEvent.id)
    ).all():
        reports.append({
            "id": report.id,
            "employee_name": person(report.employee_id) or "未知員工",
            "event_type": report.event_type,
            "note": _clean(report.note, 240),
            "reported_at": _local_hhmm(report.reported_at),
        })

    inspections = []
    for inspection in session.exec(
        select(ForkliftInspection).where(
            ForkliftInspection.site_id == worksite_id,
            ForkliftInspection.inspection_date == work_date,
        ).order_by(ForkliftInspection.id)
    ).all():
        forklift = forklifts.get(inspection.forklift_id)
        inspections.append({
            "id": inspection.id,
            "forklift_code": forklift.forklift_code if forklift else "未知堆高機",
            "forklift_model": (forklift.model or "") if forklift else "",
            "operator_name": person(inspection.operator_id) or "未知操作員",
            "all_passed": bool(inspection.all_passed),
            "notes": _clean(inspection.notes, 200),
        })

    saved = session.exec(
        select(WorksiteJournalHours).where(
            WorksiteJournalHours.work_date == work_date,
            WorksiteJournalHours.worksite_id == worksite_id,
        )
    ).first()
    return {
        "work_date": work_date.isoformat(),
        "worksite_id": worksite_id,
        "site_code": site.code,
        "site_name": site.name,
        "assignments": assignments,
        "group_texts": group_texts[:40],
        "group_text_total": len(group_texts),
        "attendance": attendance,
        "photos": photos[:15],
        "photo_total": len(photos),
        "reports": reports[:20],
        "inspections": inspections,
        "saved_hours": None if saved is None else {
            "normal_hours": float(saved.normal_hours),
            "overtime_hours": float(saved.overtime_hours),
            "support_hours": float(saved.support_hours),
        },
    }


def _people(context: dict) -> list[str]:
    names: list[str] = []
    for assignment in context["assignments"]:
        names.extend(assignment["members"])
    for row in context["attendance"] + context["reports"] + context["inspections"]:
        name = row.get("employee_name") or row.get("operator_name")
        if name and name not in {"未綁定", "未知員工", "未知操作員"}:
            names.append(name)
    result: list[str] = []
    for name in names:
        if name and name not in result:
            result.append(name)
    return result


def _equipment(context: dict) -> list[str]:
    values: list[str] = []
    for assignment in context["assignments"]:
        values.extend([assignment.get("vehicle") or "", assignment.get("equipment") or ""])
    for inspection in context["inspections"]:
        label = " ".join(part for part in (inspection.get("forklift_code"), inspection.get("forklift_model")) if part)
        values.append(label)
    result: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in result:
            result.append(text)
    return result


def _vehicle_counts(context: dict) -> dict[str, int]:
    assigned = [
        text for assignment in context["assignments"]
        for text in (assignment.get("vehicle"), assignment.get("equipment")) if text
    ]
    inspected = [row.get("forklift_model") or "" for row in context["inspections"]]
    counts = count_forklift_units(assigned, inspected)
    assigned_truck = sum(vehicle_quantity(text, TRUCK_PATTERN) for text in assigned)
    inspected_truck = sum(vehicle_quantity(text, TRUCK_PATTERN) for text in inspected)
    counts["truck"] = max(assigned_truck, inspected_truck)
    return counts


def _span_hours(events: list[dict], start_type: str, end_type: str) -> float | None:
    starts = [row["happened_at"] for row in events if row["event_type"] == start_type and row.get("happened_at")]
    ends = [row["happened_at"] for row in events if row["event_type"] == end_type and row.get("happened_at")]
    if not starts or not ends:
        return None
    start, end = min(starts), max(ends)
    if end <= start:
        return None
    return round((end - start).total_seconds() / 3600, 2)


def _window(events: list[dict], start_type: str, end_type: str) -> tuple[str | None, str | None]:
    starts = [row for row in events if row["event_type"] == start_type and row.get("happened_at")]
    ends = [row for row in events if row["event_type"] == end_type and row.get("happened_at")]
    start = min(starts, key=lambda row: row["happened_at"])["local_time"] if starts else None
    end = max(ends, key=lambda row: row["happened_at"])["local_time"] if ends else None
    return start, end


def compute_hours(context: dict) -> dict[str, Any]:
    counts = _vehicle_counts(context)
    auto_normal = float(counts["total"] * 8)
    overtime_from_clock = _span_hours(context["attendance"], "加班開始", "加班結束")
    saved = context.get("saved_hours")
    flags: list[str] = []
    if saved is not None:
        normal, overtime, support = saved["normal_hours"], saved["overtime_hours"], saved["support_hours"]
        basis = (
            f"採用已儲存的計價工時：正常 {normal:g}、加班 {overtime:g}、支援 {support:g}。"
            f"依堆高機 {counts['total']} 台 × 8 小時，自動估算正常工時為 {auto_normal:g}。"
        )
        if abs(normal - auto_normal) > 0.01:
            flags.append("hours_differ_from_auto")
    else:
        normal = auto_normal
        overtime = float(overtime_from_clock or 0)
        support = 0.0
        basis = f"正常工時依堆高機 {counts['total']} 台 × 8 小時＝{normal:g}，尚未寫入計價工時。"
        if overtime_from_clock is not None:
            basis += f"加班依打卡「加班開始／加班結束」計算 {overtime:g} 小時。"
            flags.append("overtime_from_clock")
        else:
            basis += "沒有完整的加班打卡，加班記 0，不估算。"
        basis += "支援工時沒有已儲存值，記 0。"
    check_in, check_out = _window(context["attendance"], "上班打卡", "下班打卡")
    return {
        "normal_hours": normal,
        "overtime_hours": overtime,
        "support_hours": support,
        "forklift_count": counts["total"],
        "vehicles": {
            "twoPointFive": counts.get("twoPointFive", 0),
            "threePointZero": counts.get("threePointZero", 0),
            "fourPointFive": counts.get("fourPointFive", 0),
            "truck": counts.get("truck", 0),
        },
        "hours_basis": basis,
        "flags": flags,
        "auto_normal": auto_normal,
        "check_in": check_in,
        "check_out": check_out,
        "assignment_start": _earliest(context["assignments"], "start_time"),
        "assignment_end": _latest_time(context["assignments"], "end_time"),
        "attendance_incomplete": any(row["event_type"] == "上班打卡" for row in context["attendance"]) and check_out is None,
        "has_check_in": any(row["event_type"] == "上班打卡" for row in context["attendance"]),
    }


def _earliest(rows: list[dict], key: str) -> str | None:
    values = [row[key] for row in rows if row.get(key)]
    return min(values) if values else None


def _latest_time(rows: list[dict], key: str) -> str | None:
    values = [row[key] for row in rows if row.get(key)]
    return max(values) if values else None


def _work_lines(context: dict) -> list[str]:
    lines: list[str] = []
    for assignment in context["assignments"]:
        text = assignment["work_item"]
        if assignment.get("notes"):
            text = f"{text}（{assignment['notes']}）"
        if text and text not in lines:
            lines.append(text)
    for row in context["group_texts"]:
        if row["is_included"] and row["is_work_text"] and row["content"] not in lines:
            lines.append(row["content"])
    for row in context["reports"]:
        if row["note"] and row["note"] not in lines:
            lines.append(row["note"])
    return lines


def _fallback_issues(context: dict) -> list[str]:
    issues = []
    for row in context["inspections"]:
        if not row["all_passed"]:
            issues.append(f"{row['forklift_code']} 點檢異常" + (f"：{row['notes']}" if row["notes"] else ""))
    return issues


def _context_blob(context: dict) -> str:
    visible = {
        key: context[key]
        for key in ("work_date", "site_name", "site_code", "assignments", "group_texts", "reports", "inspections", "saved_hours")
    }
    visible["attendance"] = [
        {key: value for key, value in row.items() if key != "happened_at"} for row in context["attendance"]
    ]
    visible["photos"] = [
        {key: value for key, value in row.items() if key != "drive_url"} for row in context["photos"]
    ]
    visible["people"] = _people(context)
    visible["equipment"] = _equipment(context)
    return json.dumps(visible, ensure_ascii=False, default=str)


def _known_ids(context: dict) -> dict[str, set[int]]:
    return {
        "group_text": {row["id"] for row in context["group_texts"]},
        "photo": {row["id"] for row in context["photos"]},
        "attendance": {row["id"] for row in context["attendance"]},
        "assignment": {row["id"] for row in context["assignments"]},
        "report": {row["id"] for row in context["reports"]},
        "inspection": {row["id"] for row in context["inspections"]},
    }


def _snippets(context: dict, cited: dict[str, list[int]]) -> dict[str, list[dict]]:
    catalogs = {
        "group_text": context["group_texts"],
        "photo": context["photos"],
        "attendance": context["attendance"],
        "assignment": context["assignments"],
        "report": context["reports"],
        "inspection": context["inspections"],
    }
    result: dict[str, list[dict]] = {}
    for key, rows in catalogs.items():
        wanted = set(cited.get(key) or [])
        picked = []
        for row in rows:
            if row["id"] not in wanted:
                continue
            item = {name: value for name, value in row.items() if name != "happened_at"}
            picked.append(item)
        result[key] = picked
    return result


def _grounded_lines(lines: list[str], blob: str) -> tuple[list[str], bool]:
    kept, dropped = [], False
    for line in lines:
        text = _clean(line, 300)
        if not text:
            continue
        if explanation_grounded(text, blob):
            kept.append(text)
        else:
            dropped = True
    return kept[:20], dropped


async def complete_journal_narrative(context: dict) -> JournalNarrative:
    if not settings.ai_ops_enabled:
        raise AiOpsUnavailable("disabled")
    key = settings.openai_api_key.strip()
    if not key:
        raise AiOpsUnavailable("missing_key")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise AiOpsUnavailable("openai_missing") from exc
    system = (
        "你是三通工程行的工地日誌起草助手。只根據提供的 JSON 寫繁體中文草稿。"
        "不要新增名單裡沒有的人、機具、數量、工時或金額。工時由別的程式計算，摘要不要寫總工時。"
        "看不清楚就寫資料不足。引用的 id 必須來自 JSON。"
        "照片只能依檔名與備註描述，不要假裝看過照片內容。"
    )
    client = AsyncOpenAI(api_key=key, timeout=25, max_retries=0)
    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _context_blob(context)},
            ],
            response_format=JournalNarrative,
            store=False,
            max_completion_tokens=900,
        )
    except Exception as exc:
        logger.warning("工作日誌草稿模型失敗：%s", type(exc).__name__)
        raise AiOpsUnavailable(type(exc).__name__) from exc
    message = completion.choices[0].message
    if getattr(message, "refusal", None) or getattr(message, "parsed", None) is None:
        raise AiOpsUnavailable("refusal")
    return message.parsed


def _compose_content(context: dict, narrative: JournalNarrative | None, ai_status: str) -> tuple[dict, dict, list[str]]:
    blob = _context_blob(context)
    known = _known_ids(context)
    review_flags: list[str] = []
    lines = _work_lines(context)
    issues = _fallback_issues(context)
    summary = (
        f"{context['site_name']} {context['work_date']}："
        + ("、".join(lines) if lines else "當日沒有可整理的工作內容。")
        + "此為系統依派工、群組文字與回報整理的草稿。"
    )
    cited = {key: sorted(values) for key, values in known.items()}
    citation_source = "all_context"
    if narrative is not None:
        summary = _clean(narrative.work_summary, 1200) or summary
        model_lines, dropped_lines = _grounded_lines(narrative.work_items, blob)
        model_quantities, dropped_quantities = _grounded_lines(narrative.quantities, blob)
        model_issues, dropped_issues = _grounded_lines(narrative.issues, blob)
        if model_lines:
            lines = model_lines
        if model_issues:
            issues = model_issues
        quantities = model_quantities
        if dropped_lines or dropped_quantities or dropped_issues:
            review_flags.append("模型寫了來源裡沒有的數字，該句已捨棄。")
        if not explanation_grounded(summary, blob):
            review_flags.append("摘要出現來源沒有的數字，請核對後再核准。")
        raw_cited = {
            "group_text": narrative.cited_group_text_ids,
            "photo": narrative.cited_photo_ids,
            "attendance": narrative.cited_attendance_ids,
            "assignment": narrative.cited_assignment_ids,
            "report": narrative.cited_report_ids,
            "inspection": narrative.cited_inspection_ids,
        }
        filtered = {key: sorted({item for item in values if item in known[key]}) for key, values in raw_cited.items()}
        if any(len(values) != len(set(raw_cited[key])) for key, values in filtered.items()):
            review_flags.append("模型引用了不存在的來源，已移除。")
        if any(filtered.values()):
            cited = filtered
            citation_source = "model"
    else:
        quantities = []
    if context["group_text_total"] > len(context["group_texts"]) or context["photo_total"] > len(context["photos"]):
        review_flags.append("當天來源較多，草稿只讀取前段文字與照片清單。")
    content = {
        "work_summary": summary,
        "workers": _people(context),
        "equipment": _equipment(context),
        "work_items": lines,
        "quantities": quantities,
        "issues": issues,
        "review_flags": review_flags,
        "ai_status": ai_status,
        "edited_by_admin": False,
    }
    source_refs = {
        "cited": cited,
        "context": {key: sorted(values) for key, values in known.items()},
        "snippets": _snippets(context, cited),
        "citation_source": citation_source,
    }
    return content, source_refs, review_flags


def serialize_journal(row: AiJournalDraft) -> dict:
    return {
        "id": row.id,
        "work_date": row.work_date.isoformat(),
        "worksite_id": row.worksite_id,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "content": row.content or {},
        "source_refs": row.source_refs or {},
        "normal_hours": row.normal_hours,
        "overtime_hours": row.overtime_hours,
        "support_hours": row.support_hours,
        "forklift_count": row.forklift_count,
        "hours_basis": row.hours_basis,
        "ai_status": row.ai_status,
        "model_name": row.model_name,
        "is_draft": row.status == "draft",
        "applies_to_pricing": False,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


async def create_journal_draft(
    session: Session,
    actor: Employee | None,
    work_date: date,
    worksite_id: int,
    *,
    open_new: bool = False,
) -> AiJournalDraft:
    context = gather_site_day(session, work_date, worksite_id)
    approved = session.exec(
        select(AiJournalDraft).where(
            AiJournalDraft.work_date == work_date,
            AiJournalDraft.worksite_id == worksite_id,
            AiJournalDraft.status == "approved",
        )
    ).first()
    if approved is not None and not open_new:
        raise OpsGuard(409, "已有核准的工作日誌草稿，不會覆蓋。若要另開一份新草稿，請明確選擇另開。")
    for row in session.exec(
        select(AiJournalDraft).where(
            AiJournalDraft.work_date == work_date,
            AiJournalDraft.worksite_id == worksite_id,
            AiJournalDraft.status == "draft",
        )
    ).all():
        row.status = "superseded"
        row.updated_at = datetime.utcnow()
        session.add(row)

    narrative = None
    ai_status = "disabled"
    model_name = None
    if settings.ai_ops_enabled:
        try:
            narrative = await complete_journal_narrative(context)
            ai_status = "drafted"
            model_name = settings.openai_model
        except AiOpsUnavailable as exc:
            ai_status = "disabled" if exc.reason == "disabled" else "failed"
    hours = compute_hours(context)
    content, source_refs, _flags = _compose_content(context, narrative, ai_status)
    if ai_status == "failed":
        content["review_flags"] = ["AI 暫時無法整理，已改用現有資料。"] + list(content.get("review_flags") or [])
    draft = AiJournalDraft(
        work_date=work_date,
        worksite_id=worksite_id,
        status="draft",
        content=content,
        source_refs=source_refs,
        normal_hours=hours["normal_hours"],
        overtime_hours=hours["overtime_hours"],
        support_hours=hours["support_hours"],
        forklift_count=hours["forklift_count"],
        hours_basis=hours["hours_basis"],
        ai_status=ai_status,
        model_name=model_name,
        created_by_id=actor.id if actor else None,
        updated_by_id=actor.id if actor else None,
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    _audit(
        session, actor, "create", "ai_journal_draft", draft.id,
        f"{work_date.isoformat()} {context['site_name']} 工作日誌草稿（{ai_status}），未寫入計價工時",
    )
    return draft


def update_journal_draft(session: Session, actor: Employee, draft_id: int, payload: dict) -> AiJournalDraft:
    draft = session.get(AiJournalDraft, draft_id)
    if draft is None:
        raise OpsGuard(404, "找不到工作日誌草稿")
    if draft.status != "draft":
        raise OpsGuard(409, "只有草稿可以修改；已核准的紀錄不會改動。")
    content = dict(draft.content or {})
    for key in ("work_summary", "workers", "equipment", "work_items", "quantities", "issues"):
        if key in payload:
            value = payload[key]
            content[key] = _clean(value, 1200) if isinstance(value, str) else [_clean(item, 300) for item in value][:30]
    content["edited_by_admin"] = True
    draft.content = content
    draft.normal_hours = _hour(payload.get("normal_hours", draft.normal_hours))
    draft.overtime_hours = _hour(payload.get("overtime_hours", draft.overtime_hours))
    draft.support_hours = _hour(payload.get("support_hours", draft.support_hours))
    draft.hours_basis = (draft.hours_basis or "") + " 管理員已修改草稿工時，尚未寫入計價工時。"
    draft.updated_by_id = actor.id
    draft.updated_at = datetime.utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    _audit(session, actor, "update", "ai_journal_draft", draft.id, f"修改工作日誌草稿 {draft.work_date.isoformat()}，未改原始紀錄")
    return draft


def set_journal_status(session: Session, actor: Employee, draft_id: int, status: str) -> AiJournalDraft:
    draft = session.get(AiJournalDraft, draft_id)
    if draft is None:
        raise OpsGuard(404, "找不到工作日誌草稿")
    if draft.status != "draft":
        raise OpsGuard(409, "這份草稿已結束，不會再改狀態。")
    if status not in {"approved", "rejected"}:
        raise OpsGuard(400, "無法變更成這個狀態")
    draft.status = status
    draft.reviewed_by_id = actor.id
    draft.reviewed_at = datetime.utcnow()
    draft.updated_at = datetime.utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    action = "核准" if status == "approved" else "退回"
    _audit(
        session, actor, status, "ai_journal_draft", draft.id,
        f"{action}工作日誌草稿 {draft.work_date.isoformat()}，未改派工、打卡、簽單或計價工時",
    )
    return draft


def _hour(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OpsGuard(400, "工時必須是數字") from exc
    if number < 0 or number > HOUR_LIMIT:
        raise OpsGuard(400, "工時超出可接受範圍")
    return number


def _time_label(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if _minutes(text) is None:
        raise OpsGuard(400, "時間請用 24 小時制 HH:MM")
    return text


def assess_sign_slip(draft_fields: dict, facts: dict) -> list[dict]:
    items: list[dict] = []
    if not (draft_fields.get("driver_names") or "").strip():
        items.append({
            "code": "missing_driver",
            "level": "block",
            "message": f"找不到名單內的司機（{'、'.join(SIGN_SLIP_DRIVERS)}），沒有猜測。請管理員填寫。",
        })
    if not (draft_fields.get("customer_name") or "").strip():
        items.append({
            "code": "unknown_customer",
            "level": "block",
            "message": "無法從工地規則或唯一的歷史客戶判定客戶，沒有猜測。請管理員填寫。",
        })
    if draft_fields.get("amount") is None:
        items.append({
            "code": "missing_amount",
            "level": "block",
            "message": "金額沒有套用到計價單價，已留空。請選擇計價表或手動填寫，系統不會自行估價。",
        })
    start, end = draft_fields.get("start_time"), draft_fields.get("end_time")
    clock_in, clock_out = facts.get("check_in"), facts.get("check_out")
    if start and end and clock_in and clock_out:
        if abs((_minutes(start) or 0) - (_minutes(clock_in) or 0)) > 30 or abs((_minutes(end) or 0) - (_minutes(clock_out) or 0)) > 30:
            items.append({
                "code": "hours_inconsistent_with_clock",
                "level": "warn",
                "message": (
                    f"簽單時間 {start}–{end}，打卡約 {clock_in}–{clock_out}，差距超過 30 分鐘。"
                    "工時仍用規則或已儲存值，沒有改成打卡時數。"
                ),
            })
    if facts.get("attendance_incomplete"):
        items.append({
            "code": "attendance_incomplete",
            "level": "warn",
            "message": "有上班打卡但沒有下班打卡。工時沒有用打卡長度取代。",
        })
    if facts.get("journal_not_approved"):
        items.append({
            "code": "journal_not_approved",
            "level": "warn",
            "message": "工作日誌草稿尚未核准。簽單內容供核對，確認前不會建立正式簽單。",
        })
    if "hours_differ_from_auto" in (facts.get("hour_flags") or []):
        items.append({
            "code": "hours_differ_from_auto",
            "level": "warn",
            "message": "已儲存工時和台數 × 8 小時不同。預填採用已核准草稿或已儲存工時，沒有另估。",
        })
    if facts.get("journal_hours_differ_from_saved"):
        items.append({
            "code": "journal_hours_differ_from_saved",
            "level": "warn",
            "message": "已核准日誌草稿的工時與已儲存計價工時不同。預填採用已核准草稿，沒有改計價工時。",
        })
    return items


def _drivers_from_context(context: dict) -> str:
    found = [name for name in _people(context) if name in SIGN_SLIP_DRIVERS]
    return "、".join(dict.fromkeys(found))


def _slip_window(hours: dict) -> tuple[str | None, str | None]:
    start = hours["assignment_start"] or hours["check_in"]
    end = hours["assignment_end"] or hours["check_out"]
    return start, end


async def _pricing_amount(session: Session, document_id: int | None, site_code: str, site_name: str, work_date: date, hours: dict) -> tuple[int | None, str, str | None]:
    if document_id is None:
        return None, "未選擇計價表，金額留空。", None
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise OpsGuard(404, "找不到計價文件")
    try:
        content = await google_drive_worklog_service.download_file_bytes(document.drive_file_id)
        parameters = read_pricing_parameters(content, document.original_file_name)
    except Exception as exc:
        logger.warning("讀取計價單價失敗：%s", type(exc).__name__)
        return None, "計價表讀取失敗，金額留空，沒有改檔案。", None
    label = match_label_for_site(site_code, site_name, list(parameters.labels))
    if label is None:
        return None, "這個工地對不到計價標別，金額留空。", None
    rates = parameters.rates_for(label)
    holiday = is_canonical_holiday(work_date)
    cost = day_cost(rates, hours["normal_hours"], hours["overtime_hours"], hours["support_hours"], holiday)
    amount = int(round(cost.total))
    day_kind = "假日費率" if holiday else "平日費率"
    basis = (
        f"標別 {label}，{day_kind}。正常 {cost.normal_amount:g}、加班 {cost.overtime_amount:g}、"
        f"支援 {cost.support_amount:g}，合計 {amount} 元。只寫在草稿，沒有改計價檔。"
    )
    return amount, basis, label


def _journal_for_slip(session: Session, work_date: date, worksite_id: int) -> AiJournalDraft | None:
    approved = session.exec(
        select(AiJournalDraft).where(
            AiJournalDraft.work_date == work_date,
            AiJournalDraft.worksite_id == worksite_id,
            AiJournalDraft.status == "approved",
        ).order_by(AiJournalDraft.id.desc())
    ).first()
    if approved is not None:
        return approved
    return _latest(session, AiJournalDraft, work_date, worksite_id)


async def create_sign_slip_draft(
    session: Session,
    actor: Employee,
    work_date: date,
    worksite_id: int,
    *,
    document_id: int | None = None,
    open_new: bool = False,
) -> AiSignSlipDraft:
    context = gather_site_day(session, work_date, worksite_id)
    confirmed = session.exec(
        select(AiSignSlipDraft).where(
            AiSignSlipDraft.work_date == work_date,
            AiSignSlipDraft.worksite_id == worksite_id,
            AiSignSlipDraft.status == "confirmed",
        )
    ).first()
    if confirmed is not None and not open_new:
        raise OpsGuard(409, "此工地當天已有確認過的簽單草稿，不會另建，也不會修改已存簽單。")
    for row in session.exec(
        select(AiSignSlipDraft).where(
            AiSignSlipDraft.work_date == work_date,
            AiSignSlipDraft.worksite_id == worksite_id,
            AiSignSlipDraft.status == "draft",
        )
    ).all():
        row.status = "superseded"
        row.updated_at = datetime.utcnow()
        session.add(row)

    computed = compute_hours(context)
    journal = _journal_for_slip(session, work_date, worksite_id)
    hours = dict(computed)
    journal_not_approved = False
    journal_hours_differ = False
    content_source = "records"
    work_lines = _work_lines(context)
    if journal is not None:
        content_source = "approved_journal" if journal.status == "approved" else "draft_journal"
        journal_not_approved = journal.status != "approved"
        work_lines = list((journal.content or {}).get("work_items") or work_lines)
        summary = (journal.content or {}).get("work_summary")
        if summary:
            work_lines = [summary, *work_lines]
        if journal.status == "approved":
            hours["normal_hours"] = float(journal.normal_hours)
            hours["overtime_hours"] = float(journal.overtime_hours)
            hours["support_hours"] = float(journal.support_hours)
            saved = context.get("saved_hours")
            if saved and (
                abs(saved["normal_hours"] - hours["normal_hours"]) > 0.01
                or abs(saved["overtime_hours"] - hours["overtime_hours"]) > 0.01
            ):
                journal_hours_differ = True
    start, end = _slip_window(computed)
    amount, amount_basis, label = await _pricing_amount(
        session, document_id, context["site_code"], context["site_name"], work_date, hours,
    )
    customer = _customer_from_rule(context["site_name"]) or _historical_customer(session, worksite_id)
    fields = {
        "customer_name": customer,
        "driver_names": _drivers_from_context(context),
        "amount": amount,
        "start_time": start,
        "end_time": end,
    }
    facts = {
        "check_in": computed["check_in"],
        "check_out": computed["check_out"],
        "attendance_incomplete": computed["attendance_incomplete"],
        "journal_not_approved": journal_not_approved,
        "hour_flags": computed["flags"],
        "journal_hours_differ_from_saved": journal_hours_differ,
        "pricing_label": label,
    }
    uncertainties = assess_sign_slip(fields, facts)
    draft = AiSignSlipDraft(
        work_date=work_date,
        worksite_id=worksite_id,
        journal_draft_id=journal.id if journal else None,
        customer_name=customer,
        site_code=context["site_code"],
        location=context["site_name"],
        work_content="\n".join(work_lines),
        vehicles=computed["vehicles"],
        forklift_count=computed["forklift_count"],
        normal_hours=hours["normal_hours"],
        overtime_hours=hours["overtime_hours"],
        support_hours=hours["support_hours"],
        total_hours=hours["normal_hours"] + hours["overtime_hours"],
        start_time=start,
        end_time=end,
        amount=amount,
        amount_basis=amount_basis,
        driver_names=fields["driver_names"] or None,
        uncertainties=uncertainties,
        source_refs={"checks": facts, "journal_draft_id": journal.id if journal else None, "content_source": content_source},
        content_source=content_source,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    _audit(session, actor, "create", "ai_sign_slip_draft", draft.id, f"{work_date.isoformat()} {context['site_name']} 簽單預填，尚未建立正式簽單")
    return draft


def _refresh_slip_flags(session: Session, draft: AiSignSlipDraft) -> None:
    context = gather_site_day(session, draft.work_date, draft.worksite_id)
    computed = compute_hours(context)
    journal = session.get(AiJournalDraft, draft.journal_draft_id) if draft.journal_draft_id else None
    facts = {
        "check_in": computed["check_in"],
        "check_out": computed["check_out"],
        "attendance_incomplete": computed["attendance_incomplete"],
        "journal_not_approved": bool(journal and journal.status != "approved"),
        "hour_flags": computed["flags"],
        "journal_hours_differ_from_saved": bool((draft.source_refs or {}).get("checks", {}).get("journal_hours_differ_from_saved")),
        "pricing_label": (draft.source_refs or {}).get("checks", {}).get("pricing_label"),
    }
    draft.uncertainties = assess_sign_slip({
        "customer_name": draft.customer_name,
        "driver_names": draft.driver_names,
        "amount": draft.amount,
        "start_time": draft.start_time,
        "end_time": draft.end_time,
    }, facts)
    source_refs = dict(draft.source_refs or {})
    source_refs["checks"] = facts
    draft.source_refs = source_refs


def update_sign_slip_draft(session: Session, actor: Employee, draft_id: int, payload: dict) -> AiSignSlipDraft:
    draft = session.get(AiSignSlipDraft, draft_id)
    if draft is None:
        raise OpsGuard(404, "找不到簽單草稿")
    if draft.status != "draft":
        raise OpsGuard(409, "只有未確認的簽單草稿可以修改。")
    if "customer_name" in payload:
        draft.customer_name = _clean(payload.get("customer_name"), 80) or None
    if "work_content" in payload:
        draft.work_content = _clean(payload.get("work_content"), 2000)
    if "driver_names" in payload:
        draft.driver_names = _clean(payload.get("driver_names"), 80) or None
    if "normal_hours" in payload:
        draft.normal_hours = _hour(payload["normal_hours"])
    if "overtime_hours" in payload:
        draft.overtime_hours = _hour(payload["overtime_hours"])
    if "support_hours" in payload:
        draft.support_hours = _hour(payload["support_hours"])
    draft.total_hours = draft.normal_hours + draft.overtime_hours
    if "start_time" in payload:
        draft.start_time = _time_label(payload.get("start_time"))
    if "end_time" in payload:
        draft.end_time = _time_label(payload.get("end_time"))
    if "amount" in payload:
        amount = payload.get("amount")
        if amount is None or amount == "":
            draft.amount = None
            draft.amount_basis = "管理員清掉金額。未套用估價。"
        else:
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise OpsGuard(400, "金額請填 0 以上的整數元，或留空。")
            draft.amount = amount
            draft.amount_basis = "管理員手動填寫，沒有改計價規則。"
    if "vehicles" in payload:
        vehicles = payload["vehicles"] or {}
        cleaned = {}
        for key in ("twoPointFive", "threePointZero", "fourPointFive", "truck"):
            try:
                cleaned[key] = max(0, int(vehicles.get(key) or 0))
            except (TypeError, ValueError) as exc:
                raise OpsGuard(400, "車輛台數必須是整數") from exc
        draft.vehicles = cleaned
        draft.forklift_count = cleaned["twoPointFive"] + cleaned["threePointZero"] + cleaned["fourPointFive"]
    draft.updated_by_id = actor.id
    draft.updated_at = datetime.utcnow()
    _refresh_slip_flags(session, draft)
    session.add(draft)
    session.commit()
    session.refresh(draft)
    _audit(session, actor, "update", "ai_sign_slip_draft", draft.id, "修改簽單草稿，尚未建立正式簽單")
    return draft


def confirm_sign_slip_draft(
    session: Session,
    actor: Employee,
    draft_id: int,
    slip_no: str,
    acknowledge: bool,
) -> tuple[AiSignSlipDraft, SignSlipRecord]:
    draft = session.get(AiSignSlipDraft, draft_id)
    if draft is None:
        raise OpsGuard(404, "找不到簽單草稿")
    if draft.status != "draft":
        raise OpsGuard(409, "這份簽單草稿已結束，不會再建立簽單。")
    _refresh_slip_flags(session, draft)
    blocking = [item for item in draft.uncertainties if item.get("level") == "block"]
    warnings = [item for item in draft.uncertainties if item.get("level") == "warn"]
    number = _clean(slip_no, 40)
    if not number:
        blocking.append({"code": "missing_slip_no", "level": "block", "message": "請填寫紙本單號。系統不會自動編號。"})
    elif session.exec(select(SignSlipRecord).where(SignSlipRecord.slip_no == number)).first():
        raise OpsGuard(409, "這個單號已經存在，不會覆蓋。")
    if blocking:
        draft.uncertainties = blocking + warnings
        session.add(draft)
        session.commit()
        raise OpsGuard(409, "還有必須處理的項目：" + "；".join(item["message"] for item in blocking))
    if warnings and not acknowledge:
        raise OpsGuard(409, "還有待核對項目。確認前請勾選已核對，系統不會自動當作沒問題。")
    existing = session.exec(
        select(SignSlipRecord).where(
            SignSlipRecord.worksite_id == draft.worksite_id,
            SignSlipRecord.slip_date == draft.work_date,
            SignSlipRecord.is_active.is_(True),
        )
    ).first()
    if existing is not None:
        raise OpsGuard(409, f"當天這個工地已有簽單 {existing.slip_no}，不會修改。")
    note = f"由簽單草稿 {draft.id} 經管理員確認建立。"
    if warnings:
        note += "已核對：" + "、".join(item["code"] for item in warnings) + "。"
    slip = SignSlipRecord(
        slip_no=number,
        slip_date=draft.work_date,
        customer_name=draft.customer_name,
        worksite_id=draft.worksite_id,
        site_code=draft.site_code,
        location=draft.location,
        work_content=draft.work_content,
        vehicles=draft.vehicles or {},
        forklift_count=draft.forklift_count,
        normal_hours=draft.normal_hours,
        overtime_hours=draft.overtime_hours,
        total_hours=draft.total_hours,
        start_time=draft.start_time,
        end_time=draft.end_time,
        amount=draft.amount,
        driver_names=draft.driver_names,
        notes=note,
        created_by_id=actor.id,
    )
    session.add(slip)
    session.commit()
    session.refresh(slip)
    draft.status = "confirmed"
    draft.sign_slip_id = slip.id
    draft.updated_by_id = actor.id
    draft.updated_at = datetime.utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    _audit(session, actor, "confirm", "ai_sign_slip_draft", draft.id, f"確認簽單草稿並新增 {number}，沒有修改其他簽單")
    return draft, slip


def serialize_sign_slip_draft(row: AiSignSlipDraft) -> dict:
    return {
        "id": row.id,
        "work_date": row.work_date.isoformat(),
        "worksite_id": row.worksite_id,
        "journal_draft_id": row.journal_draft_id,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "customer_name": row.customer_name,
        "site_code": row.site_code,
        "location": row.location,
        "work_content": row.work_content,
        "vehicles": row.vehicles or {},
        "forklift_count": row.forklift_count,
        "normal_hours": row.normal_hours,
        "overtime_hours": row.overtime_hours,
        "support_hours": row.support_hours,
        "total_hours": row.total_hours,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "amount": row.amount,
        "amount_basis": row.amount_basis,
        "driver_names": row.driver_names,
        "uncertainties": row.uncertainties or [],
        "source_refs": row.source_refs or {},
        "content_source": row.content_source,
        "sign_slip_id": row.sign_slip_id,
        "is_draft": row.status == "draft",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _near(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return True
    return abs(float(left) - float(right)) <= 0.01


def build_billing_rows(session: Session, month: str, workbook_rows: dict[tuple[str, date], dict] | None, label_by_site: dict[int, str | None]) -> list[dict]:
    first, last = month_bounds(month)
    start_at, end_at = local_day_bounds(first)[0], local_day_bounds(last)[1]
    worksites = {row.id: row for row in session.exec(select(Worksite)).all()}
    saved = session.exec(
        select(WorksiteJournalHours).where(WorksiteJournalHours.work_date >= first, WorksiteJournalHours.work_date <= last)
    ).all()
    slips = session.exec(
        select(SignSlipRecord).where(
            SignSlipRecord.slip_date >= first,
            SignSlipRecord.slip_date <= last,
            SignSlipRecord.is_active.is_(True),
        )
    ).all()
    imports = session.exec(
        select(WorkHourImportLog).where(WorkHourImportLog.work_date >= first, WorkHourImportLog.work_date <= last)
        .order_by(WorkHourImportLog.id)
    ).all()
    assignments = session.exec(
        select(WorkAssignment).where(WorkAssignment.status != "cancelled", WorkAssignment.work_date >= first, WorkAssignment.work_date <= last)
    ).all()
    inspections = session.exec(
        select(ForkliftInspection).where(
            ForkliftInspection.inspection_date >= first,
            ForkliftInspection.inspection_date <= last,
        )
    ).all()
    attendance = session.exec(
        select(AttendanceEvent).where(AttendanceEvent.happened_at >= start_at, AttendanceEvent.happened_at < end_at)
    ).all()
    latest_import: dict[tuple[int | None, date], WorkHourImportLog] = {}
    for row in imports:
        latest_import[(row.worksite_id, row.work_date)] = row

    keys: set[tuple[int | None, date]] = set()
    for row in saved:
        keys.add((row.worksite_id, row.work_date))
    for row in slips:
        keys.add((row.worksite_id, row.slip_date))
    for row in latest_import:
        keys.add(row)
    for row in assignments:
        keys.add((row.site_id, row.work_date))
    for row in inspections:
        keys.add((row.site_id, row.inspection_date))
    for event in attendance:
        local_day = event.happened_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).date()
        if first <= local_day <= last:
            keys.add((event.site_id, local_day))

    saved_map = {(row.worksite_id, row.work_date): row for row in saved}
    slip_map: dict[tuple[int | None, date], list[SignSlipRecord]] = {}
    for row in slips:
        slip_map.setdefault((row.worksite_id, row.slip_date), []).append(row)

    rows: list[dict] = []
    for site_id, day in sorted(keys, key=lambda item: (item[1], item[0] or 0)):
        site = worksites.get(site_id) if site_id else None
        label = label_by_site.get(site_id) if site_id else None
        saved_row = saved_map.get((site_id, day))
        day_slips = slip_map.get((site_id, day), [])
        imported = latest_import.get((site_id, day))
        workbook = None
        if workbook_rows is not None and label:
            workbook = workbook_rows.get((label, day))
        day_attendance = [
            event for event in attendance
            if event.site_id == site_id and event.happened_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).date() == day
        ]
        check_ins = [event.happened_at for event in day_attendance if event.event_type == "上班打卡"]
        check_outs = [event.happened_at for event in day_attendance if event.event_type == "下班打卡"]
        facts = {
            "work_date": day.isoformat(),
            "site_name": site.name if site else "未判定工地",
            "label": label,
            "journal_normal": None if saved_row is None else saved_row.normal_hours,
            "journal_overtime": None if saved_row is None else saved_row.overtime_hours,
            "journal_support": None if saved_row is None else saved_row.support_hours,
            "slip_count": len(day_slips),
            "slip_normal": day_slips[0].normal_hours if day_slips else None,
            "slip_overtime": day_slips[0].overtime_hours if day_slips else None,
            "slip_total": day_slips[0].total_hours if day_slips else None,
            "slip_amount": day_slips[0].amount if day_slips else None,
            "slip_no": day_slips[0].slip_no if day_slips else None,
            "billed_normal": None if imported is None else imported.normal_hours,
            "billed_overtime": None if imported is None else imported.overtime_hours,
            "billed_support": None if imported is None else imported.support_hours,
            "workbook_normal": None if workbook is None else workbook.get("normal_hours"),
            "workbook_overtime": None if workbook is None else workbook.get("overtime_hours"),
            "check_in": _local_hhmm(min(check_ins)) if check_ins else None,
            "check_out": _local_hhmm(max(check_outs)) if check_outs else None,
        }
        discrepancies = _day_discrepancies(facts, day_slips, bool(check_ins), bool(check_outs), saved_row is not None or bool(day_slips) or imported is not None)
        for item in discrepancies:
            item.update({
                "worksite_id": site_id,
                "site_name": facts["site_name"],
                "site_code": site.code if site else None,
                "label": label,
                "work_date": day.isoformat(),
                "facts": facts,
            })
            rows.append(item)
    return rows


def _day_discrepancies(facts: dict, slips: list[SignSlipRecord], has_in: bool, has_out: bool, had_work: bool) -> list[dict]:
    found = []

    def add(code: str, severity: str, explanation: str, suggested_fix: str) -> None:
        found.append({
            "code": code,
            "severity": severity,
            "explanation": explanation,
            "suggested_fix": suggested_fix,
            "explanation_source": "template",
        })

    if slips and not _near((facts["slip_normal"] or 0) + (facts["slip_overtime"] or 0), facts["slip_total"]):
        add(
            "slip_total_mismatch",
            "error",
            f"簽單 {facts['slip_no']} 正常 {facts['slip_normal']}、加班 {facts['slip_overtime']}，合計不等於總時數 {facts['slip_total']}。",
            "請人工核對紙本。系統不會改這張簽單的時數或金額。",
        )
    if facts["journal_normal"] is not None and facts["slip_normal"] is not None and (
        not _near(facts["journal_normal"], facts["slip_normal"]) or not _near(facts["journal_overtime"], facts["slip_overtime"])
    ):
        add(
            "journal_vs_slip",
            "error",
            f"日誌工時正常 {facts['journal_normal']}、加班 {facts['journal_overtime']}，簽單 {facts['slip_no']} 為正常 {facts['slip_normal']}、加班 {facts['slip_overtime']}。",
            "請決定以日誌或簽單為準後，再用既有畫面手動修改。不會自動改金額。",
        )
    if facts["journal_normal"] is not None and facts["billed_normal"] is not None and (
        not _near(facts["journal_normal"], facts["billed_normal"])
        or not _near(facts["journal_overtime"], facts["billed_overtime"])
        or not _near(facts["journal_support"], facts["billed_support"])
    ):
        add(
            "journal_vs_billed",
            "error",
            f"日誌工時正常 {facts['journal_normal']}、加班 {facts['journal_overtime']}、支援 {facts['journal_support']}；已寫入計價為正常 {facts['billed_normal']}、加班 {facts['billed_overtime']}、支援 {facts['billed_support']}。",
            "請用既有的計價預覽核對後再決定是否手動寫入。這份報告不會寫入 Google Drive。",
        )
    if facts["slip_normal"] is not None and facts["billed_normal"] is not None and not _near(facts["slip_normal"], facts["billed_normal"]):
        add(
            "slip_vs_billed",
            "error",
            f"簽單正常工時 {facts['slip_normal']}，已寫入計價的正常工時 {facts['billed_normal']}。",
            "請人工核對後再用既有匯入流程處理。不會自動改計價檔。",
        )
    if facts["workbook_normal"] is not None and facts["billed_normal"] is not None and not _near(facts["workbook_normal"], facts["billed_normal"]):
        add(
            "workbook_vs_import",
            "error",
            f"計價檔目前正常工時 {facts['workbook_normal']}，系統最後寫入紀錄是 {facts['billed_normal']}。",
            "檔案可能在寫入後被改過。請打開原檔核對，系統不會把報告數字寫回去。",
        )
    if slips and facts["check_in"] and facts["check_out"] and slips[0].start_time and slips[0].end_time:
        if abs((_minutes(slips[0].start_time) or 0) - (_minutes(facts["check_in"]) or 0)) > 30 or abs((_minutes(slips[0].end_time) or 0) - (_minutes(facts["check_out"]) or 0)) > 30:
            add(
                "attendance_vs_slip_window",
                "warning",
                f"簽單時間 {slips[0].start_time}–{slips[0].end_time}，打卡約 {facts['check_in']}–{facts['check_out']}。",
                "請看打卡與紙本時間。報告只列出差異，不改工時。",
            )
    if has_in and not has_out and had_work:
        add("attendance_incomplete", "warning", "有上班打卡，沒有下班打卡。", "請向現場確認下班時間。不會用估算時數補上。")
    if had_work and facts["journal_normal"] not in (None, 0) and not slips:
        add("missing_slip", "info", f"日誌有正常工時 {facts['journal_normal']}，當天沒有有效簽單。", "若需要請款，請用既有簽單畫面人工建立。")
    if had_work and (facts["journal_normal"] not in (None, 0) or facts["slip_normal"] not in (None, 0)) and facts["billed_normal"] is None:
        add("missing_bill", "info", "有日誌或簽單工時，但沒有寫入計價的紀錄。", "請用既有整月預覽確認後再決定是否寫入。這份報告不會寫入。")
    return found


async def complete_billing_explanations(rows: list[dict]) -> BillingExplanations:
    if not settings.ai_ops_enabled:
        raise AiOpsUnavailable("disabled")
    key = settings.openai_api_key.strip()
    if not key:
        raise AiOpsUnavailable("missing_key")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise AiOpsUnavailable("openai_missing") from exc
    payload = [
        {"index": index, "code": row["code"], "explanation": row["explanation"], "facts": row["facts"]}
        for index, row in enumerate(rows[:40])
    ]
    client = AsyncOpenAI(api_key=key, timeout=25, max_retries=0)
    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": "你用繁體中文說明已算出的計價差異。不要改數字，不要建議自動改金額或覆蓋簽單。每一筆的 index 必須對應輸入。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            response_format=BillingExplanations,
            store=False,
            max_completion_tokens=1600,
        )
    except Exception as exc:
        logger.warning("計價核對說明模型失敗：%s", type(exc).__name__)
        raise AiOpsUnavailable(type(exc).__name__) from exc
    message = completion.choices[0].message
    if getattr(message, "refusal", None) or getattr(message, "parsed", None) is None:
        raise AiOpsUnavailable("refusal")
    return message.parsed


def apply_billing_explanations(rows: list[dict], explanations: BillingExplanations | None) -> tuple[list[dict], int]:
    if explanations is None:
        return rows, 0
    applied = 0
    by_index = {item.index: item for item in explanations.items}
    for index, row in enumerate(rows[:40]):
        item = by_index.get(index)
        if item is None:
            continue
        allowed = json.dumps({"facts": row["facts"], "explanation": row["explanation"]}, ensure_ascii=False, default=str)
        explanation = _clean(item.explanation, 500)
        suggestion = _clean(item.suggested_fix, 400)
        if explanation and explanation_grounded(explanation, allowed) and explanation_grounded(suggestion, allowed):
            row["ai_explanation"] = explanation
            row["ai_suggested_fix"] = suggestion
            row["explanation_source"] = "ai"
            applied += 1
        else:
            row["ai_rejected"] = True
    return rows, applied


async def create_billing_check(session: Session, actor: Employee, month: str, document_id: int | None) -> AiBillingCheck:
    first, _last = month_bounds(month)
    workbook_rows = None
    label_by_site: dict[int, str | None] = {}
    workbook_note = "未讀計價檔，已寫入工時只對系統內的匯入紀錄。"
    if document_id is not None:
        document = session.get(ManagedDocument, document_id)
        if document is None:
            raise OpsGuard(404, "找不到計價文件")
        try:
            content = await google_drive_worklog_service.download_file_bytes(document.drive_file_id)
            month_data = read_month_cost_data(content, document.original_file_name, roc_period(first))
            workbook_rows = {}
            for label, sheet_rows in month_data.sheets.items():
                for sheet_row in sheet_rows:
                    workbook_rows[(label, sheet_row.day)] = {
                        "normal_hours": sheet_row.normal_hours,
                        "overtime_hours": sheet_row.overtime_hours,
                        "support_hours": sheet_row.support_hours,
                    }
            labels = list(month_data.parameters.labels)
            for site in session.exec(select(Worksite)).all():
                label_by_site[site.id] = match_label_for_site(site.code, site.name, labels)
            workbook_note = "已讀計價檔工時做核對，沒有寫回檔案。"
        except (CostWorkbookError, Exception) as exc:
            logger.warning("計價核對讀檔失敗：%s", type(exc).__name__)
            workbook_note = "計價檔讀取失敗，只核對系統內的日誌、簽單、打卡與匯入紀錄。"
    rows = build_billing_rows(session, month, workbook_rows, label_by_site)
    ai_status = "disabled"
    model_name = None
    explanations = None
    if settings.ai_ops_enabled and rows:
        try:
            explanations = await complete_billing_explanations(rows)
            ai_status = "drafted"
            model_name = settings.openai_model
        except AiOpsUnavailable as exc:
            ai_status = "disabled" if exc.reason == "disabled" else "failed"
    rows, applied = apply_billing_explanations(rows, explanations)
    if ai_status == "drafted" and applied == 0 and rows:
        ai_status = "failed"
    report = {
        "month": month,
        "workbook_note": workbook_note,
        "summary": _billing_summary(rows),
        "rows": rows,
        "ai_applied": applied,
        "changes_amounts": False,
    }
    item = AiBillingCheck(
        month=month,
        document_id=document_id,
        discrepancy_count=len(rows),
        report=report,
        ai_status=ai_status,
        model_name=model_name,
        created_by_id=actor.id,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    _audit(session, actor, "create", "ai_billing_check", item.id, f"{month} 計價核對 {len(rows)} 筆差異，未改金額")
    return item


def _billing_summary(rows: list[dict]) -> str:
    if not rows:
        return "這個月沒有找出日誌、簽單、打卡與已寫入工時之間的差異。"
    errors = sum(1 for row in rows if row["severity"] == "error")
    warnings = sum(1 for row in rows if row["severity"] == "warning")
    infos = sum(1 for row in rows if row["severity"] == "info")
    return f"共 {len(rows)} 項：需處理 {errors}、待核對 {warnings}、提醒 {infos}。沒有自動修改金額或工時。"


def serialize_billing(row: AiBillingCheck) -> dict:
    return {
        "id": row.id,
        "month": row.month,
        "document_id": row.document_id,
        "discrepancy_count": row.discrepancy_count,
        "report": row.report or {},
        "ai_status": row.ai_status,
        "model_name": row.model_name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "changes_amounts": False,
    }


def active_sites_for_day(session: Session, work_date: date) -> tuple[list[int], int]:
    start_at, end_at = local_day_bounds(work_date)
    site_ids: set[int] = set()
    unassigned = 0
    queries = [
        select(WorkAssignment.site_id).where(WorkAssignment.status != "cancelled", WorkAssignment.work_date == work_date),
        select(ForkliftInspection.site_id).where(ForkliftInspection.inspection_date == work_date),
        select(GroupTextLog.site_id).where(GroupTextLog.sent_at >= start_at, GroupTextLog.sent_at < end_at),
        select(AttendanceEvent.site_id).where(AttendanceEvent.happened_at >= start_at, AttendanceEvent.happened_at < end_at),
        select(PhotoUploadLog.site_id).where(PhotoUploadLog.uploaded_at >= start_at, PhotoUploadLog.uploaded_at < end_at),
        select(WorkReportEvent.site_id).where(WorkReportEvent.reported_at >= start_at, WorkReportEvent.reported_at < end_at),
    ]
    for statement in queries:
        for site_id in session.exec(statement).all():
            if site_id is None:
                unassigned += 1
            else:
                site_ids.add(site_id)
    active = set(session.exec(select(Worksite.id).where(Worksite.is_active.is_(True))).all())
    return sorted(site_id for site_id in site_ids if site_id in active), unassigned


async def prepare_evening_drafts(session: Session, work_date: date) -> dict[str, Any]:
    site_ids, unassigned = active_sites_for_day(session, work_date)
    created: list[int] = []
    for site_id in site_ids:
        exists = session.exec(
            select(AiJournalDraft).where(AiJournalDraft.work_date == work_date, AiJournalDraft.worksite_id == site_id)
        ).first()
        if exists is not None:
            continue
        draft = await create_journal_draft(session, None, work_date, site_id)
        created.append(draft.id)
    pending = session.exec(
        select(AiJournalDraft).where(AiJournalDraft.work_date == work_date, AiJournalDraft.status == "draft")
    ).all()
    names = []
    for draft in pending:
        site = session.get(Worksite, draft.worksite_id)
        if site and site.name not in names:
            names.append(site.name)
    message = (
        f"【三通工程行】{work_date:%Y/%m/%d} 的工作日誌草稿已準備"
        f"（{('、'.join(names)) if names else '沒有待審工地'}）。"
        "請到後台「今日各工地工作日誌」審核。草稿尚未核准，也不會自動產生簽單或改計價。"
    )
    if not settings.ai_ops_enabled:
        message += "AI 未開啟，這次是依現有派工與群組資料整理。"
    if unassigned:
        message += "另有未判定工地的訊息，請人工整理。"
    return {
        "created": created,
        "pending": len(pending),
        "unassigned": unassigned,
        "message": message,
        "notify": bool(pending),
    }
