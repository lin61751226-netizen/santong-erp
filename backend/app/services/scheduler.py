from __future__ import annotations

from datetime import date

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.core.config import settings
from app.core.db import session_scope
from app.models import AssignmentMember, Employee, NotificationCategory, WorkAssignment, Worksite
from app.services.line import _build_schedule_summary, notify_employees


scheduler = AsyncIOScheduler(timezone=settings.timezone)


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


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        push_daily_assignments,
        "cron",
        hour=settings.daily_push_hour,
        minute=settings.daily_push_minute,
        id="daily-assignment-push",
        replace_existing=True,
    )
    # 每 10 分鐘喚醒服務，避免 Render free plan 休眠
    scheduler.add_job(
        wake_up_service,
        "interval",
        minutes=10,
        id="service-wake-up",
        replace_existing=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


