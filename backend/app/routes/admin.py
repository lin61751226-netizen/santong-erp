from __future__ import annotations

import csv
import calendar
import secrets
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.core.db import get_session
from app.core.config import settings
from app.deps import ensure_employee_scope, ensure_site_scope, get_current_actor, require_roles
from app.models import (
    AssignmentMember,
    Employee,
    EmployeeStatus,
    Forklift,
    ForkliftInspection,
    ForkliftStatus,
    LeaveRequest,
    LeaveStatus,
    LoginLog,
    LoginStatus,
    MasterOption,
    MeetingRecord,
    NotificationCategory,
    NotificationBatch,
    NotificationDelivery,
    PhotoUploadLog,
    Role,
    WorkAssignment,
    Worksite,
)
from app.schemas import (
    AssignmentCreate,
    EmployeeCreate,
    EmployeeUpdate,
    MasterOptionCreate,
    ForkliftCareUpdate,
    ForkliftCreate,
    LeaveDecision,
    LeaveRequestCreate,
    LineRichMenuDeployRequest,
    LineWebhookConfigureRequest,
    MeetingCreate,
    NotificationCreate,
    ReassignmentApplyRequest,
    SimulateLineMessage,
)
from app.services.hr import (
    apply_reassignment_to_assignment,
    annotate_leave_conflicts,
    build_attendance_rows,
    build_leave_reassignment_suggestions,
    evaluate_leave_policy,
    find_leave_conflicts,
    format_policy_notes,
    get_covering_leave,
    list_employees_for_review,
)
from app.core.security import hash_password
from app.services.line import notify_employees, process_webhook_event
from app.services.forklift_service import INSPECTION_ITEMS, check_forklift_warnings, local_today
from app.services.forklift_notifications import (
    INSPECTION_SCOPE, WARNING_SCOPE, deliver_forklift_notifications, queue_vehicle_warning,
)


router = APIRouter(prefix="/api", tags=["admin"])

SITE_ALIASES = {
    "45": "45",
    "53": "齊裕53",
    "56": "56",
    "47": "善捷47",
    "善捷47": "善捷47",
    "金駿76": "金駿76",
    "桃園28": "桃園28",
    "桃園29": "桃園29",
    "齊裕53": "齊裕53",
    "新竹寶山1": "新竹寶山1",
    "新竹寶山2": "新竹寶山2",
    "新竹寶山3": "新竹寶山3",
}


def _employees_for_actor(session: Session, actor: Employee) -> list[Employee]:
    return list_employees_for_review(session, actor)


def _worksites_for_actor(session: Session, actor: Employee) -> list[Worksite]:
    statement = select(Worksite).where(Worksite.is_active.is_(True))
    if actor.role == Role.site_manager and actor.home_site_id:
        statement = statement.where(Worksite.id == actor.home_site_id)
    return session.exec(statement.order_by(Worksite.name)).all()


def _resolve_employee_codes(session: Session, employee_codes: list[str]) -> list[Employee]:
    employees = session.exec(select(Employee).where(Employee.employee_code.in_(employee_codes))).all()
    if len(employees) != len(set(employee_codes)):
        found_codes = {employee.employee_code for employee in employees}
        missing_codes = sorted(set(employee_codes) - found_codes)
        raise HTTPException(status_code=404, detail=f"找不到員工代碼：{', '.join(missing_codes)}")
    return employees


def _normalize_assigned_sites(site_names: list[str]) -> list[str]:
    normalized: list[str] = []
    for site_name in site_names:
        raw = str(site_name or "").strip()
        if not raw:
            continue
        canonical = SITE_ALIASES.get(raw, raw)
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _serialize_employee(worksites: dict[int, Worksite], item: Employee) -> dict:
    return {
        "employee_code": item.employee_code,
        "name": item.name,
        "role": item.role,
        "title": item.title,
        "department": item.department,
        "phone": item.phone,
        "email": item.email,
        "home_site_name": worksites.get(item.home_site_id).name if item.home_site_id in worksites else "-",
        "assigned_sites": item.assigned_sites,
        "line_bound": bool(item.line_user_id),
        "bind_token": item.bind_token,
    }


def _serialize_leave(session: Session, leave_request: LeaveRequest) -> dict:
    employee = session.get(Employee, leave_request.employee_id)
    conflict_rows = [
        {
            "assignment_id": assignment.id,
            "work_date": assignment.work_date.isoformat(),
            "site_name": worksite.name if worksite else "-",
            "work_item": assignment.work_item,
        }
        for _, assignment, worksite in find_leave_conflicts(
            session,
            leave_request.employee_id,
            leave_request.start_date,
            leave_request.end_date,
        )
    ]
    return {
        "id": leave_request.id,
        "employee_code": employee.employee_code if employee else "-",
        "employee_name": employee.name if employee else "-",
        "leave_type": leave_request.leave_type,
        "start_date": leave_request.start_date.isoformat(),
        "end_date": leave_request.end_date.isoformat(),
        "reason": leave_request.reason,
        "status": leave_request.status,
        "policy_note": leave_request.policy_note,
        "review_note": leave_request.review_note,
        "conflict_count": len(conflict_rows),
        "conflicts": conflict_rows,
    }


