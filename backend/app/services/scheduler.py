from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.core.config import settings
from app.core.db import session_scope
from app.models import (
    AssignmentMember,
    AttendanceEvent,
    AttendanceEventType,
    Employee,
    EmployeeStatus,
    ForkliftInspection,
    NotificationBatch,
    NotificationCategory,
    WorkAssignment,
    Worksite,
)
from app.services.line import _build_schedule_summary, line_service, notify_employees
from app.services.forklift_notifications import deliver_forklift_notifications, process_forklift_alerts, queue_inspection_reminders
from app.services.forklift_service import local_today


scheduler = AsyncIOScheduler(timezone=settings.timezone)


# 僅以員工代碼指定，姓名調整或 LINE 重新綁定時不會影響每日通知對象。
ATTENDANCE_SUMMARY_RECIPIENT_CODES = ("ADMIN002", "ADMIN001", "BOSS001")
ATTENDANCE_SUMMARY_TARGET_SCOPE = "daily_attendance_summary"


def _local_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    """將台灣曆日轉成資料庫採用的 UTC-naive 查詢範圍。"""
    local_tz = ZoneInfo(settings.timezone)
    start = datetime.combine(target_date, time.min, tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = (datetime.combine(target_date, time.min, tzinfo=local_tz) + timedelta(days=1)) \
        .astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def _format_local_time(value: datetime) -> str:
    """打卡時間以台灣時間顯示；歷史 UTC-naive 資料也會正確轉換。"""
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")


def _build_daily_attendance_summary(session, target_date: date) -> str:
    """整理當日曾上下班打卡的人員；未完整者一併列出供管理人員追查。"""
    start_at, end_at = _local_day_bounds(target_date)
    events = session.exec(
        select(AttendanceEvent)
        .where(
            AttendanceEvent.event_type.in_([
                AttendanceEventType.check_in.value,
                AttendanceEventType.check_out.value,
            ]),
            AttendanceEvent.happened_at >= start_at,
            AttendanceEvent.happened_at < end_at,
        )
        .order_by(AttendanceEvent.employee_id, AttendanceEvent.happened_at)
    ).all()

    by_employee: dict[int, dict[str, AttendanceEvent]] = {}
    for event in events:
        entry = by_employee.setdefault(event.employee_id, {})
        if event.event_type == AttendanceEventType.check_in.value:
            entry.setdefault("check_in", event)
        elif event.event_type == AttendanceEventType.check_out.value:
            entry["check_out"] = event

    employees = {
        employee.id: employee
        for employee in session.exec(
            select(Employee).where(Employee.id.in_(list(by_employee)), Employee.status == EmployeeStatus.active)
        ).all()
    }
    check_in_lines: list[str] = []
    check_out_lines: list[str] = []
    incomplete_lines: list[str] = []

    for employee_id in sorted(by_employee, key=lambda item: employees.get(item).employee_code if employees.get(item) else ""):
        employee = employees.get(employee_id)
        if not employee:
            continue
        entry = by_employee[employee_id]
        check_in = entry.get("check_in")
        check_out = entry.get("check_out")
        if check_in:
            check_in_lines.append(f"・{employee.name}　{_format_local_time(check_in.happened_at)}")
        if check_out:
            check_out_lines.append(f"・{employee.name}　{_format_local_time(check_out.happened_at)}")
        if not check_in or not check_out:
            missing = "上班打卡" if not check_in else "下班打卡"
            incomplete_lines.append(f"・{employee.name}　未{missing}")

    lines = [f"【三通工程行每日上下班打卡】", f"日期：{target_date:%Y/%m/%d}"]
    lines.append(f"上班打卡（{len(check_in_lines)} 人）")
    lines.extend(check_in_lines or ["・今日尚無上班打卡紀錄"])
    lines.append(f"下班打卡（{len(check_out_lines)} 人）")
    lines.extend(check_out_lines or ["・今日尚無下班打卡紀錄"])
    if incomplete_lines:
        lines.append(f"未完整打卡（{len(incomplete_lines)} 人）")
        lines.extend(incomplete_lines)
    return "\n".join(lines)


async def push_daily_attendance_summary(target_date: date | None = None) -> dict[str, object]:
    """傳送每日上下班打卡彙整；同一曆日只建立一個通知批次。"""
    target_date = target_date or datetime.now(ZoneInfo(settings.timezone)).date()
    target_value = target_date.isoformat()
    with session_scope() as session:
        already_sent = session.exec(
            select(NotificationBatch).where(
                NotificationBatch.category == NotificationCategory.attendance_alert,
                NotificationBatch.target_scope == ATTENDANCE_SUMMARY_TARGET_SCOPE,
                NotificationBatch.target_value == target_value,
            )
        ).first()
        if already_sent:
            return {"status": "already_sent", "batch_id": already_sent.id}

        recipients = session.exec(
            select(Employee).where(
                Employee.employee_code.in_(ATTENDANCE_SUMMARY_RECIPIENT_CODES),
                Employee.status == EmployeeStatus.active,
            )
        ).all()
        recipients.sort(key=lambda employee: ATTENDANCE_SUMMARY_RECIPIENT_CODES.index(employee.employee_code))
        content = _build_daily_attendance_summary(session, target_date)
        sender = next((employee for employee in recipients if employee.employee_code == "BOSS001"), None)
        result = await notify_employees(
            session=session,
            sender=sender,
            employees=recipients,
            category=NotificationCategory.attendance_alert,
            target_scope=ATTENDANCE_SUMMARY_TARGET_SCOPE,
            target_value=target_value,
            content=content,
        )
        return {"status": "sent", **result}


async def wake_up_service() -> None:
    """定期呼叫 /health 端點，避免 Render free plan 休眠導致 LINE Webhook 逾時。"""
    health_url = f"{settings.public_base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(health_url)
            if response.status_code == 200:
                pass  # 喚醒成功，不需記錄
    except Exception:
        pass  # 網路錯誤時靜默忽略，避免影響其他排程


async def push_daily_assignments() -> None:
    with session_scope() as session:
        assignments = session.exec(select(WorkAssignment).where(WorkAssignment.work_date == date.today())).all()
        for assignment in assignments:
            members = session.exec(
                select(AssignmentMember).where(AssignmentMember.assignment_id == assignment.id)
            ).all()
            employees = [session.get(Employee, member.employee_id) for member in members]
            employees = [employee for employee in employees if employee]
            if not employees:
                continue
            worksite = session.get(Worksite, assignment.site_id)
            supervisor = session.get(Employee, assignment.supervisor_id) if assignment.supervisor_id else None
            content = _build_schedule_summary(assignment, worksite, supervisor.name if supervisor else None)
            await notify_employees(
                session=session,
                sender=supervisor,
                employees=employees,
                category=NotificationCategory.daily_schedule,
                target_scope="assignment",
                target_value=str(assignment.id),
                content=content,
                assignment_id=assignment.id,
            )


async def push_forklift_inspection_reminder() -> None:
    """08:30 後補發當日未點檢提醒；同日已有批次時不重複發送。"""
    now = datetime.now(ZoneInfo(settings.timezone))
    if (now.hour, now.minute) < (8, 30):
        return
    with session_scope() as session:
        queue_inspection_reminders(session)
        await deliver_forklift_notifications(session)


async def backup_database_snapshot() -> None:
    from app.services.google_drive import google_drive_worklog_service
    await google_drive_worklog_service.backup_database()


async def push_forklift_alerts() -> None:
    with session_scope() as session:
        await process_forklift_alerts(session)


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        push_forklift_alerts,
        "cron",
        minute=0,
        id="forklift-alert-delivery",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        push_daily_assignments,
        "cron",
        hour=settings.daily_push_hour,
        minute=settings.daily_push_minute,
        id="daily-assignment-push",
        replace_existing=True,
    )
    # 每日晚間彙整上下班打卡給行政、系統管理者與老闆；函式內另以日期去重。
    scheduler.add_job(
        push_daily_attendance_summary,
        "cron",
        hour=settings.attendance_summary_hour,
        minute=settings.attendance_summary_minute,
        id="daily-attendance-summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # 每日早上 8:30 提醒員工完成堆高機點檢
    scheduler.add_job(
        push_forklift_inspection_reminder,
        "cron",
        hour="8-23",
        minute="*/10",
        id="forklift-inspection-reminder",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # 每 10 分鐘喚醒服務，避免 Render free plan 休眠
    scheduler.add_job(
        wake_up_service,
        "interval",
        minutes=10,
        id="service-wake-up",
        replace_existing=True,
    )
    scheduler.add_job(
        backup_database_snapshot,
        "interval",
        minutes=10,
        id="database-drive-backup",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


