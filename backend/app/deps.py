from collections.abc import Callable
from datetime import datetime
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import get_session
from app.core.security import verify_signed_token
from app.models import Employee, Role

SESSION_COOKIE_NAME = "santong_session"


def get_current_actor(
    session: Session = Depends(get_session),
    cookie_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Employee:
    """後台操作身分：一律以簽名 session cookie 驗證，不再接受 X-Actor-Code 模擬身分。

    - 未登入 → 401
    - 僅 owner/admin 可存取後台
    - 若帳號仍須強制改密碼，僅放行改密碼相關 API（由 require_roles 層級判斷）
    """
    actor = _resolve_actor(session, cookie_session)
    if actor is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="尚未登入或登入已過期")

    if actor.role not in {Role.owner, Role.admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="此帳號無後台存取權限")

    # 尚未改密碼時擋下所有後台操作（改密碼/登出/身分查詢由 auth 路由自行處理，不走本依賴）
    if actor.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="首次登入請先變更密碼",
        )

    return actor


def require_roles(*allowed_roles: Role | str) -> Callable:
    allowed_values = {role.value if isinstance(role, Role) else role for role in allowed_roles}

    def dependency(actor: Employee = Depends(get_current_actor)) -> Employee:
        if actor.role.value not in allowed_values:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="此角色無權限執行此操作")
        return actor

    return dependency


def _resolve_actor(session: Session, cookie_session: Optional[str]) -> Optional[Employee]:
    if not cookie_session:
        return None
    raw_token = verify_signed_token(cookie_session)
    if not raw_token:
        return None
    actor = session.exec(
        select(Employee).where(Employee.session_key == raw_token)
    ).first()
    if actor is None:
        return None
    if actor.session_expires_at is None or actor.session_expires_at < datetime.utcnow():
        return None
    return actor


def ensure_site_scope(actor: Employee, site_id: Optional[int]) -> None:
    if actor.role in {Role.owner, Role.admin}:
        return
    if actor.role == Role.site_manager and actor.home_site_id == site_id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="無權存取此工地資料")


def ensure_employee_scope(actor: Employee, employee: Employee) -> None:
    if actor.role in {Role.owner, Role.admin}:
        return
    if actor.role == Role.site_manager and actor.home_site_id == employee.home_site_id:
        return
    if actor.id == employee.id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="無權存取此員工資料")
