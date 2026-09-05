# -*- coding: utf-8 -*-
"""堆高機每日點檢 LINE 互動服務。

流程：員工輸入「點檢」→ 選擇工地 → 選擇堆高機 → 逐項點檢（正常/異常）→ 完成記錄
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from sqlmodel import Session, select

from app.models import Employee, Forklift, ForkliftInspection, Worksite


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


def build_forklift_quick_replies(session: Session, site_id: int) -> list[tuple[str, str]]:
    """建立指定工地的堆高機選擇 quick reply 列表。"""
    forklifts = session.exec(
        select(Forklift).where(Forklift.current_site_id == site_id, Forklift.status != "inactive")
    ).all()
    if not forklifts:
        return []
    return [(f.forklift_code, f"點檢堆高機:{f.id}") for f in forklifts]


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
    """儲存點檢記錄到資料庫。"""
    all_passed = all(session_state.inspection_results.values())
    inspection = ForkliftInspection(
        forklift_id=session_state.forklift_id,
        operator_id=session_state.employee_id,
        site_id=session_state.site_id,
        inspection_date=date.today(),
        inspection_items=session_state.inspection_results,
        all_passed=all_passed,
        notes=session_state.notes.strip() or None,
    )
    session.add(inspection)
    session.commit()
    session.refresh(inspection)
    return inspection


def build_inspection_summary(session: Session, inspection: ForkliftInspection) -> str:
    """建立點檢完成的摘要訊息。"""
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
            if not inspection.inspection_results.get(item["key"], True)
        ]
        lines.append(f"⚠️ 有 {len(failed_items)} 項異常：")
        for item in failed_items:
            lines.append(f"  - {item}")

    if inspection.notes:
        lines.append("")
        lines.append(f"備註：{inspection.notes}")

    return "\n".join(lines)


def list_today_inspections(session: Session, forklift_id: int) -> list[ForkliftInspection]:
    """查詢某台堆高機今日的點檢記錄。"""
    return session.exec(
        select(ForkliftInspection).where(
            ForkliftInspection.forklift_id == forklift_id,
            ForkliftInspection.inspection_date == date.today(),
        )
    ).all()