def _ensure_no_leave_conflict(session: Session, employee: Employee, target_date: date) -> None:
    conflict = get_covering_leave(session, employee.id, target_date, {LeaveStatus.approved})
    if conflict:
        raise HTTPException(
            status_code=400,
            detail=f"{employee.name} 在 {target_date:%Y-%m-%d} 已核准請假，不能排班。",
        )


def _filter_assignments_for_actor(session: Session, actor: Employee, target_date: Optional[date]) -> list[dict]:
    statement = select(WorkAssignment)
    if target_date:
        statement = statement.where(WorkAssignment.work_date == target_date)
    if actor.role == Role.site_manager:
        statement = statement.where(WorkAssignment.site_id == actor.home_site_id)
    if actor.role == Role.employee:
        statement = (
            statement.join(AssignmentMember, AssignmentMember.assignment_id == WorkAssignment.id)
            .where(AssignmentMember.employee_id == actor.id)
        )
    assignments = session.exec(statement.order_by(WorkAssignment.work_date.desc(), WorkAssignment.id.desc())).all()

    data = []
    for assignment in assignments:
        worksite = session.get(Worksite, assignment.site_id)
        supervisor = session.get(Employee, assignment.supervisor_id) if assignment.supervisor_id else None
        members = session.exec(
            select(AssignmentMember).where(
                AssignmentMember.assignment_id == assignment.id,
                AssignmentMember.is_active.is_(True),
            )
        ).all()
        employee_names = []
        employee_codes = []
        for member in members:
            employee = session.get(Employee, member.employee_id)
            if employee:
                employee_names.append(employee.name)
                employee_codes.append(employee.employee_code)
        data.append(
            {
                "id": assignment.id,
                "work_date": assignment.work_date.isoformat(),
                "site_name": worksite.name if worksite else "-",
                "work_item": assignment.work_item,
                "supervisor_name": supervisor.name if supervisor else "-",
                "start_time": assignment.start_time.isoformat() if assignment.start_time else None,
                "end_time": assignment.end_time.isoformat() if assignment.end_time else None,
                "notes": assignment.notes,
                "status": assignment.status,
                "members": employee_names,
                "member_codes": employee_codes,
            }
        )
    return data


def _recipients_from_scope(session: Session, actor: Employee, payload: NotificationCreate) -> list[Employee]:
    employees = session.exec(select(Employee).where(Employee.status == EmployeeStatus.active)).all()
    if actor.role == Role.site_manager:
        employees = [employee for employee in employees if employee.home_site_id == actor.home_site_id]

    if payload.target_scope == "all":
        return [employee for employee in employees if employee.role != Role.external]
    if payload.target_scope == "site":
        site_id = int(payload.target_value or "0")
        ensure_site_scope(actor, site_id)
        return [employee for employee in employees if employee.home_site_id == site_id]
    if payload.target_scope == "department":
        return [employee for employee in employees if employee.department == payload.target_value]
    if payload.target_scope == "employee":
        codes = [code.strip() for code in (payload.target_value or "").split(",") if code.strip()]
        return [employee for employee in employees if employee.employee_code in codes]
    if payload.target_scope == "management":
        return [employee for employee in employees if employee.role in {Role.owner, Role.admin, Role.site_manager}]
    if payload.target_scope == "supervisor":
        return [employee for employee in employees if employee.role == Role.site_manager]
    raise HTTPException(status_code=400, detail="不支援的發送範圍")


def _attendance_rows_for_actor(
    session: Session,
    actor: Employee,
    target_date: date,
    employee_code: Optional[str] = None,
) -> list[dict]:
    employees = _employees_for_actor(session, actor)
    if employee_code:
        employees = [employee for employee in employees if employee.employee_code == employee_code]
    return build_attendance_rows(session, employees, target_date)


def _active_option_labels(session: Session, option_type: str) -> list[str]:
    """取得某類別（work_item/equipment）目前啟用中的選項名稱，供派工表單點選。"""
    rows = session.exec(
        select(MasterOption).where(
            MasterOption.option_type == option_type,
            MasterOption.is_active.is_(True),
        ).order_by(MasterOption.sort_order, MasterOption.id)
    ).all()
    return [row.label for row in rows]


