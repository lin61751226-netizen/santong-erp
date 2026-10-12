# -*- coding: utf-8 -*-
"""LINE 群組文字的工地重新歸類。

群組文字在寫入當下就帶有 site_id，當時若員工尚未點「到達工地」、尚未做 GPS
上班打卡，或派工單是事後才補建，site_id 會落到主工地甚至落空。本日誌整理屬於
事後行為，此模組用最新的「事實位置優先」解析器（app.services.hr）重新歸類，
讓後台每次整理工作日誌都能自動修正，也提供單筆人工改工地的能力。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlmodel import Session, select

from app.core.local_time import as_local, utc_day_bounds
from app.models import AdminAuditLog, Employee, GroupTextLog, Worksite
from app.services.hr import resolve_employee_site_at


class GroupTextReassignError(Exception):
    """重新歸類失敗（如記錄或工地不存在）。"""


def _audit(session: Session, actor: Optional[Employee], action: str, summary: str, entity_id: Optional[int]) -> None:
    session.add(AdminAuditLog(
        actor_id=actor.id if actor else None,
        actor_code=actor.employee_code if actor else "SYSTEM",
        actor_name=actor.name if actor else "工作日誌自動歸類",
        action=action,
        entity_type="group_text_log",
        entity_id=entity_id,
        summary=summary[:1000],
    ))


def reassign_group_texts_for_date(
    session: Session,
    work_date: date,
    actor: Optional[Employee] = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """重新解析某日全部群組文字的工地，回傳異動明細。

    只會把記錄改到「解析得到且與目前不同」的工地，絕不把工地清空；
    未綁定員工或完全無考勤、派工、主工地線索的記錄會原樣保留。
    """
    start_at, end_at = utc_day_bounds(work_date)
    logs = session.exec(
        select(GroupTextLog).where(
            GroupTextLog.sent_at >= start_at,
            GroupTextLog.sent_at < end_at,
        ).order_by(GroupTextLog.sent_at, GroupTextLog.id)
    ).all()

    worksites = {row.id: row for row in session.exec(select(Worksite)).all() if row.id is not None}
    updated: list[dict[str, Any]] = []
    unresolved: list[int] = []
    # 自動修正只接受考勤事實與有效派工；「主工地」只是寫入當下的猜測，
    # 不應在事後覆蓋既有紀錄，也不把未判定紀錄憑空猜成主工地。
    AUTO_REASONS = {"location_before", "location_after", "assignment"}

    for log in logs:
        if getattr(log, "site_manual", False):
            # 人工已指定工地並鎖定，絕不自動覆蓋。
            continue
        if not log.employee_id:
            unresolved.append(log.id)
            continue
        site, reason, assignment = resolve_employee_site_at(session, log.employee_id, log.sent_at, work_date)
        if site is None or reason not in AUTO_REASONS:
            unresolved.append(log.id)
            continue
        if log.site_id == site.id:
            continue

        old_name = worksites.get(log.site_id).name if log.site_id and worksites.get(log.site_id) else "未判定工地"
        updated.append({
            "id": log.id,
            "employee_id": log.employee_id,
            "from_site_id": log.site_id,
            "from_site_name": old_name,
            "to_site_id": site.id,
            "to_site_name": site.name,
            "reason": reason,
            "content": (log.content or "")[:60],
            "sent_at": as_local(log.sent_at).strftime("%H:%M"),
        })
        log.site_id = site.id
        if assignment is not None:
            log.assignment_id = assignment.id
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        tail = f"系統於 {stamp} 依考勤/派工重新歸類至「{site.name}」"
        log.note = f"{log.note}；{tail}" if log.note else tail
        session.add(log)

    if updated and not dry_run:
        _audit(
            session,
            actor,
            "group_text_auto_reassign",
            f"{work_date.isoformat()} 群組文字自動重新歸類 {len(updated)} 筆："
            + "；".join(
                f"#{row['id']} {row['sent_at']} {row['from_site_name']}→{row['to_site_name']}（{row['content']}）"
                for row in updated
            ),
            None,
        )
        session.commit()
    elif updated and dry_run:
        session.rollback()

    return {
        "work_date": work_date.isoformat(),
        "checked": len(logs),
        "updated_count": len(updated),
        "updated": updated,
        "unresolved_ids": unresolved,
    }


def reassign_group_text_log(
    session: Session,
    log_id: int,
    worksite_id: int,
    actor: Employee,
) -> GroupTextLog:
    """人工把單筆群組文字移到指定工地（後台工作日誌使用）。"""
    log = session.get(GroupTextLog, log_id)
    if log is None:
        raise GroupTextReassignError("找不到該筆群組文字記錄")
    site = session.get(Worksite, worksite_id)
    if site is None or not site.is_active:
        raise GroupTextReassignError("找不到啟用中的目標工地")
    if log.site_id == site.id:
        return log

    worksites = {row.id: row for row in session.exec(select(Worksite)).all() if row.id is not None}
    old_name = worksites.get(log.site_id).name if log.site_id and worksites.get(log.site_id) else "未判定工地"
    log.site_id = site.id
    log.site_manual = True
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    tail = f"{actor.name} 於 {stamp} 人工改歸「{site.name}」（原 {old_name}）"
    log.note = f"{log.note}；{tail}" if log.note else tail
    session.add(log)
    _audit(
        session,
        actor,
        "group_text_manual_reassign",
        f"群組文字 #{log.id}「{(log.content or '')[:40]}」由 {old_name} 改歸 {site.name}",
        log.id,
    )
    session.commit()
    return log
