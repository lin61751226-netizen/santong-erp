from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
import re

from sqlmodel import Session, select

from app.models import (
    AckStatus,
    AssignmentMember,
    AttendanceEvent,
    AttendanceEventType,
    Employee,
    EmployeeStatus,
    LeaveRequest,
    LeaveStatus,
    NotificationCategory,
    Role,
    WorkAssignment,
    Worksite,
)


LEAVE_ACTIVE_STATUSES = {LeaveStatus.pending, LeaveStatus.approved}
ATTENDANCE_COMMAND_MAP = {
    "上班打卡": AttendanceEventType.check_in.value,
    "下班打卡": AttendanceEventType.check_out.value,
    "到達工地": AttendanceEventType.arrive_site.value,
    "離開工地": AttendanceEventType.leave_site.value,
    "外出": AttendanceEventType.go_out.value,
    "返回": AttendanceEventType.return_back.value,
    "加班開始": AttendanceEventType.overtime_start.value,
    "加班結束": AttendanceEventType.overtime_end.value,
}
ATTENDANCE_ACK_MAP = {
    "上班打卡": AckStatus.received,
    "到達工地": AckStatus.arrived,
    "工作開始": AckStatus.started,
    "加班開始": AckStatus.started,
    "工作完成": AckStatus.completed,
    "下班打卡": AckStatus.completed,
    "加班結束": AckStatus.completed,
}


@dataclass
class LeavePolicyResult:
    errors: list[str]
    notes: list[str]
    conflicts: list[dict]


@dataclass
class AttendanceRecordResult:
    event: AttendanceEvent
    assignment: WorkAssignment | None
    anomalies: list[str]


@dataclass
class ReassignmentCandidate:
    employee: Employee
    score: int
    reasons: list[str]


def daterange(start_date: date, end_date: date):
    days = (end_date - start_date).days
    for offset in range(days + 1):
        yield start_date + timedelta(days=offset)


def format_policy_notes(notes: list[str]) -> str | None:
    clean_notes = [note.strip() for note in notes if note and note.strip()]
    return "；".join(clean_notes) if clean_notes else None


def find_leave_conflicts(
    session: Session,
    employee_id: int,
    start_date: date,
    end_date: date,
) -> list[tuple[AssignmentMember, WorkAssignment, Worksite | None]]:
    rows = session.exec(
        select(AssignmentMember, WorkAssignment)
        .join(WorkAssignment, WorkAssignment.id == AssignmentMember.assignment_id)
        .where(
            AssignmentMember.employee_id == employee_id,
            AssignmentMember.is_active.is_(True),
            WorkAssignment.work_date >= start_date,
            WorkAssignment.work_date <= end_date,
        )
        .order_by(WorkAssignment.work_date)
    ).all()
    return [(member, assignment, session.get(Worksite, assignment.site_id)) for member, assignment in rows]


def _count_leave_days_for_month(
    session: Session,
    employee_id: int,
    leave_type: str,
    target_month_start: date,
    target_month_end: date,
    exclude_request_id: int | None = None,
) -> int:
    requests = session.exec(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_type == leave_type,
            LeaveRequest.status.in_(list(LEAVE_ACTIVE_STATUSES)),
            LeaveRequest.start_date <= target_month_end,
            LeaveRequest.end_date >= target_month_start,
        )
    ).all()

    total_days = 0
    for request in requests:
        if exclude_request_id is not None and request.id == exclude_request_id:
            continue
        overlap_start = max(request.start_date, target_month_start)
        overlap_end = min(request.end_date, target_month_end)
        total_days += (overlap_end - overlap_start).days + 1
    return total_days


