"""Persist forklift alerts and delivery results using the existing notification tables."""
from __future__ import annotations

from datetime import datetime
import asyncio
import hashlib

from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    DeliveryStatus, Employee, Forklift, ForkliftInspection, NotificationBatch,
    NotificationCategory, NotificationDelivery, Role,
)
from app.services.forklift_service import (
    build_boss_notification, check_forklift_warnings, local_today,
)

INSPECTION_SCOPE = "forklift_inspection_alert"
WARNING_SCOPE = "forklift_vehicle_warning"
_delivery_lock = asyncio.Lock()


def _managers(session: Session) -> list[Employee]:
    return list(session.exec(select(Employee).where(
        Employee.status == "active", Employee.role.in_([Role.owner, Role.admin]),
    )).all())


def _queue(session: Session, scope: str, key: str, content: str, recipients: list[Employee]) -> None:
    batch = session.exec(select(NotificationBatch).where(
        NotificationBatch.target_scope == scope, NotificationBatch.target_value == key,
    )).first()
    if batch is None:
        batch = NotificationBatch(
            category=NotificationCategory.attendance_alert,
            target_scope=scope, target_value=key, content=content,
        )
        session.add(batch)
        session.flush()
    existing = {row.employee_id for row in session.exec(select(NotificationDelivery).where(
        NotificationDelivery.batch_id == batch.id,
    )).all()}
    for employee in recipients:
        if employee.id not in existing:
            session.add(NotificationDelivery(
                batch_id=batch.id, employee_id=employee.id, line_user_id=employee.line_user_id,
                delivery_status=DeliveryStatus.pending,
            ))
            existing.add(employee.id)
    session.commit()


def queue_inspection_alert(session: Session, inspection: ForkliftInspection) -> None:
    if not inspection.all_passed:
        _queue(session, INSPECTION_SCOPE, str(inspection.id),
               build_boss_notification(session, inspection), _managers(session))


def _warning_key(session: Session, forklift: Forklift) -> tuple[str, list[str]]:
    warnings = check_forklift_warnings(session, forklift.id)
    digest = hashlib.sha256("\n".join(warnings).encode("utf-8")).hexdigest()[:16]
    return f"{local_today().isoformat()}:{forklift.id}:{digest}", warnings


def queue_vehicle_warning(session: Session, forklift: Forklift) -> None:
    if forklift.status == "inactive":
        return
    key, warnings = _warning_key(session, forklift)
    if not warnings:
        return
    recipients = _managers(session)
    operator = session.get(Employee, forklift.current_operator_id) if forklift.current_operator_id else None
    if operator and operator.status == "active":
        recipients.append(operator)
    content = f"【堆高機油量／保養提醒】\n車輛：{forklift.forklift_code}\n" + "\n".join(warnings)
    _queue(session, WARNING_SCOPE, key, content, recipients)


async def deliver_forklift_notifications(session: Session) -> None:
    # LINE webhook handlers and the scheduler share one process; serialize their send loops.
    async with _delivery_lock:
        await _deliver_pending(session)


async def _deliver_pending(session: Session) -> None:
    from app.services.line import line_service

    rows = session.exec(select(NotificationDelivery, NotificationBatch).join(
        NotificationBatch, NotificationDelivery.batch_id == NotificationBatch.id,
    ).where(
        NotificationBatch.target_scope.in_([INSPECTION_SCOPE, WARNING_SCOPE]),
        NotificationDelivery.delivery_status != DeliveryStatus.sent,
    ).order_by(NotificationDelivery.id)).all()
    for delivery, batch in rows:
        employee = session.get(Employee, delivery.employee_id) if delivery.employee_id else None
        if batch.target_scope == WARNING_SCOPE:
            parts = (batch.target_value or "").split(":")
            forklift = session.get(Forklift, int(parts[1])) if len(parts) == 3 and parts[1].isdigit() else None
            if (not forklift or forklift.status == "inactive"
                    or _warning_key(session, forklift)[0] != batch.target_value):
                # Don't retry an obsolete fuel/maintenance warning after it has been resolved.
                delivery.delivery_status = DeliveryStatus.skipped
                delivery.delivery_message = "提醒條件已改變或日期已過，不再重送"
                session.add(delivery)
                session.commit()
                continue
        eligible = employee is not None and employee.status == "active"
        if batch.target_scope == INSPECTION_SCOPE:
            eligible = eligible and employee.role in {Role.owner, Role.admin}
        else:
            eligible = eligible and (employee.role in {Role.owner, Role.admin}
                                     or employee.id == forklift.current_operator_id)
        delivery.line_user_id = employee.line_user_id if employee else None
        if not eligible or not delivery.line_user_id:
            delivery.delivery_status = DeliveryStatus.skipped
            delivery.delivery_message = "帳號已停用、無通知資格或尚未綁定 LINE"
        elif not settings.line_channel_access_token:
            delivery.delivery_status = DeliveryStatus.simulated
            delivery.delivery_message = "LINE token 未設定，未實際發送"
        else:
            try:
                ok, detail = await line_service.push_text(delivery.line_user_id, batch.content)
                delivery.delivery_status = DeliveryStatus.sent if ok else DeliveryStatus.failed
                delivery.delivery_message = detail
            except Exception as exc:
                delivery.delivery_status = DeliveryStatus.failed
                delivery.delivery_message = f"發送失敗（{type(exc).__name__}），等待重試"
        delivery.sent_at = datetime.utcnow()
        session.add(delivery)
        session.commit()


async def process_forklift_alerts(session: Session) -> None:
    for forklift in session.exec(select(Forklift).where(Forklift.status != "inactive")).all():
        queue_vehicle_warning(session, forklift)
    await deliver_forklift_notifications(session)
