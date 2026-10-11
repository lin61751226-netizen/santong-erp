from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.deps import ensure_employee_scope, ensure_site_scope
from app.models import (
    AckStatus, AdminAuditLog, AssignmentMember, AttendanceEvent, Employee,
    EmployeeStatus, LeaveStatus, PhotoUploadLog, Role, WorkAssignment,
    WorkReportEvent, Worksite,
)
from app.schemas import AssignmentCreate, AssignmentUpdate
from app.services.hr import get_covering_leave


def assignment_snapshot(session: Session, assignment: WorkAssignment) -> dict:
    members = session.exec(select(AssignmentMember).where(
        AssignmentMember.assignment_id == assignment.id,
    ).order_by(AssignmentMember.id)).all()
    return {
        "assignment": assignment.model_dump(mode="json"),
        "members": [member.model_dump(mode="json") for member in members],
    }


def snapshot_version(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def serialize_assignment(session: Session, assignment: WorkAssignment) -> dict:
    snapshot = assignment_snapshot(session, assignment)
    site = session.get(Worksite, assignment.site_id)
    supervisor = session.get(Employee, assignment.supervisor_id) if assignment.supervisor_id else None
    members = [session.get(Employee, member["employee_id"]) for member in snapshot["members"] if member["is_active"]]
    return {
        **snapshot["assignment"],
        "site_name": site.name if site else "-",
        "supervisor_name": supervisor.name if supervisor else "-",
        "supervisor_code": supervisor.employee_code if supervisor else None,
        "members": [employee.name for employee in members if employee],
        "member_codes": [employee.employee_code for employee in members if employee],
        "version": snapshot_version(snapshot),
    }


def validate_assignment(session: Session, actor: Employee, payload: AssignmentCreate,
                        existing: WorkAssignment | None = None) -> tuple[list[Employee], int | None]:
    ensure_site_scope(actor, payload.site_id)
    site = session.get(Worksite, payload.site_id)
    if not site:
        raise HTTPException(404, "找不到工地")
    if not site.is_active and (not existing or existing.site_id != site.id):
        raise HTTPException(400, "此工地已停用，請選擇啟用工地")
    if not payload.work_item.strip() or len(payload.work_item) > 2000:
        raise HTTPException(400, "工作內容必須填寫，且不得超過 2000 字")
    if payload.notes and len(payload.notes) > 4000:
        raise HTTPException(400, "注意事項不得超過 4000 字")
    if any(value and len(value) > 500 for value in (payload.vehicle, payload.equipment)):
        raise HTTPException(400, "車輛及機具不得超過 500 字")
    if payload.start_time and payload.end_time and payload.end_time <= payload.start_time:
        raise HTTPException(400, "結束時間必須晚於開始時間；跨日工作請分日安排")
    if not payload.employee_codes:
        raise HTTPException(400, "請至少選擇一位執行人員")
    employees = session.exec(select(Employee).where(Employee.employee_code.in_(payload.employee_codes))).all()
    if len(employees) != len(set(payload.employee_codes)):
        raise HTTPException(404, "找不到部分員工代碼，請重新選擇人員")
    retained_ids = set(session.exec(select(AssignmentMember.employee_id).where(
        AssignmentMember.assignment_id == existing.id, AssignmentMember.is_active.is_(True),
    )).all()) if existing else set()
    for employee in employees:
        ensure_employee_scope(actor, employee)
        if employee.status != EmployeeStatus.active and employee.id not in retained_ids:
            raise HTTPException(400, f"{employee.name} 已停用，不能加入派工")
        if get_covering_leave(session, employee.id, payload.work_date, {LeaveStatus.approved}):
            raise HTTPException(400, f"{employee.name} 在 {payload.work_date} 已核准請假，不能排班。")
    supervisor_id = None
    if payload.supervisor_code:
        supervisor = session.exec(select(Employee).where(Employee.employee_code == payload.supervisor_code)).first()
        if not supervisor:
            raise HTTPException(404, "找不到主管代碼")
        ensure_employee_scope(actor, supervisor)
        if not existing or supervisor.id != existing.supervisor_id:
            if supervisor.status != EmployeeStatus.active or supervisor.role not in {Role.owner, Role.admin, Role.site_manager}:
                raise HTTPException(400, "請選擇啟用的主管帳號")
        supervisor_id = supervisor.id
    elif actor.role == Role.site_manager:
        supervisor_id = actor.id
    return employees, supervisor_id


def add_assignment_audit(session: Session, actor: Employee, assignment: WorkAssignment,
                         before: dict | None, after: dict) -> None:
    session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action="update" if before else "create", entity_type="work_assignment", entity_id=assignment.id,
        summary=json.dumps({"before": before, "after": after}, ensure_ascii=False)))


def update_assignment(session: Session, actor: Employee, assignment_id: int, payload: AssignmentUpdate) -> tuple[WorkAssignment, bool]:
    assignment = session.get(WorkAssignment, assignment_id)
    if not assignment:
        raise HTTPException(404, "找不到工作安排")
    ensure_site_scope(actor, assignment.site_id)
    try:
        # A no-op row update acquires a write lock on SQLite and PostgreSQL before checking the version.
        # Concurrent editors cannot both replace the same assignment; failures roll back this transaction.
        session.exec(update(WorkAssignment).where(WorkAssignment.id == assignment_id).values(id=assignment_id))
        session.refresh(assignment)
        ensure_site_scope(actor, assignment.site_id)
        before = assignment_snapshot(session, assignment)
        if payload.version != snapshot_version(before):
            raise HTTPException(409, "派工已被修改或收到新回報；請重新開啟最新設定後再保存。")
        employees, supervisor_id = validate_assignment(session, actor, payload, assignment)
        if payload.work_date != assignment.work_date or payload.site_id != assignment.site_id:
            history = any(session.exec(select(model.id).where(model.assignment_id == assignment.id).limit(1)).first()
                          for model in (AttendanceEvent, WorkReportEvent, PhotoUploadLog))
            if history or assignment.report_photos or assignment.is_completed or any(
                member["ack_status"] != AckStatus.pending.value or member["last_line_action"] or member["photo_url"]
                for member in before["members"]
            ):
                raise HTTPException(409, "此派工已有打卡或工作回報，不能變更日期／工地；請另建派工，原紀錄完整保留。")
        for key in ("work_date", "site_id", "work_item", "start_time", "end_time", "vehicle", "equipment", "notes"):
            value = getattr(payload, key)
            setattr(assignment, key, value.strip() if isinstance(value, str) else value)
        assignment.supervisor_id = supervisor_id
        selected_ids = {employee.id for employee in employees}
        existing_members = session.exec(select(AssignmentMember).where(
            AssignmentMember.assignment_id == assignment.id,
        ).order_by(AssignmentMember.id)).all()
        chosen: dict[int, AssignmentMember] = {}
        for member in existing_members:
            if member.employee_id not in chosen or member.is_active:
                chosen[member.employee_id] = member
        for member in existing_members:
            member.is_active = member.employee_id in selected_ids and chosen[member.employee_id].id == member.id
            session.add(member)
        for employee_id in selected_ids - chosen.keys():
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=employee_id))
        session.add(assignment)
        session.flush()
        after = assignment_snapshot(session, assignment)
        changed = before != after
        if changed:
            add_assignment_audit(session, actor, assignment, before, after)
        session.commit()
        return assignment, changed
    except OperationalError:
        session.rollback()
        raise HTTPException(409, "派工正在被另一位使用者修改，請重新查詢後再保存。")
    except Exception:
        session.rollback()
        raise
