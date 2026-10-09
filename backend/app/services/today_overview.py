"""Read-only today dashboard for the admin home page.

Counts use the Asia/Taipei calendar day. Timestamp columns are stored as naive
UTC, so the day window is converted the same way as other Taiwan-day queries.
This module only reads. It does not write attendance, drafts, backups, or Drive.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    AiBillingCheck,
    AiJournalDraft,
    AiSignSlipDraft,
    AssignmentMember,
    AssignmentStatus,
    AttendanceEvent,
    Employee,
    EmployeeStatus,
    Forklift,
    ForkliftInspection,
    ForkliftStatus,
    LeaveRequest,
    LeaveStatus,
    NotificationBatch,
    Role,
    SignSlipRecord,
    WorkAssignment,
    WorkReportEvent,
    Worksite,
    WorksiteJournalHours,
)
from app.services.ai_ops import local_day_bounds
from app.services.google_drive import google_drive_worklog_service

CLOCK_IN = {"上班打卡", "到達工地"}
CLOCK_OUT = {"下班打卡", "離開工地"}
LIST_LIMIT = 8
DRIVE_ALERT_SCOPE = "google_drive_upload_access"


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def taipei_now() -> datetime:
    return datetime.now(_zone())


def _as_taipei(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_zone())


def _hhmm(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_taipei(value).strftime("%H:%M")


def _person(employee: Employee) -> dict:
    return {"code": employee.employee_code, "name": employee.name}


def _is_operator(employee: Employee) -> bool:
    title = employee.title or ""
    return "堆高機" in title or "叉車" in title or "forklift" in title.lower()


def _clip(items: list, limit: int = LIST_LIMIT) -> list:
    return items[:limit]


def build_today_overview(session: Session, now: datetime | None = None) -> dict:
    current = now or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(_zone())
    today = current.date()
    start_at, end_at = local_day_bounds(today)
    employees = {
        row.id: row
        for row in session.exec(select(Employee)).all()
        if row.id is not None
    }
    worksites = {row.id: row for row in session.exec(select(Worksite)).all()}
    active_people = [
        row for row in employees.values()
        if row.status == EmployeeStatus.active and row.role != Role.external
    ]

    assignments = session.exec(
        select(WorkAssignment).where(
            WorkAssignment.work_date == today,
            WorkAssignment.status != AssignmentStatus.cancelled,
        )
    ).all()
    assignment_ids = [row.id for row in assignments if row.id is not None]
    members = session.exec(
        select(AssignmentMember).where(AssignmentMember.assignment_id.in_(assignment_ids))
    ).all() if assignment_ids else []
    active_members = [row for row in members if row.is_active]
    events = session.exec(
        select(AttendanceEvent).where(
            AttendanceEvent.happened_at >= start_at,
            AttendanceEvent.happened_at < end_at,
        )
    ).all()
    reports = session.exec(
        select(WorkReportEvent).where(
            WorkReportEvent.reported_at >= start_at,
            WorkReportEvent.reported_at < end_at,
            WorkReportEvent.event_type == "異常回報",
        )
    ).all()
    inspections = session.exec(
        select(ForkliftInspection).where(ForkliftInspection.inspection_date == today)
    ).all()
    leaves = session.exec(select(LeaveRequest)).all()
    journal_hours = session.exec(
        select(WorksiteJournalHours).where(WorksiteJournalHours.work_date == today)
    ).all()
    sign_slips = session.exec(
        select(SignSlipRecord).where(
            SignSlipRecord.slip_date == today,
            SignSlipRecord.is_active == True,  # noqa: E712
        )
    ).all()
    journal_drafts = session.exec(
        select(AiJournalDraft).where(AiJournalDraft.status == "draft")
    ).all()
    slip_drafts = session.exec(
        select(AiSignSlipDraft).where(AiSignSlipDraft.status == "draft")
    ).all()
    month = today.strftime("%Y-%m")
    billing_rows = session.exec(
        select(AiBillingCheck).where(AiBillingCheck.month == month).order_by(AiBillingCheck.id.desc())
    ).all()
    forklifts = session.exec(select(Forklift)).all()
    drive_alert = session.exec(
        select(NotificationBatch).where(
            NotificationBatch.target_scope == DRIVE_ALERT_SCOPE,
            NotificationBatch.target_value == today.isoformat(),
        )
    ).first()

    covering = {
        row.employee_id: row
        for row in leaves
        if row.status in {LeaveStatus.approved, LeaveStatus.pending}
        and row.start_date <= today <= row.end_date
    }
    pending_leaves = [row for row in leaves if row.status == LeaveStatus.pending]
    assigned_ids = {row.employee_id for row in active_members}
    events_by_person: dict[int, list[AttendanceEvent]] = {}
    for event in events:
        events_by_person.setdefault(event.employee_id, []).append(event)

    def earliest(person_id: int, kinds: set[str]) -> AttendanceEvent | None:
        matched = [row for row in events_by_person.get(person_id, []) if row.event_type in kinds]
        return min(matched, key=lambda row: row.happened_at) if matched else None

    def latest(person_id: int, kinds: set[str]) -> AttendanceEvent | None:
        matched = [row for row in events_by_person.get(person_id, []) if row.event_type in kinds]
        return max(matched, key=lambda row: row.happened_at) if matched else None

    site_by_employee: dict[int, str] = {}
    for member in active_members:
        assignment = next((row for row in assignments if row.id == member.assignment_id), None)
        site = worksites.get(assignment.site_id) if assignment else None
        if site is not None:
            site_by_employee[member.employee_id] = site.name

    clocked_in = []
    clocked_out = []
    late = []
    missing = []
    on_leave = []
    for employee in sorted(active_people, key=lambda row: row.employee_code):
        person_id = employee.id
        if person_id is None:
            continue
        arrived = earliest(person_id, CLOCK_IN)
        left = latest(person_id, CLOCK_OUT)
        assignment = next(
            (
                row for row in assignments
                if any(member.assignment_id == row.id and member.employee_id == person_id for member in active_members)
            ),
            None,
        )
        start_time = assignment.start_time if assignment and assignment.start_time else time(8, 0)
        cutoff = datetime.combine(today, start_time, tzinfo=_zone()) + timedelta(minutes=15)
        site_name = site_by_employee.get(person_id, "-")
        if person_id in covering and person_id in assigned_ids:
            leave = covering[person_id]
            on_leave.append({**_person(employee), "site": site_name, "leave_type": leave.leave_type})
        if arrived is not None:
            clocked_in.append({**_person(employee), "site": site_name, "time": _hhmm(arrived.happened_at)})
            if assignment and assignment.start_time and _as_taipei(arrived.happened_at) > cutoff:
                late.append({**_person(employee), "site": site_name, "time": _hhmm(arrived.happened_at)})
        if left is not None:
            clocked_out.append({**_person(employee), "site": site_name, "time": _hhmm(left.happened_at)})
        if (
            person_id in assigned_ids
            and arrived is None
            and person_id not in covering
            and current > cutoff
        ):
            missing.append({**_person(employee), "site": site_name})

    grouped: dict[int, dict] = {}
    for assignment in assignments:
        site = worksites.get(assignment.site_id)
        bucket = grouped.setdefault(assignment.site_id, {
            "site_name": site.name if site else "-",
            "people": [],
            "forklifts": [],
            "work_items": [],
            "count": 0,
        })
        bucket["count"] += 1
        if assignment.work_item and assignment.work_item not in bucket["work_items"]:
            bucket["work_items"].append(assignment.work_item)
        for label in (assignment.vehicle, assignment.equipment):
            text = (label or "").strip()
            if text and text not in bucket["forklifts"]:
                bucket["forklifts"].append(text)
        for member in active_members:
            if member.assignment_id != assignment.id:
                continue
            person = employees.get(member.employee_id)
            if person and person.name not in bucket["people"]:
                bucket["people"].append(person.name)
    for machine in forklifts:
        if machine.status != ForkliftStatus.operating or machine.current_site_id not in grouped:
            continue
        bucket = grouped[machine.current_site_id]
        if machine.forklift_code not in bucket["forklifts"]:
            bucket["forklifts"].append(machine.forklift_code)
    assignment_sites = sorted(grouped.values(), key=lambda row: row["site_name"])

    def site_name_of(site_id: int | None) -> str:
        site = worksites.get(site_id)
        return site.name if site else "-"

    review_leaves = [
        {
            **_person(employees[row.employee_id]),
            "leave_type": row.leave_type,
            "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(),
        }
        for row in pending_leaves
        if row.employee_id in employees
    ]
    review_journals = [
        {"id": row.id, "work_date": row.work_date.isoformat(), "site_name": site_name_of(row.worksite_id)}
        for row in journal_drafts
    ]
    review_slips = [
        {
            "id": row.id,
            "work_date": row.work_date.isoformat(),
            "site_name": site_name_of(row.worksite_id),
            "customer_name": row.customer_name or "",
        }
        for row in slip_drafts
    ]
    billing = billing_rows[0] if billing_rows else None
    billing_items = []
    billing_count = 0
    billing_note = "本月尚未產生計價核對報告。"
    if billing is not None:
        billing_count = int(billing.discrepancy_count or 0)
        billing_note = "以下是最近一次核對報告，沒有改動金額或工時。"
        for item in (billing.report or {}).get("rows") or []:
            if not isinstance(item, dict):
                continue
            billing_items.append({
                "site_name": item.get("site_name") or "-",
                "work_date": item.get("work_date") or "",
                "explanation": item.get("explanation") or "",
            })

    inspected_operators = {row.operator_id for row in inspections}
    inspected_machines = {row.forklift_id for row in inspections}
    missed = []
    for employee in active_people:
        if employee.id in inspected_operators or not _is_operator(employee):
            continue
        missed.append({**_person(employee), "detail": "今日尚未點檢"})
    for machine in forklifts:
        if machine.status != ForkliftStatus.operating or machine.id in inspected_machines:
            continue
        if any(item.get("code") == machine.forklift_code for item in missed):
            continue
        missed.append({"code": machine.forklift_code, "name": machine.forklift_code, "detail": "作業中尚未點檢"})
    failed = []
    for row in inspections:
        if row.all_passed:
            continue
        machine = next((item for item in forklifts if item.id == row.forklift_id), None)
        operator = employees.get(row.operator_id)
        failed.append({
            "forklift_code": machine.forklift_code if machine else "-",
            "name": operator.name if operator else "-",
            "site": site_name_of(row.site_id),
            "detail": (row.notes or "點檢有異常項目").strip(),
        })
    abnormal = []
    for row in reports:
        person = employees.get(row.employee_id)
        abnormal.append({
            "name": person.name if person else "-",
            "code": person.employee_code if person else "",
            "site": site_name_of(row.site_id),
            "detail": (row.note or "異常回報").strip(),
            "time": _hhmm(row.reported_at),
        })

    journal_site_ids = {row.worksite_id for row in journal_hours}
    slip_site_ids = {row.worksite_id for row in sign_slips if row.worksite_id is not None}
    missing_journals = []
    missing_slips = []
    for site_id, bucket in grouped.items():
        if site_id not in journal_site_ids:
            missing_journals.append({"site_name": bucket["site_name"]})
        if site_id not in slip_site_ids:
            missing_slips.append({"site_name": bucket["site_name"]})

    drive_warnings = []
    folder_ready = bool(settings.google_drive_worklog_folder_id.strip())
    if not google_drive_worklog_service.is_configured():
        drive_warnings.append({"message": "Google Drive 未設定，雲端備份與新工作照片都不會上傳。"})
    elif not google_drive_worklog_service._has_oauth() or not folder_ready:
        drive_warnings.append({"message": "Google Drive 上傳授權未設定，新工作照片可能無法保存。"})
    if drive_alert is not None and (drive_alert.content or "").strip():
        drive_warnings.append({"message": " ".join(drive_alert.content.split())[:180]})

    review_total = len(review_leaves) + len(review_journals) + len(review_slips) + billing_count
    anomaly_total = (
        len(missed) + len(abnormal) + len(failed) + len(drive_warnings)
        + len(missing_journals) + len(missing_slips)
    )
    return {
        "today": today.isoformat(),
        "timezone": settings.timezone,
        "attendance": {
            "in_count": len(clocked_in),
            "out_count": len(clocked_out),
            "missing_count": len(missing),
            "late_count": len(late),
            "clocked_in": _clip(clocked_in),
            "clocked_out": _clip(clocked_out),
            "missing": _clip(missing),
            "late": _clip(late),
            "on_leave": _clip(on_leave),
            "panel": "attendance",
        },
        "assignments": {
            "count": len(assignments),
            "sites": _clip(assignment_sites, 12),
            "panel": "assignment-list",
        },
        "reviews": {
            "total": review_total,
            "leave_count": len(review_leaves),
            "journal_draft_count": len(review_journals),
            "sign_slip_draft_count": len(review_slips),
            "billing_issue_count": billing_count,
            "leaves": _clip(review_leaves),
            "journal_drafts": _clip(review_journals),
            "sign_slip_drafts": _clip(review_slips),
            "billing": {
                "month": month,
                "count": billing_count,
                "note": billing_note,
                "items": _clip(billing_items, 6),
                "panel": "pricing-month",
            },
            "panels": {
                "leave": "leave",
                "journal": "journal",
                "sign_slip": "journal",
                "billing": "pricing-month",
            },
        },
        "anomalies": {
            "total": anomaly_total,
            "missed_inspection_count": len(missed),
            "abnormal_report_count": len(abnormal),
            "failed_inspection_count": len(failed),
            "drive_warning_count": len(drive_warnings),
            "missing_journal_count": len(missing_journals),
            "missing_sign_slip_count": len(missing_slips),
            "missed_inspections": _clip(missed),
            "abnormal_reports": _clip(abnormal),
            "failed_inspections": _clip(failed),
            "drive_warnings": drive_warnings,
            "missing_journals": _clip(missing_journals),
            "missing_sign_slips": _clip(missing_slips),
            "panels": {
                "inspection": "inspections",
                "report": "attendance",
                "drive": "audit",
                "journal": "journal",
                "sign_slip": "sign-slips",
            },
        },
    }
