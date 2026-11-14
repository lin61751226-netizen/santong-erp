"""Read-only, site-scoped daily workbench over the existing records."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select
from sqlalchemy import and_, or_

from app.core.local_time import local_now, local_today, utc_day_bounds
from app.models import (
    AssignmentMember, AssignmentStatus, AttendanceEvent, Employee, Forklift,
    ForkliftInspection, ForkliftStatus, GroupTextLog, LeaveRequest, LeaveStatus, PhotoUploadLog,
    SignSlipRecord, WorkAssignment, WorkHourImportLog, Worksite,
    WorksiteJournalHours,
)


def build_worksite_board(session: Session, work_date: date, site: Worksite) -> dict:
    start, end = utc_day_bounds(work_date)
    assignments = session.exec(select(WorkAssignment).where(
        WorkAssignment.work_date == work_date, WorkAssignment.site_id == site.id,
        WorkAssignment.status != AssignmentStatus.cancelled,
    ).order_by(WorkAssignment.id)).all()
    assignment_ids = [row.id for row in assignments]
    members = session.exec(select(AssignmentMember).where(
        AssignmentMember.assignment_id.in_(assignment_ids), AssignmentMember.is_active.is_(True)
    )).all() if assignment_ids else []
    people = {row.id: row for row in session.exec(select(Employee)).all()}
    member_ids = {row.employee_id for row in members}
    approved_leave_ids = {row.employee_id for row in session.exec(select(LeaveRequest).where(
        LeaveRequest.status == LeaveStatus.approved,
        LeaveRequest.start_date <= work_date, LeaveRequest.end_date >= work_date,
    )).all()}
    clocks = session.exec(select(AttendanceEvent).where(
        AttendanceEvent.site_id == site.id,
        AttendanceEvent.happened_at >= start, AttendanceEvent.happened_at < end,
    ).order_by(AttendanceEvent.happened_at)).all()
    inspected = session.exec(select(ForkliftInspection).where(
        ForkliftInspection.site_id == site.id,
        ForkliftInspection.inspection_date == work_date,
    ).order_by(ForkliftInspection.id)).all()
    machines = {row.id: row for row in session.exec(select(Forklift)).all()}
    photos = session.exec(select(PhotoUploadLog).where(
        PhotoUploadLog.site_id == site.id,
        or_(PhotoUploadLog.work_date == work_date, and_(
            PhotoUploadLog.work_date.is_(None),
            PhotoUploadLog.uploaded_at >= start, PhotoUploadLog.uploaded_at < end,
        )),
    ).order_by(PhotoUploadLog.id)).all()
    texts = session.exec(select(GroupTextLog).where(
        GroupTextLog.site_id == site.id,
        GroupTextLog.sent_at >= start, GroupTextLog.sent_at < end,
    )).all()
    hours = session.exec(select(WorksiteJournalHours).where(
        WorksiteJournalHours.worksite_id == site.id,
        WorksiteJournalHours.work_date == work_date,
    )).first()
    slips = session.exec(select(SignSlipRecord).where(
        SignSlipRecord.worksite_id == site.id,
        SignSlipRecord.slip_date == work_date,
        SignSlipRecord.is_active.is_(True),
    ).order_by(SignSlipRecord.id)).all()
    imports = session.exec(select(WorkHourImportLog).where(
        WorkHourImportLog.worksite_id == site.id,
        WorkHourImportLog.work_date == work_date,
    ).order_by(WorkHourImportLog.created_at.desc(), WorkHourImportLog.id.desc())).all()

    assignment_rows = []
    for row in assignments:
        names = [people[item.employee_id].name for item in members
                 if item.assignment_id == row.id and item.employee_id in people]
        assignment_rows.append({
            "id": row.id, "work_item": row.work_item, "members": names,
            "time": f"{row.start_time or ''}–{row.end_time or ''}" if row.start_time or row.end_time else "",
            "vehicle": row.vehicle or row.equipment or "",
            "completed": row.is_completed,
        })
    clocked_ids = {row.employee_id for row in clocks
                   if row.event_type in {"上班打卡", "到達工地"}}
    missing_clock = []
    for person_id in sorted(member_ids - clocked_ids - approved_leave_ids):
        if person_id not in people:
            continue
        person_assignments = [assignment for assignment in assignments if any(
            member.assignment_id == assignment.id and member.employee_id == person_id for member in members
        )]
        due = min((assignment.start_time or time(8, 0) for assignment in person_assignments), default=time(8, 0))
        if work_date < local_today() or (work_date == local_today() and local_now().time() >=
                                         (datetime.combine(work_date, due) + timedelta(minutes=15)).time()):
            missing_clock.append(people[person_id].name)
    clock_rows = [{
        "name": people[row.employee_id].name if row.employee_id in people else "未知員工",
        "event_type": row.event_type,
        "happened_at": row.happened_at.isoformat(),
    } for row in clocks]
    inspection_rows = [{
        "machine": machines[row.forklift_id].forklift_code if row.forklift_id in machines else "未知車輛",
        "operator": people[row.operator_id].name if row.operator_id in people else "未知操作員",
        "passed": row.all_passed,
    } for row in inspected]
    # Current fleet location is not historical evidence; only use it for today's reminder.
    expected_machines = [row.forklift_code for row in machines.values()
                         if work_date == local_today() and local_now().time() >= time(8, 30)
                         and row.current_site_id == site.id
                         and row.status == ForkliftStatus.operating]
    inspected_ids = {row.forklift_id for row in inspected}
    missing_inspection = [row.forklift_code for row in machines.values()
                          if row.forklift_code in expected_machines and row.id not in inspected_ids]
    latest_import = imports[0] if imports else None
    slip_changed = bool(latest_import and any(
        row.updated_at and row.updated_at > latest_import.created_at for row in slips
    ))
    if not imports:
        pricing_state = "待核對" if slips or hours else "未查到此工地匯入紀錄"
    elif slip_changed:
        pricing_state = "簽單較匯入紀錄新，需重新核對"
    else:
        pricing_state = "已有匯入紀錄，請核對原檔"

    return {
        "date": work_date.isoformat(),
        "site": {"id": site.id, "code": site.code, "name": site.name},
        "assignments": {"rows": assignment_rows, "missing": not bool(assignments)},
        "attendance": {"rows": clock_rows, "missing_names": missing_clock,
                       "missing": bool(missing_clock), "unassigned": len(clocks) if not assignments else 0},
        "inspections": {"rows": inspection_rows, "expected_machines": expected_machines,
                        "missing_machines": missing_inspection,
                        "missing": bool(missing_inspection),
                        "abnormal": sum(not row.all_passed for row in inspected)},
        "photos": {"count": len(photos), "recent": [
            {"name": row.original_file_name or row.file_name, "url": row.drive_url}
            for row in photos[-3:]
        ]},
        "journal": {"group_text_count": len(texts), "saved_hours": None if hours is None else {
            "normal": hours.normal_hours, "overtime": hours.overtime_hours,
            "support": hours.support_hours,
        }},
        "sign_slips": {"rows": [{"id": row.id, "number": row.slip_no,
                                 "normal": row.normal_hours, "overtime": row.overtime_hours}
                                for row in slips], "missing": bool(assignments) and not slips},
        "pricing": {"state": pricing_state, "import_count": len(imports),
                    "latest_import_at": latest_import.created_at.isoformat() if latest_import else None},
    }