@router.get("/meta/options")
def get_options(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    employees = _employees_for_actor(session, actor)
    worksites = _worksites_for_actor(session, actor)
    return {
        "actor": {
            "employee_code": actor.employee_code,
            "name": actor.name,
            "role": actor.role,
        },
        "employees": [
            {
                "employee_code": item.employee_code,
                "name": item.name,
                "role": item.role,
                "title": item.title,
                "phone": item.phone,
                "email": item.email,
                "home_site_id": item.home_site_id,
                "assigned_sites": item.assigned_sites,
                "line_bound": bool(item.line_user_id),
            }
            for item in employees
        ],
        "worksites": [{"id": item.id, "name": item.name} for item in worksites],
        "work_item_options": _active_option_labels(session, "work_item"),
        "equipment_options": _active_option_labels(session, "equipment"),
        "forklift_options": [
            {
                "id": item.id,
                "code": item.forklift_code,
                "model": item.model,
                "label": f"{item.forklift_code}（{item.model}）" if item.model else item.forklift_code,
            }
            for item in session.exec(
                select(Forklift)
                .where(Forklift.status != ForkliftStatus.inactive)
                .order_by(Forklift.forklift_code)
            ).all()
        ],
        "roles": [role.value for role in Role],
        "leave_statuses": [status_item.value for status_item in LeaveStatus],
        "leave_types": ["事假", "病假", "特休", "公假", "排休", "其他"],
    }


@router.get("/master-options")
def list_master_options(
    option_type: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    statement = select(MasterOption)
    if option_type:
        statement = statement.where(MasterOption.option_type == option_type)
    rows = session.exec(
        statement.order_by(MasterOption.option_type, MasterOption.sort_order, MasterOption.id)
    ).all()
    return [
        {
            "id": row.id,
            "option_type": row.option_type,
            "label": row.label,
            "sort_order": row.sort_order,
            "is_active": row.is_active,
        }
        for row in rows
    ]


@router.post("/master-options", status_code=status.HTTP_201_CREATED)
def create_master_option(
    payload: MasterOptionCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    option_type = payload.option_type.strip()
    label = payload.label.strip()
    if option_type not in {"work_item", "equipment"}:
        raise HTTPException(status_code=400, detail="選項類型僅支援 work_item（工作內容）或 equipment（機具）")
    if not label:
        raise HTTPException(status_code=400, detail="選項名稱不可為空")
    existing = session.exec(
        select(MasterOption).where(
            MasterOption.option_type == option_type,
            MasterOption.label == label,
        )
    ).first()
    if existing:
        existing.is_active = True
        session.add(existing)
        session.commit()
        return {"message": "選項已存在，已重新啟用", "id": existing.id}
    same_type_rows = session.exec(
        select(MasterOption).where(MasterOption.option_type == option_type)
    ).all()
    option = MasterOption(
        option_type=option_type,
        label=label,
        sort_order=len(same_type_rows),
        is_active=True,
    )
    session.add(option)
    session.commit()
    session.refresh(option)
    return {"message": "選項已新增", "id": option.id}


@router.delete("/master-options/{option_id}")
def deactivate_master_option(
    option_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    option = session.get(MasterOption, option_id)
    if not option:
        raise HTTPException(status_code=404, detail="找不到選項")
    option.is_active = False
    session.add(option)
    session.commit()
    return {"message": "選項已停用"}


@router.get("/dashboard")
def dashboard(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    employees = _employees_for_actor(session, actor)
    worksites = _worksites_for_actor(session, actor)
    pending_leaves_stmt = select(LeaveRequest).where(LeaveRequest.status == LeaveStatus.pending)
    assignments_stmt = select(WorkAssignment).where(WorkAssignment.work_date == date.today())

    if actor.role == Role.site_manager:
        pending_leaves_stmt = (
            pending_leaves_stmt.join(Employee, Employee.id == LeaveRequest.employee_id)
            .where(Employee.home_site_id == actor.home_site_id)
        )
        assignments_stmt = assignments_stmt.where(WorkAssignment.site_id == actor.home_site_id)
    elif actor.role == Role.employee:
        pending_leaves_stmt = pending_leaves_stmt.where(LeaveRequest.employee_id == actor.id)
        assignments_stmt = (
            assignments_stmt.join(AssignmentMember, AssignmentMember.assignment_id == WorkAssignment.id)
            .where(AssignmentMember.employee_id == actor.id)
        )

    attendance_exceptions = [
        row for row in _attendance_rows_for_actor(session, actor, date.today()) if row["anomalies"]
    ]

    return {
        "app_name": "三通工程自動化管理系統",
        "today": date.today().isoformat(),
        "employee_count": len(employees),
        "worksite_count": len(worksites),
        "today_assignment_count": len(session.exec(assignments_stmt).all()),
        "pending_leave_count": len(session.exec(pending_leaves_stmt).all()),
        "unbound_employee_count": len([item for item in employees if not item.line_user_id]),
        "attendance_exception_count": len(attendance_exceptions),
    }


@router.get("/employees")
def list_employees(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    employees = _employees_for_actor(session, actor)
    worksites = {item.id: item for item in session.exec(select(Worksite)).all()}
    return [_serialize_employee(worksites, item) for item in employees]


@router.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(
    payload: EmployeeCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    code = payload.employee_code.strip()
    name = payload.name.strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="員工代碼與姓名不可空白")
    if session.exec(select(Employee).where(Employee.employee_code == code)).first():
        raise HTTPException(status_code=409, detail="員工代碼已存在")
    normalized_sites = _normalize_assigned_sites(payload.assigned_sites)
    worksites = {item.name: item for item in session.exec(select(Worksite)).all()}
    missing_sites = [site_name for site_name in normalized_sites if site_name not in worksites]
    if missing_sites:
        raise HTTPException(status_code=400, detail=f"找不到工地：{', '.join(missing_sites)}")
    employee = Employee(
        employee_code=code,
        name=name,
        role=payload.role,
        title=(payload.title or "").strip() or None,
        phone=(payload.phone or "").strip() or None,
        email=(payload.email or "").strip() or None,
        assigned_sites=normalized_sites,
        home_site_id=worksites[normalized_sites[0]].id if normalized_sites else None,
        bind_token=secrets.token_urlsafe(8),
    )
    if employee.role in {Role.owner, Role.admin}:
        employee.password_hash = hash_password(settings.default_password)
        employee.must_change_password = True
    session.add(employee)
    session.commit()
    session.refresh(employee)
    worksites_by_id = {item.id: item for item in session.exec(select(Worksite)).all()}
    return {"message": "員工已新增", "employee": _serialize_employee(worksites_by_id, employee)}


@router.put("/employees/{employee_code}")
def update_employee(
    employee_code: str,
    payload: EmployeeUpdate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    employee = session.exec(select(Employee).where(Employee.employee_code == employee_code)).first()
    if not employee:
        raise HTTPException(status_code=404, detail="找不到員工代碼")

    ensure_employee_scope(actor, employee)

    normalized_sites = _normalize_assigned_sites(payload.assigned_sites)
    worksites = {item.name: item for item in session.exec(select(Worksite)).all()}
    missing_sites = [site_name for site_name in normalized_sites if site_name not in worksites]
    if missing_sites:
        raise HTTPException(status_code=400, detail=f"找不到工地：{', '.join(missing_sites)}")

    employee.title = (payload.title or "").strip() or None
    employee.phone = (payload.phone or "").strip() or None
    employee.email = (payload.email or "").strip() or None
    employee.assigned_sites = normalized_sites
    employee.home_site_id = worksites[normalized_sites[0]].id if normalized_sites else None
    session.add(employee)
    session.commit()
    session.refresh(employee)

    worksites_by_id = {item.id: item for item in session.exec(select(Worksite)).all()}
    return {
        "message": "員工資料已更新",
        "employee": _serialize_employee(worksites_by_id, employee),
    }


@router.get("/assignments")
def list_assignments(
    target_date: Optional[date] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    return _filter_assignments_for_actor(session, actor, target_date)


@router.post("/assignments", status_code=status.HTTP_201_CREATED)
def create_assignment(
    payload: AssignmentCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    ensure_site_scope(actor, payload.site_id)
    employees = _resolve_employee_codes(session, payload.employee_codes)
    for employee in employees:
        ensure_employee_scope(actor, employee)
        _ensure_no_leave_conflict(session, employee, payload.work_date)

    supervisor_id = None
    if payload.supervisor_code:
        supervisor = session.exec(select(Employee).where(Employee.employee_code == payload.supervisor_code)).first()
        if not supervisor:
            raise HTTPException(status_code=404, detail="找不到主管代碼")
        ensure_employee_scope(actor, supervisor)
        supervisor_id = supervisor.id
    elif actor.role == Role.site_manager:
        supervisor_id = actor.id

    assignment = WorkAssignment(
        work_date=payload.work_date,
        site_id=payload.site_id,
        work_item=payload.work_item,
        supervisor_id=supervisor_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        vehicle=payload.vehicle,
        equipment=payload.equipment,
        notes=payload.notes,
        created_by=actor.id,
    )
    session.add(assignment)
    session.commit()
    session.refresh(assignment)

    for employee in employees:
        session.add(AssignmentMember(assignment_id=assignment.id, employee_id=employee.id))
    session.commit()
    return {"message": "工作安排已建立", "assignment_id": assignment.id}


@router.post("/notifications/send")
async def send_notification(
    payload: NotificationCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    recipients = _recipients_from_scope(session, actor, payload)
    if not recipients:
        raise HTTPException(status_code=400, detail="找不到符合條件的通知對象")
    result = await notify_employees(
        session=session,
        sender=actor,
        employees=recipients,
        category=payload.category,
        target_scope=payload.target_scope,
        target_value=payload.target_value,
        content=payload.content,
        assignment_id=payload.assignment_id,
        meeting_id=payload.meeting_id,
    )
    return {"message": "通知處理完成", **result}


@router.get("/leave-requests")
def list_leave_requests(
    status_filter: str = Query(default="all"),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    statement = select(LeaveRequest)
    if actor.role == Role.site_manager:
        statement = (
            statement.join(Employee, Employee.id == LeaveRequest.employee_id)
            .where(Employee.home_site_id == actor.home_site_id)
        )
    elif actor.role == Role.employee:
        statement = statement.where(LeaveRequest.employee_id == actor.id)
    if status_filter != "all":
        statement = statement.where(LeaveRequest.status == status_filter)
    leaves = session.exec(statement.order_by(LeaveRequest.requested_at.desc())).all()
    return [_serialize_leave(session, item) for item in leaves]


@router.post("/leave-requests", status_code=status.HTTP_201_CREATED)
def create_leave_request(
    payload: LeaveRequestCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="請假結束日期不得早於開始日期")

    employee = actor
    if payload.employee_code and payload.employee_code != actor.employee_code:
        employee = session.exec(select(Employee).where(Employee.employee_code == payload.employee_code)).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到員工代碼")
        ensure_employee_scope(actor, employee)

    policy = evaluate_leave_policy(
        session=session,
        employee=employee,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    if policy.errors:
        raise HTTPException(status_code=400, detail="；".join(policy.errors))

    leave_request = LeaveRequest(
        employee_id=employee.id,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        reason=payload.reason,
        policy_note=format_policy_notes(policy.notes),
    )
    session.add(leave_request)
    session.commit()
    session.refresh(leave_request)

    return {
        "message": "請假申請已建立",
        "leave_request_id": leave_request.id,
        "policy_note": leave_request.policy_note,
        "conflict_count": len(policy.conflicts),
    }


@router.post("/leave-requests/{leave_request_id}/decision")
async def decide_leave_request(
    leave_request_id: int,
    payload: LeaveDecision,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    leave_request = session.get(LeaveRequest, leave_request_id)
    if not leave_request:
        raise HTTPException(status_code=404, detail="找不到請假申請")

    employee = session.get(Employee, leave_request.employee_id)
    ensure_employee_scope(actor, employee)

    conflict_rows: list[dict] = []
    if payload.status == LeaveStatus.approved:
        policy = evaluate_leave_policy(
            session=session,
            employee=employee,
            leave_type=leave_request.leave_type,
            start_date=leave_request.start_date,
            end_date=leave_request.end_date,
            requested_on=leave_request.requested_at.date(),
            exclude_request_id=leave_request.id,
        )
        if policy.errors and "同時段已有待審或已核准的請假申請。" not in policy.errors:
            raise HTTPException(status_code=400, detail="；".join(policy.errors))

    leave_request.status = payload.status
    leave_request.review_note = payload.review_note
    leave_request.approver_id = actor.id
    leave_request.reviewed_at = datetime.utcnow()
    session.add(leave_request)
    session.commit()

    if payload.status == LeaveStatus.approved:
        conflict_rows = annotate_leave_conflicts(session, employee, leave_request)

    notice_text = (
        f"【請假審核結果】\n"
        f"員工：{employee.name}\n"
        f"假別：{leave_request.leave_type}\n"
        f"日期：{leave_request.start_date:%Y/%m/%d} - {leave_request.end_date:%Y/%m/%d}\n"
        f"結果：{'已核准' if payload.status == LeaveStatus.approved else '已退回'}\n"
        f"說明：{payload.review_note or '無'}"
    )
    await notify_employees(
        session=session,
        sender=actor,
        employees=[employee],
        category=NotificationCategory.leave,
        target_scope="employee",
        target_value=employee.employee_code,
        content=notice_text,
    )

    return {
        "message": "請假審核已更新",
        "status": leave_request.status,
        "conflict_count": len(conflict_rows),
        "conflicts": conflict_rows,
    }


@router.get("/leave-requests/{leave_request_id}/reassignment-suggestions")
def get_leave_reassignment_suggestions(
    leave_request_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    leave_request = session.get(LeaveRequest, leave_request_id)
    if not leave_request:
        raise HTTPException(status_code=404, detail="找不到請假申請")
    employee = session.get(Employee, leave_request.employee_id)
    ensure_employee_scope(actor, employee)
    return build_leave_reassignment_suggestions(session, leave_request)


@router.post("/assignments/{assignment_id}/apply-reassignment")
async def apply_reassignment(
    assignment_id: int,
    payload: ReassignmentApplyRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    assignment = session.get(WorkAssignment, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="找不到工作安排")
    ensure_site_scope(actor, assignment.site_id)

    absent_employee = session.exec(select(Employee).where(Employee.employee_code == payload.absent_employee_code)).first()
    replacement_employee = session.exec(
        select(Employee).where(Employee.employee_code == payload.replacement_employee_code)
    ).first()
    if not absent_employee or not replacement_employee:
        raise HTTPException(status_code=404, detail="找不到原人員或候補人員")
    ensure_employee_scope(actor, absent_employee)
    ensure_employee_scope(actor, replacement_employee)

    try:
        result = apply_reassignment_to_assignment(
            session=session,
            assignment=assignment,
            absent_employee=absent_employee,
            replacement_employee=replacement_employee,
            actor_name=actor.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.notify_replacement:
        worksite = session.get(Worksite, assignment.site_id)
        supervisor = session.get(Employee, assignment.supervisor_id) if assignment.supervisor_id else None
        message = (
            f"【代班通知】\n"
            f"日期：{assignment.work_date:%Y/%m/%d}\n"
            f"工地：{worksite.name if worksite else '-'}\n"
            f"工作內容：{assignment.work_item}\n"
            f"原人員：{absent_employee.name}\n"
            f"改派：{replacement_employee.name}\n"
            f"主管：{supervisor.name if supervisor else '-'}"
        )
        await notify_employees(
            session=session,
            sender=actor,
            employees=[replacement_employee],
            category=NotificationCategory.daily_schedule,
            target_scope="employee",
            target_value=replacement_employee.employee_code,
            content=message,
            assignment_id=assignment.id,
        )

    return {"message": "候補人員已套用到工作安排", **result}


@router.get("/attendance")
def list_attendance(
    target_date: Optional[date] = Query(default=None),
    employee_code: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    actual_date = target_date or date.today()
    return _attendance_rows_for_actor(session, actor, actual_date, employee_code)


@router.get("/attendance/exceptions")
def list_attendance_exceptions(
    target_date: Optional[date] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rows = _attendance_rows_for_actor(session, actor, target_date or date.today())
    return [row for row in rows if row["anomalies"]]


@router.get("/me/schedule")
def my_schedule(
    target_date: Optional[date] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    return _filter_assignments_for_actor(session, actor, target_date or date.today())


@router.get("/me/attendance")
def my_attendance(
    target_date: Optional[date] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    return _attendance_rows_for_actor(session, actor, target_date or date.today(), actor.employee_code)


@router.get("/me/leaves")
def my_leaves(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    leaves = session.exec(
        select(LeaveRequest).where(LeaveRequest.employee_id == actor.id).order_by(LeaveRequest.requested_at.desc())
    ).all()
    return [_serialize_leave(session, leave_item) for leave_item in leaves]


@router.post("/meetings", status_code=status.HTTP_201_CREATED)
async def create_meeting(
    payload: MeetingCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    owner_id = actor.id
    if payload.owner_code:
        owner = session.exec(select(Employee).where(Employee.employee_code == payload.owner_code)).first()
        if not owner:
            raise HTTPException(status_code=404, detail="找不到會議負責人")
        ensure_employee_scope(actor, owner)
        owner_id = owner.id

    meeting = MeetingRecord(
        title=payload.title,
        meeting_at=payload.meeting_at,
        location=payload.location,
        attendee_codes=payload.attendee_codes,
        agenda=payload.agenda,
        decisions=payload.decisions,
        owner_id=owner_id,
        due_date=payload.due_date,
        status=payload.status,
        summary=payload.summary,
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    if payload.send_summary and payload.attendee_codes:
        recipients = _resolve_employee_codes(session, payload.attendee_codes)
        owner = session.get(Employee, owner_id)
        if meeting.due_date:
            summary_text = (
                f"【會議決議通知】\n"
                f"會議：{meeting.title}\n"
                f"時間：{meeting.meeting_at:%Y/%m/%d %H:%M}\n"
                f"事項：{meeting.decisions}\n"
                f"負責人：{owner.name if owner else '-'}\n"
                f"完成期限：{meeting.due_date:%Y/%m/%d}"
            )
        else:
            summary_text = (
                f"【會議決議通知】\n"
                f"會議：{meeting.title}\n"
                f"事項：{meeting.decisions}\n"
                f"負責人：{owner.name if owner else '-'}"
            )
        await notify_employees(
            session=session,
            sender=actor,
            employees=recipients,
            category=NotificationCategory.meeting,
            target_scope="employee",
            target_value=",".join(payload.attendee_codes),
            content=summary_text,
            meeting_id=meeting.id,
        )

    return {"message": "會議記錄已建立", "meeting_id": meeting.id}


@router.post("/simulate/line-message")
async def simulate_line_message(
    payload: SimulateLineMessage,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    event = {
        "type": "message",
        "replyToken": "demo-reply-token",
        "source": {"type": "user", "userId": payload.line_user_id},
        "message": {"type": "text", "text": payload.message},
    }
    await process_webhook_event(session, event)
    return {"message": "模擬訊息已送入 webhook 流程"}


@router.get("/login-logs")
def list_login_logs(
    employee_code: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    statement = select(LoginLog)
    if employee_code:
        statement = statement.where(LoginLog.employee_code == employee_code)
    if status_filter:
        statement = statement.where(LoginLog.status == status_filter)
    logs = session.exec(statement.order_by(LoginLog.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": log.id,
            "employee_code": log.employee_code,
            "employee_name": log.employee_name,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "status": log.status,
            "failure_reason": log.failure_reason,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


@router.put("/employees/{employee_code}/status")
def update_employee_status(
    employee_code: str,
    payload: dict,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    employee = session.exec(select(Employee).where(Employee.employee_code == employee_code)).first()
    if not employee:
        raise HTTPException(status_code=404, detail="找不到員工代碼")
    ensure_employee_scope(actor, employee)
    new_status = payload.get("status")
    if new_status not in [EmployeeStatus.active.value, EmployeeStatus.inactive.value]:
        raise HTTPException(status_code=400, detail="狀態必須是 active 或 inactive")
    employee.status = new_status
    session.add(employee)
    session.commit()
    session.refresh(employee)
    return {
        "message": f"員工狀態已更新為 {new_status}",
        "employee_code": employee.employee_code,
        "status": employee.status,
    }


@router.post("/employees/{employee_code}/reset-password")
def reset_employee_password(
    employee_code: str,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    employee = session.exec(select(Employee).where(Employee.employee_code == employee_code)).first()
    if not employee:
        raise HTTPException(status_code=404, detail="找不到員工代碼")
    ensure_employee_scope(actor, employee)
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    new_password = "".join(secrets.choice(alphabet) for _ in range(12))
    employee.password_hash = hash_password(new_password)
    employee.must_change_password = True
    employee.failed_login_count = 0
    employee.locked_until = None
    session.add(employee)
    session.commit()
    return {
        "message": "密碼已重設，請將新密碼告知員工",
        "employee_code": employee.employee_code,
        "new_password": new_password,
        "must_change_password": True,
    }


@router.get("/photo-uploads")
def list_photo_uploads(
    employee_code: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    statement = select(PhotoUploadLog)
    if employee_code:
        employee = session.exec(select(Employee).where(Employee.employee_code == employee_code)).first()
        if employee:
            statement = statement.where(PhotoUploadLog.employee_id == employee.id)
    if date_from:
        statement = statement.where(PhotoUploadLog.uploaded_at >= date_from)
    if date_to:
        statement = statement.where(PhotoUploadLog.uploaded_at < date_to + timedelta(days=1))
    if actor.role == Role.site_manager and actor.home_site_id:
        statement = statement.where(PhotoUploadLog.site_id == actor.home_site_id)
    elif actor.role == Role.employee:
        statement = statement.where(PhotoUploadLog.employee_id == actor.id)
    logs = session.exec(statement.order_by(PhotoUploadLog.uploaded_at.desc()).limit(limit)).all()
    result = []
    for log in logs:
        employee = session.get(Employee, log.employee_id) if log.employee_id else None
        worksite = session.get(Worksite, log.site_id) if log.site_id else None
        result.append({
            "id": log.id,
            "employee_code": employee.employee_code if employee else None,
            "employee_name": employee.name if employee else None,
            "site_name": worksite.name if worksite else None,
            "file_name": log.file_name,
            "drive_url": log.drive_url,
            "uploaded_at": log.uploaded_at.isoformat() if log.uploaded_at else None,
            "note": log.note,
        })
    return result



@router.get("/forklifts")
def list_forklifts(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    forklifts = session.exec(select(Forklift).order_by(Forklift.forklift_code)).all()
    result = []
    for f in forklifts:
        site = session.get(Worksite, f.current_site_id) if f.current_site_id else None
        operator = session.get(Employee, f.current_operator_id) if f.current_operator_id else None
        result.append({
            "id": f.id,
            "forklift_code": f.forklift_code,
            "model": f.model,
            "status": f.status,
            "site_name": site.name if site else None,
            "operator_name": operator.name if operator else None,
            "fuel_level": f.fuel_level,
            "next_maintenance_date": f.next_maintenance_date.isoformat() if f.next_maintenance_date else None,
            "warnings": check_forklift_warnings(session, f.id),
        })
    return result


@router.post("/forklifts", status_code=status.HTTP_201_CREATED)
def create_forklift(
    payload: ForkliftCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    code = payload.forklift_code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="堆高機編號不可空白")
    if session.exec(select(Forklift).where(Forklift.forklift_code == code)).first():
        raise HTTPException(status_code=409, detail="堆高機編號已存在")
    if payload.site_id is not None:
        site = session.get(Worksite, payload.site_id)
        if not site or not site.is_active:
            raise HTTPException(status_code=400, detail="目前工地不存在或已停用")
        ensure_site_scope(actor, payload.site_id)
    forklift = Forklift(
        forklift_code=code,
        model=(payload.model or "").strip() or None,
        current_site_id=payload.site_id,
        fuel_level=payload.fuel_level,
        next_maintenance_date=payload.next_maintenance_date,
    )
    session.add(forklift)
    session.commit()
    session.refresh(forklift)
    return {"message": "堆高機已新增", "id": forklift.id, "forklift_code": forklift.forklift_code}


def _inspection_range(date_filter, start_date, end_date, month):
    if month:
        if date_filter or start_date or end_date:
            raise HTTPException(status_code=422, detail="月份與日期區間請擇一查詢。")
        try:
            year, number = map(int, month.split("-"))
            start_date = date(year, number, 1)
            end_date = date(year, number, calendar.monthrange(year, number)[1])
        except ValueError:
            raise HTTPException(status_code=422, detail="月份格式必須為 YYYY-MM。")
    if date_filter:
        start_date = end_date = date_filter
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="開始日期不可晚於結束日期。")
    return start_date, end_date


@router.get("/forklift-inspections")
def list_forklift_inspections(
    date_filter: Optional[date] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    site_id: Optional[int] = Query(default=None),
    forklift_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    statement = select(ForkliftInspection)
    start_date, end_date = _inspection_range(date_filter, start_date, end_date, month)
    if start_date:
        statement = statement.where(ForkliftInspection.inspection_date >= start_date)
    if end_date:
        statement = statement.where(ForkliftInspection.inspection_date <= end_date)
    if site_id:
        statement = statement.where(ForkliftInspection.site_id == site_id)
    if forklift_id:
        statement = statement.where(ForkliftInspection.forklift_id == forklift_id)
    if actor.role == Role.site_manager and actor.home_site_id:
        statement = statement.where(ForkliftInspection.site_id == actor.home_site_id)
    inspections = session.exec(
        statement.order_by(ForkliftInspection.inspection_date.desc(), ForkliftInspection.id.desc()).limit(limit)
    ).all()
    result = []
    for insp in inspections:
        forklift = session.get(Forklift, insp.forklift_id)
        operator = session.get(Employee, insp.operator_id)
        site = session.get(Worksite, insp.site_id) if insp.site_id else None
        result.append({
            "id": insp.id,
            "forklift_code": forklift.forklift_code if forklift else None,
            "forklift_model": forklift.model if forklift else None,
            "operator_name": operator.name if operator else None,
            "site_name": site.name if site else None,
            "inspection_date": insp.inspection_date.isoformat(),
            "all_passed": insp.all_passed,
            "inspection_items": insp.inspection_items or {},
            "abnormal_items": [item["label"] for item in INSPECTION_ITEMS
                               if (insp.inspection_items or {}).get(item["key"]) is False],
            "notes": insp.notes,
            "created_at": insp.created_at.isoformat() if insp.created_at else None,
        })
    return result



@router.get("/forklift-inspections/export")
def export_forklift_inspections_csv(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    site_id: Optional[int] = Query(default=None),
    forklift_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    start_date, end_date = _inspection_range(None, start_date, end_date, month)
    statement = select(ForkliftInspection)
    if start_date:
        statement = statement.where(ForkliftInspection.inspection_date >= start_date)
    if end_date:
        statement = statement.where(ForkliftInspection.inspection_date <= end_date)
    if site_id:
        statement = statement.where(ForkliftInspection.site_id == site_id)
    if forklift_id:
        statement = statement.where(ForkliftInspection.forklift_id == forklift_id)
    if actor.role == Role.site_manager and actor.home_site_id:
        statement = statement.where(ForkliftInspection.site_id == actor.home_site_id)

    inspections = session.exec(
        statement.order_by(ForkliftInspection.inspection_date.desc(), ForkliftInspection.id.desc())
    ).all()

    output = StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "點檢日期", "堆高機編號", "堆高機型號", "操作員", "工地",
        "全部通過", "引擎機油", "冷卻水", "電瓶液", "輪胎磨損",
        "喇叭", "燈光", "剎車系統", "油壓系統", "貨叉與鏈條", "安全帶與後照鏡",
        "備註",
    ])

    item_keys = [
        "engine_oil", "coolant", "battery", "tires", "horn",
        "lights", "brakes", "hydraulic", "fork_chain", "safety_belt",
    ]

    for insp in inspections:
        forklift = session.get(Forklift, insp.forklift_id)
        operator = session.get(Employee, insp.operator_id)
        site = session.get(Worksite, insp.site_id) if insp.site_id else None
        row = [
            insp.inspection_date.isoformat(),
            forklift.forklift_code if forklift else "",
            forklift.model if forklift else "",
            operator.name if operator else "",
            site.name if site else "",
            "是" if insp.all_passed else "否",
        ]
        for key in item_keys:
            result = (insp.inspection_items or {}).get(key)
            row.append("正常" if result is True else "異常" if result is False else "未記錄")
        row.append(insp.notes or "")
        # Spreadsheet programs must treat free-text notes as text, not formulas.
        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(
            ("=", "+", "-", "@", "\t", "\r", "\n")
        ) else value for value in row])

    output.seek(0)
    period = month or f"{start_date or 'all'}_{end_date or local_today()}"
    filename = f"forklift_inspections_{period}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.put("/forklifts/{forklift_id}/status")
def update_forklift_status(
    forklift_id: int,
    status: str = Query(..., description="堆高機狀態：operating/available/maintenance/inactive"),
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles("owner", "admin", "site_manager")),
):
    forklift = session.get(Forklift, forklift_id)
    if not forklift:
        raise HTTPException(status_code=404, detail="找不到堆高機")
    valid_statuses = [s.value for s in ForkliftStatus]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"狀態無效，有效值：{', '.join(valid_statuses)}")
    forklift.status = status
    session.add(forklift)
    session.commit()
    session.refresh(forklift)
    return {
        "id": forklift.id,
        "forklift_code": forklift.forklift_code,
        "status": forklift.status,
        "message": "狀態已更新",
    }


@router.put("/forklifts/{forklift_id}/care")
async def update_forklift_care(
    forklift_id: int,
    payload: ForkliftCareUpdate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    forklift = session.get(Forklift, forklift_id)
    if not forklift:
        raise HTTPException(status_code=404, detail="找不到堆高機")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(forklift, key, value)
    forklift.updated_at = datetime.utcnow()
    session.add(forklift)
    session.commit()
    queue_vehicle_warning(session, forklift)
    await deliver_forklift_notifications(session)
    return {"message": "油量與保養日期已儲存", "warnings": check_forklift_warnings(session, forklift.id)}


@router.get("/forklift-notifications")
def list_forklift_notifications(
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    rows = session.exec(select(NotificationDelivery, NotificationBatch).join(
        NotificationBatch, NotificationDelivery.batch_id == NotificationBatch.id,
    ).where(NotificationBatch.target_scope.in_([INSPECTION_SCOPE, WARNING_SCOPE]))
        .order_by(NotificationDelivery.id.desc()).limit(50)).all()
    return [{"recipient": (session.get(Employee, delivery.employee_id).name
                           if delivery.employee_id and session.get(Employee, delivery.employee_id) else "-"),
             "status": delivery.delivery_status, "detail": delivery.delivery_message,
             "content": batch.content, "sent_at": delivery.sent_at.isoformat()}
            for delivery, batch in rows]