def evaluate_leave_policy(
    session: Session,
    employee: Employee,
    leave_type: str,
    start_date: date,
    end_date: date,
    requested_on: Optional[date] = None,
    exclude_request_id: int | None = None,
) -> LeavePolicyResult:
    errors: list[str] = []
    notes: list[str] = []
    requested_on = requested_on or date.today()

    overlap_rows = session.exec(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == employee.id,
            LeaveRequest.status.in_(list(LEAVE_ACTIVE_STATUSES)),
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
        )
    ).all()
    overlap_rows = [row for row in overlap_rows if exclude_request_id is None or row.id != exclude_request_id]
    if overlap_rows:
        errors.append("同時段已有待審或已核准的請假申請。")

    conflicts = [
        {
            "assignment_id": assignment.id,
            "work_date": assignment.work_date.isoformat(),
            "site_name": worksite.name if worksite else "-",
            "work_item": assignment.work_item,
        }
        for _, assignment, worksite in find_leave_conflicts(session, employee.id, start_date, end_date)
    ]
    if conflicts:
        notes.append(f"期間內已有 {len(conflicts)} 筆工作安排，核准後需改派。")

    if leave_type == "排休":
        if start_date.month != end_date.month or start_date.year != end_date.year:
            errors.append("排休需在同一個月份內申請，不可跨月。")
        target_month_start = start_date.replace(day=1)
        if start_date.month == 12:
            target_month_end = date(start_date.year + 1, 1, 1) - timedelta(days=1)
        else:
            target_month_end = date(start_date.year, start_date.month + 1, 1) - timedelta(days=1)

        requested_days = (end_date - start_date).days + 1
        existing_days = _count_leave_days_for_month(
            session,
            employee.id,
            leave_type,
            target_month_start,
            target_month_end,
            exclude_request_id=exclude_request_id,
        )
        if existing_days + requested_days > 6:
            errors.append("排休總天數超過每月六天上限。")

        weekday_roster_days = [day for day in daterange(start_date, end_date) if day.weekday() != 6]
        if any(day <= requested_on for day in weekday_roster_days):
            errors.append("平日排休需提前申請，不可當天或事後補送。")

        if start_date.year == requested_on.year and start_date.month == requested_on.month and requested_on.day > 5:
            notes.append("已超過月初統一排假建議時間，請主管確認。")

        if any(day.weekday() == 6 for day in daterange(start_date, end_date)):
            notes.append("週日為原則休假日，若改出勤需另計加班或換休。")

    return LeavePolicyResult(errors=errors, notes=notes, conflicts=conflicts)


def annotate_leave_conflicts(
    session: Session,
    employee: Employee,
    leave_request: LeaveRequest,
) -> list[dict]:
    conflicts = []
    for member, assignment, worksite in find_leave_conflicts(
        session,
        employee.id,
        leave_request.start_date,
        leave_request.end_date,
    ):
        note = f"請假衝突：{employee.name} {leave_request.start_date:%Y-%m-%d} 至 {leave_request.end_date:%Y-%m-%d} 已核准請假，需改派。"
        member.note = note
        session.add(member)
        conflicts.append(
            {
                "assignment_id": assignment.id,
                "work_date": assignment.work_date.isoformat(),
                "site_name": worksite.name if worksite else "-",
                "work_item": assignment.work_item,
            }
        )
    session.commit()
    return conflicts


def get_latest_attendance_event(
    session: Session,
    employee_id: int,
    target_date: date,
) -> AttendanceEvent | None:
    start_at = datetime.combine(target_date, time.min)
    end_at = datetime.combine(target_date, time.max)
    return session.exec(
        select(AttendanceEvent)
        .where(
            AttendanceEvent.employee_id == employee_id,
            AttendanceEvent.happened_at >= start_at,
            AttendanceEvent.happened_at <= end_at,
        )
        .order_by(AttendanceEvent.happened_at.desc())
    ).first()


def find_assignment_for_employee(
    session: Session,
    employee_id: int,
    work_date: Optional[date] = None,
) -> WorkAssignment | None:
    target_date = work_date or date.today()
    return session.exec(
        select(WorkAssignment)
        .join(AssignmentMember, AssignmentMember.assignment_id == WorkAssignment.id)
        .where(
            AssignmentMember.employee_id == employee_id,
            AssignmentMember.is_active.is_(True),
            WorkAssignment.work_date == target_date,
        )
        .order_by(WorkAssignment.created_at.desc())
    ).first()


def find_assignment_member(
    session: Session,
    employee_id: int,
    assignment_id: Optional[int],
) -> AssignmentMember | None:
    statement = select(AssignmentMember).where(AssignmentMember.employee_id == employee_id)
    if assignment_id is not None:
        statement = statement.where(AssignmentMember.assignment_id == assignment_id)
    statement = statement.where(AssignmentMember.is_active.is_(True))
    return session.exec(statement.order_by(AssignmentMember.updated_at.desc())).first()


