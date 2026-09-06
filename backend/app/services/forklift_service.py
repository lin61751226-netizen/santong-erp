# -*- coding: utf-8 -*-
"""堆高機每日點檢 LINE 互動服務。

流程：員工輸入「點檢」→ 自動帶入今日打卡工地 → 選擇今日開的堆高機 → 逐項點檢 → 完成記錄
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Optional

from sqlmodel import Session, select

from app.core.config import settings
from app.models import Employee, Forklift, ForkliftInspection, ForkliftStatus, Worksite


# 點檢項目定義
INSPECTION_ITEMS = [
    {"key": "engine_oil", "label": "引擎機油"},
    {"key": "coolant", "label": "冷卻水"},
    {"key": "battery", "label": "電瓶液"},
    {"key": "tires", "label": "輪胎磨損"},
    {"key": "horn", "label": "喇叭"},
    {"key": "lights", "label": "燈光（前燈/尾燈/方向燈）"},
    {"key": "brakes", "label": "剎車系統"},
    {"key": "hydraulic", "label": "油壓系統"},
    {"key": "fork_chain", "label": "貨叉與鏈條"},
    {"key": "safety_belt", "label": "安全帶與後照鏡"},
]

# 提醒門檻
LOW_FUEL_THRESHOLD = 30  # 油量低於 30% 提醒
MAINTENANCE_WARNING_DAYS = 7  # 保養日期 7 天內提醒

# 現場「異常回報」常見堆高機問題（LINE quick replies 點選，點擊後送出「異常回報 {問題}」）
FORKLIFT_EXCEPTION_OPTIONS = [
    "無法啟動/發不動",
    "煞車異常",
    "輪胎破損",
    "漏油或漏水",
    "貨叉無法升降",
    "燈光或喇叭故障",
    "電瓶沒電",
    "異常聲音或震動",
    "燃料不足",
    "其他問題",
]


def local_today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


@dataclass
class InspectionSession:
    """點檢工作階段狀態。"""
    line_user_id: str
    employee_id: int
    step: str = "select_site"  # select_site, select_forklift, inspecting, done
    site_id: Optional[int] = None
    forklift_id: Optional[int] = None
    current_item_index: int = 0
    inspection_results: dict[str, bool] = field(default_factory=dict)
    notes: str = ""


# 記憶體中的工作階段管理（key: line_user_id）
_active_sessions: dict[str, InspectionSession] = {}


def get_session(line_user_id: str) -> Optional[InspectionSession]:
    """取得使用者的點檢工作階段。"""
    return _active_sessions.get(line_user_id)


def start_session(line_user_id: str, employee_id: int) -> InspectionSession:
    """開始新的點檢工作階段。"""
    session = InspectionSession(line_user_id=line_user_id, employee_id=employee_id)
    _active_sessions[line_user_id] = session
    return session


def clear_session(line_user_id: str) -> None:
    """清除使用者的點檢工作階段。"""
    _active_sessions.pop(line_user_id, None)


def build_site_quick_replies(session: Session) -> list[tuple[str, str]]:
    """建立工地選擇的 quick reply 列表。"""
    worksites = session.exec(select(Worksite).where(Worksite.is_active.is_(True)).order_by(Worksite.name)).all()
    return [(ws.name, f"點檢工地:{ws.id}") for ws in worksites]


def build_all_forklift_quick_replies(session: Session) -> list[tuple[str, str]]:
    """建立所有堆高機的 quick reply 列表（不限工地，員工選今日開的車）。"""
    forklifts = session.exec(
        select(Forklift).where(Forklift.status != "inactive").order_by(Forklift.forklift_code)
    ).all()
    if not forklifts:
        return []
    return [(f.forklift_code, f"點檢堆高機:{f.id}") for f in forklifts]


def build_forklift_quick_replies(session: Session, site_id: int) -> list[tuple[str, str]]:
    """建立指定工地的堆高機選擇 quick reply 列表（保留相容性）。"""
    return build_all_forklift_quick_replies(session)


def get_current_item(session_state: InspectionSession) -> Optional[dict[str, Any]]:
    """取得目前正在點檢的項目。"""
    if session_state.current_item_index < len(INSPECTION_ITEMS):
        return INSPECTION_ITEMS[session_state.current_item_index]
    return None


def record_item_result(session_state: InspectionSession, is_normal: bool, notes: str = "") -> bool:
    """記錄目前項目的點檢結果。回傳 True 表示還有下一項，False 表示全部完成。"""
    item = get_current_item(session_state)
    if not item:
        return False
    session_state.inspection_results[item["key"]] = is_normal
    if notes:
        session_state.notes += f"\n{item['label']}：{notes}"
    session_state.current_item_index += 1
    return session_state.current_item_index < len(INSPECTION_ITEMS)


def save_inspection(session: Session, session_state: InspectionSession) -> ForkliftInspection:
    """儲存點檢記錄到資料庫，並自動更新堆高機的目前操作員。"""
    keys = {item["key"] for item in INSPECTION_ITEMS}
    if set(session_state.inspection_results) != keys or any(
        type(value) is not bool for value in session_state.inspection_results.values()
    ):
        raise ValueError("請完成全部 10 項點檢後再儲存。")
    employee = session.get(Employee, session_state.employee_id)
    forklift = session.get(Forklift, session_state.forklift_id)
    site = session.get(Worksite, session_state.site_id)
    if not employee or employee.status != "active" or employee.line_user_id != session_state.line_user_id:
        raise ValueError("員工身分已異動，請重新開始點檢。")
    if not forklift or forklift.status == ForkliftStatus.inactive or not site or not site.is_active:
        raise ValueError("工地或堆高機已停用，請重新選擇。")
    all_passed = all(session_state.inspection_results.values())
    inspection = ForkliftInspection(
        forklift_id=session_state.forklift_id,
        operator_id=session_state.employee_id,
        site_id=session_state.site_id,
        inspection_date=local_today(),
        inspection_items=session_state.inspection_results,
        all_passed=all_passed,
        notes=session_state.notes.strip() or None,
    )
    session.add(inspection)

    # 自動更新堆高機的目前操作員和工地
    forklift = session.get(Forklift, session_state.forklift_id)
    if forklift:
        forklift.current_operator_id = session_state.employee_id
        if session_state.site_id:
            forklift.current_site_id = session_state.site_id
        forklift.updated_at = datetime.utcnow()
        session.add(forklift)

    session.commit()
    session.refresh(inspection)
    return inspection


def check_forklift_warnings(session: Session, forklift_id: int) -> list[str]:
    """檢查堆高機的油量和保養提醒。"""
    warnings = []
    forklift = session.get(Forklift, forklift_id)
    if not forklift:
        return warnings

    # 油量提醒
    if forklift.fuel_level is not None and forklift.fuel_level < LOW_FUEL_THRESHOLD:
        warnings.append(f"⛽ 油量僅剩 {forklift.fuel_level}%，請盡快加油")

    # 保養提醒
    if forklift.next_maintenance_date:
        days_until = (forklift.next_maintenance_date - local_today()).days
        if days_until < 0:
            warnings.append(f"🔧 保養日期已過期 {abs(days_until)} 天，請盡快安排保養")
        elif days_until <= MAINTENANCE_WARNING_DAYS:
            warnings.append(f"🔧 將於 {days_until} 天後（{forklift.next_maintenance_date.isoformat()}）到期保養")

    return warnings


def build_inspection_summary(session: Session, inspection: ForkliftInspection) -> str:
    """建立點檢完成的摘要訊息，包含油量和保養提醒。"""
    forklift = session.get(Forklift, inspection.forklift_id)
    site = session.get(Worksite, inspection.site_id) if inspection.site_id else None
    employee = session.get(Employee, inspection.operator_id)

    lines = [
        "✅ 堆高機每日點檢完成",
        "",
        f"堆高機：{forklift.forklift_code if forklift else '-'}（{forklift.model if forklift else '-'}）",
        f"工地：{site.name if site else '-'}",
        f"操作員：{employee.name if employee else '-'}",
        f"日期：{inspection.inspection_date.isoformat()}",
        "",
    ]

    if inspection.all_passed:
        lines.append("✅ 全部 10 項檢查正常")
    else:
        failed_items = [
            item["label"]
            for item in INSPECTION_ITEMS
            if (inspection.inspection_items or {}).get(item["key"]) is False
        ]
        lines.append(f"⚠️ 有 {len(failed_items)} 項異常：")
        for item in failed_items:
            lines.append(f"  - {item}")

    if inspection.notes:
        lines.append("")
        lines.append(f"備註：{inspection.notes}")

    # 油量和保養提醒
    warnings = check_forklift_warnings(session, inspection.forklift_id)
    if warnings:
        lines.append("")
        lines.append("【提醒】")
        for warning in warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def build_boss_notification(session: Session, inspection: ForkliftInspection) -> str:
    """建立給老闆的異常通知訊息。"""
    forklift = session.get(Forklift, inspection.forklift_id)
    site = session.get(Worksite, inspection.site_id) if inspection.site_id else None
    employee = session.get(Employee, inspection.operator_id)

    failed_items = [
        item["label"]
        for item in INSPECTION_ITEMS
        if (inspection.inspection_items or {}).get(item["key"]) is False
    ]

    lines = [
        "🚨 堆高機點檢異常通知",
        "",
        f"堆高機：{forklift.forklift_code if forklift else '-'}（{forklift.model if forklift else '-'}）",
        f"工地：{site.name if site else '-'}",
        f"操作員：{employee.name if employee else '-'}",
        f"日期：{inspection.inspection_date.isoformat()}",
        "",
        f"異常項目（{len(failed_items)} 項）：",
    ]
    for item in failed_items:
        lines.append(f"  - {item}")

    if inspection.notes:
        lines.append("")
        lines.append(f"備註：{inspection.notes}")

    lines.append("")
    lines.append("請盡快安排檢修或確認。")

    return "\n".join(lines)


def list_today_inspections(session: Session, forklift_id: int) -> list[ForkliftInspection]:
    """查詢某台堆高機今日的點檢記錄。"""
    return session.exec(
        select(ForkliftInspection).where(
            ForkliftInspection.forklift_id == forklift_id,
            ForkliftInspection.inspection_date == local_today(),
        )
    ).all()


def list_today_inspections_by_operator(session: Session, operator_id: int) -> list[ForkliftInspection]:
    """查詢某操作員今日的點檢記錄。"""
    return session.exec(
        select(ForkliftInspection).where(
            ForkliftInspection.operator_id == operator_id,
            ForkliftInspection.inspection_date == local_today(),
        )
    ).all()