def _append_note(existing: Optional[str], new_note: str) -> str:
    if not existing:
        return new_note
    if new_note in existing:
        return existing
    return f"{existing}；{new_note}"


def record_attendance_event(
    session: Session,
    employee: Employee,
    command: str,
    note: Optional[str] = None,
) -> AttendanceRecordResult:
    assignment = find_assignment_for_employee(session, employee.id, date.today())
    member = find_assignment_member(session, employee.id, assignment.id if assignment else None)
    event = AttendanceEvent(
        employee_id=employee.id,
        site_id=assignment.site_id if assignment else employee.home_site_id,
        assignment_id=assignment.id if assignment else None,
        event_type=ATTENDANCE_COMMAND_MAP[command],
        note=note,
    )
    session.add(event)

    if member:
        member.last_line_action = command
        member.updated_at = datetime.utcnow()
        if command in ATTENDANCE_ACK_MAP:
            member.ack_status = ATTENDANCE_ACK_MAP[command]
        if note:
            member.note = _append_note(member.note, note)
        session.add(member)

    session.commit()
    session.refresh(event)

    anomalies: list[str] = []
    if assignment and assignment.work_date.weekday() == 6:
        anomalies.append("週日出勤，需另計加班或換休。")
    if not assignment:
        anomalies.append("今日尚未排定工作，已先記錄打卡事件。")

    return AttendanceRecordResult(event=event, assignment=assignment, anomalies=anomalies)


def get_covering_leave(
    session: Session,
    employee_id: int,
    target_date: date,
    statuses: Optional[set[LeaveStatus]] = None,
) -> LeaveRequest | None:
    statuses = statuses or {LeaveStatus.approved, LeaveStatus.pending}
    return session.exec(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_(list(statuses)),
            LeaveRequest.start_date <= target_date,
            LeaveRequest.end_date >= target_date,
        )
        .order_by(LeaveRequest.requested_at.desc())
    ).first()


def build_attendance_rows(
    session: Session,
    employees: list[Employee],
    target_date: date,
) -> list[dict]:
    rows: list[dict] = []
    now = datetime.now()

    for employee in employees:
        leave = get_covering_leave(session, employee.id, target_date, {LeaveStatus.approved, LeaveStatus.pending})
        assignment = find_assignment_for_employee(session, employee.id, target_date)
        member = find_assignment_member(session, employee.id, assignment.id if assignment else None) if assignment else None
        latest_event = get_latest_attendance_event(session, employee.id, target_date)

        if not assignment and not leave and not latest_event:
            continue

        site = session.get(Worksite, assignment.site_id) if assignment else None
        anomalies: list[str] = []
        summary_status = leave.status.value if leave else (member.ack_status.value if member else "pending")

        if leave:
            summary_status = f"請假-{leave.status.value}"
            if assignment:
                anomalies.append("請假衝突")
        if member and member.note and "請假衝突" in member.note:
            anomalies.append("請假衝突")
        if latest_event:
            summary_status = latest_event.event_type
        if assignment and assignment.work_date < now.date() and not latest_event and not leave:
            anomalies.append("未打卡")
        if assignment and assignment.work_date == now.date() and assignment.start_time:
            late_cutoff = datetime.combine(target_date, assignment.start_time) + timedelta(minutes=15)
            if latest_event and latest_event.event_type in {
                AttendanceEventType.check_in.value,
                AttendanceEventType.arrive_site.value,
            } and latest_event.happened_at > late_cutoff:
                anomalies.append("遲到")
            elif now > late_cutoff and not latest_event and not leave:
                anomalies.append("未打卡")
        if assignment and assignment.work_date.weekday() == 6 and latest_event:
            anomalies.append("週日出勤")

        rows.append(
            {
                "employee_code": employee.employee_code,
                "employee_name": employee.name,
                "work_date": target_date.isoformat(),
                "site_name": site.name if site else "-",
                "ack_status": summary_status,
                "last_action": latest_event.event_type if latest_event else (member.last_line_action if member else None),
                "last_event_at": latest_event.happened_at.isoformat() if latest_event else None,
                "note": member.note if member and member.note else (leave.policy_note if leave else None),
                "anomalies": sorted(set(anomalies)),
                "leave_status": leave.status.value if leave else None,
                "leave_type": leave.leave_type if leave else None,
            }
        )

    rows.sort(key=lambda row: (row["work_date"], row["employee_code"]))
    return rows


def list_employees_for_review(session: Session, actor: Employee) -> list[Employee]:
    statement = select(Employee).where(Employee.status == EmployeeStatus.active)
    if actor.role == Role.site_manager:
        statement = statement.where(Employee.home_site_id == actor.home_site_id)
    elif actor.role == Role.employee:
        statement = statement.where(Employee.id == actor.id)
    return session.exec(statement.order_by(Employee.employee_code)).all()


def list_assignment_members(session: Session, assignment_id: int) -> list[tuple[AssignmentMember, Employee]]:
    rows = session.exec(
        select(AssignmentMember, Employee)
        .join(Employee, Employee.id == AssignmentMember.employee_id)
        .where(AssignmentMember.assignment_id == assignment_id, AssignmentMember.is_active.is_(True))
        .order_by(Employee.employee_code)
    ).all()
    return rows


def _normalize_skill_tokens(*values: Optional[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        parts = re.split(r"[、,，/\s]+", value)
        for part in parts:
            part = part.strip().lower()
            if part:
                tokens.add(part)
    return tokens


def _candidate_is_busy(session: Session, employee_id: int, work_date: date, exclude_assignment_id: int) -> WorkAssignment | None:
    return session.exec(
        select(WorkAssignment)
        .join(AssignmentMember, AssignmentMember.assignment_id == WorkAssignment.id)
        .where(
            AssignmentMember.employee_id == employee_id,
            AssignmentMember.is_active.is_(True),
            WorkAssignment.work_date == work_date,
            WorkAssignment.id != exclude_assignment_id,
        )
        .order_by(WorkAssignment.created_at.desc())
    ).first()


def _candidate_leave(session: Session, employee_id: int, work_date: date) -> LeaveRequest | None:
    return get_covering_leave(session, employee_id, work_date, {LeaveStatus.pending, LeaveStatus.approved})


def suggest_reassignment_candidates(
    session: Session,
    assignment: WorkAssignment,
    absent_employee: Employee,
    limit: int = 5,
) -> list[dict]:
    existing_members = list_assignment_members(session, assignment.id)
    existing_member_ids = {employee.id for _, employee in existing_members}
    worksite = session.get(Worksite, assignment.site_id)
    required_tokens = _normalize_skill_tokens(assignment.equipment, assignment.work_item)

    candidates = session.exec(
        select(Employee).where(
            Employee.status == EmployeeStatus.active,
            Employee.role.in_([Role.employee, Role.site_manager]),
            Employee.id != absent_employee.id,
        )
    ).all()

    ranked: list[ReassignmentCandidate] = []
    for candidate in candidates:
        if candidate.id in existing_member_ids:
            continue

        leave_request = _candidate_leave(session, candidate.id, assignment.work_date)
        if leave_request:
            continue

        busy_assignment = _candidate_is_busy(session, candidate.id, assignment.work_date, assignment.id)
        if busy_assignment:
            continue

        score = 0
        reasons: list[str] = []

        if candidate.home_site_id == assignment.site_id:
            score += 40
            reasons.append("所屬工地相同")
        elif candidate.department == absent_employee.department:
            score += 18
            reasons.append("同部門支援")

        candidate_skill_tokens = {skill.strip().lower() for skill in candidate.machine_skills if skill.strip()}
        matched_skills = sorted(required_tokens & candidate_skill_tokens)
        if matched_skills:
            score += 35
            reasons.append(f"技能符合：{'、'.join(matched_skills)}")
        elif candidate.machine_skills:
            score += 8
            reasons.append("具備機具操作或現場技能")

        if candidate.role == Role.site_manager:
            score += 6
            reasons.append("可兼任現場調度")

        if not candidate.line_user_id:
            score -= 5
            reasons.append("尚未綁定 LINE")
        else:
            score += 5
            reasons.append("已綁定 LINE")

        if candidate.home_site_id == absent_employee.home_site_id:
            score += 10
            reasons.append("原同工地班底")

        ranked.append(ReassignmentCandidate(employee=candidate, score=score, reasons=reasons))

    ranked.sort(key=lambda item: (-item.score, item.employee.employee_code))
    return [
        {
            "employee_code": item.employee.employee_code,
            "name": item.employee.name,
            "role": item.employee.role.value,
            "department": item.employee.department,
            "home_site_name": session.get(Worksite, item.employee.home_site_id).name if item.employee.home_site_id else "-",
            "machine_skills": item.employee.machine_skills,
            "score": item.score,
            "reasons": item.reasons,
        }
        for item in ranked[:limit]
    ]


def build_leave_reassignment_suggestions(session: Session, leave_request: LeaveRequest) -> dict:
    absent_employee = session.get(Employee, leave_request.employee_id)
    if not absent_employee:
        return {"leave_request_id": leave_request.id, "assignments": []}

    suggestions = []
    for _, assignment, worksite in find_leave_conflicts(
        session,
        leave_request.employee_id,
        leave_request.start_date,
        leave_request.end_date,
    ):
        suggestions.append(
            {
                "assignment_id": assignment.id,
                "work_date": assignment.work_date.isoformat(),
                "site_name": worksite.name if worksite else "-",
                "work_item": assignment.work_item,
                "equipment": assignment.equipment,
                "vehicle": assignment.vehicle,
                "absent_employee_code": absent_employee.employee_code,
                "absent_employee_name": absent_employee.name,
                "candidates": suggest_reassignment_candidates(
                    session=session,
                    assignment=assignment,
                    absent_employee=absent_employee,
                ),
            }
        )

    return {
        "leave_request_id": leave_request.id,
        "employee_code": absent_employee.employee_code,
        "employee_name": absent_employee.name,
        "leave_type": leave_request.leave_type,
        "start_date": leave_request.start_date.isoformat(),
        "end_date": leave_request.end_date.isoformat(),
        "assignments": suggestions,
    }


def apply_reassignment_to_assignment(
    session: Session,
    assignment: WorkAssignment,
    absent_employee: Employee,
    replacement_employee: Employee,
    actor_name: str,
) -> dict:
    current_member = session.exec(
        select(AssignmentMember).where(
            AssignmentMember.assignment_id == assignment.id,
            AssignmentMember.employee_id == absent_employee.id,
            AssignmentMember.is_active.is_(True),
        )
    ).first()
    if not current_member:
        raise ValueError("找不到待改派的原始排班人員，可能已經處理過。")

    existing_replacement = session.exec(
        select(AssignmentMember).where(
            AssignmentMember.assignment_id == assignment.id,
            AssignmentMember.employee_id == replacement_employee.id,
            AssignmentMember.is_active.is_(True),
        )
    ).first()
    if existing_replacement:
        raise ValueError("候補人員已在這筆工作安排中。")

    if _candidate_leave(session, replacement_employee.id, assignment.work_date):
        raise ValueError("候補人員當天已有請假，不能直接套用。")

    if _candidate_is_busy(session, replacement_employee.id, assignment.work_date, assignment.id):
        raise ValueError("候補人員當天已有其他工作安排。")

    current_member.is_active = False
    current_member.note = _append_note(current_member.note, f"已由 {replacement_employee.name} 代班，操作人：{actor_name}")
    session.add(current_member)

    replacement_member = AssignmentMember(
        assignment_id=assignment.id,
        employee_id=replacement_employee.id,
        is_active=True,
        replacement_for_employee_id=absent_employee.id,
        ack_status=AckStatus.pending,
        note=f"代班 {absent_employee.name}",
    )
    session.add(replacement_member)
    session.commit()
    session.refresh(replacement_member)

    worksite = session.get(Worksite, assignment.site_id)
    return {
        "assignment_id": assignment.id,
        "work_date": assignment.work_date.isoformat(),
        "site_name": worksite.name if worksite else "-",
        "replacement_employee_code": replacement_employee.employee_code,
        "replacement_employee_name": replacement_employee.name,
        "absent_employee_code": absent_employee.employee_code,
        "absent_employee_name": absent_employee.name,
    }
