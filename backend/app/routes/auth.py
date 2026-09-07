"""後台登入 / 登出 / 改密碼路由。

規則：
- 僅 owner / admin 可登入後台
- 首次以統一預設密碼登入，登入後強制改密碼
- 連續登入失敗 N 次鎖定一段時間（預設 3 次 / 15 分鐘）
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import get_session
from app.core.security import (
    create_session_token,
    hash_password,
    sign_token,
    verify_password,
    verify_signed_token,
)
from app.deps import require_roles
from app.models import Employee, LoginLog, LoginStatus, Role
from app.schemas import ChangePasswordRequest, LoginRequest, PasswordResetRequest
from app.services.google_drive import google_drive_worklog_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE_NAME = "santong_session"


def _now() -> datetime:
    return datetime.utcnow()


async def _log_login(
    session: Session,
    request: Request,
    employee_code: str,
    employee_name: str | None,
    status: LoginStatus,
    failure_reason: str | None = None,
) -> None:
    """記錄登入稽核日誌。"""
    try:
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        log = LoginLog(
            employee_code=employee_code,
            employee_name=employee_name,
            ip_address=ip_address,
            user_agent=user_agent,
            status=status,
            failure_reason=failure_reason,
        )
        session.add(log)
        session.commit()
        await google_drive_worklog_service.backup_database()
    except Exception:
        session.rollback()


def _employee_by_code(session: Session, employee_code: str) -> Employee | None:
    return session.exec(
        select(Employee).where(Employee.employee_code == employee_code)
    ).first()


def _is_backoffice_role(employee: Employee) -> bool:
    return employee.role in {Role.owner, Role.admin}


def _set_session_cookie(response: Response, employee: Employee) -> None:
    raw_token = create_session_token()
    signed = sign_token(raw_token)
    # 以員工代碼為 session key；raw token 不落庫，僅以簽章 cookie 保存（無狀態 session）。
    employee.session_key = raw_token
    session_expires = _now() + timedelta(minutes=settings.session_expire_minutes)
    employee.session_expires_at = session_expires
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed,
        max_age=settings.session_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME)


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    session: Session = Depends(get_session),
):
    employee = _employee_by_code(session, payload.employee_code.strip())
    now = _now()

    # 統一錯誤訊息，避免帳號是否存在被探測
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="帳號或密碼錯誤",
    )

    if employee is None:
        await _log_login(
            session, request, payload.employee_code.strip(), None,
            LoginStatus.failed, "帳號不存在或密碼錯誤",
        )
        raise invalid_credentials

    # 僅 owner/admin 可登入後台
    if not _is_backoffice_role(employee):
        await _log_login(
            session, request, employee.employee_code, employee.name,
            LoginStatus.failed, "無後台登入權限",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此帳號無後台登入權限",
        )

    if employee.status != "active":
        await _log_login(session, request, employee.employee_code, employee.name, LoginStatus.failed, "帳號未啟用")
        raise invalid_credentials

    # 鎖定檢查
    if employee.locked_until and employee.locked_until > now:
        remaining = int((employee.locked_until - now).total_seconds() // 60) + 1
        await _log_login(session, request, employee.employee_code, employee.name, LoginStatus.locked, "帳號已鎖定")
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"登入失敗次數過多，帳號已鎖定，請於 {remaining} 分鐘後再試",
        )

    if not verify_password(payload.password, employee.password_hash):
        employee.failed_login_count = (employee.failed_login_count or 0) + 1
        if employee.failed_login_count >= settings.login_fail_limit:
            employee.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            employee.failed_login_count = 0
            session.add(employee)
            session.commit()
            await _log_login(session, request, employee.employee_code, employee.name, LoginStatus.locked, "登入失敗次數過多，帳號鎖定")
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=f"登入失敗 {settings.login_fail_limit} 次，帳號已鎖定 {settings.login_lock_minutes} 分鐘",
            )
        session.add(employee)
        session.commit()
        remaining_attempts = settings.login_fail_limit - employee.failed_login_count
        await _log_login(session, request, employee.employee_code, employee.name, LoginStatus.failed, "密碼錯誤")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"帳號或密碼錯誤（還剩 {remaining_attempts} 次嘗試機會）",
        )

    # 登入成功：重置失敗計數
    employee.failed_login_count = 0
    employee.locked_until = None
    session.add(employee)
    session.commit()

    _set_session_cookie(response, employee)
    session.add(employee)
    session.commit()
    await _log_login(session, request, employee.employee_code, employee.name, LoginStatus.success)

    return {
        "employee_code": employee.employee_code,
        "name": employee.name,
        "role": employee.role,
        "must_change_password": employee.must_change_password,
    }


@router.post("/logout")
def logout(
    response: Response,
    session: Session = Depends(get_session),
    cookie_session: str = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    # 清除伺服器端 session，使舊 cookie 立即失效
    employee = _resolve_session_employee(session, cookie_session)
    if employee is not None:
        employee.session_key = None
        employee.session_expires_at = None
        session.add(employee)
        session.commit()
    _clear_session_cookie(response)
    return {"message": "已登出"}


@router.get("/me")
def me(
    session: Session = Depends(get_session),
    cookie_session: str = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    employee = _resolve_session_employee(session, cookie_session)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="尚未登入")
    return {
        "employee_code": employee.employee_code,
        "name": employee.name,
        "role": employee.role,
        "must_change_password": employee.must_change_password,
    }


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    session: Session = Depends(get_session),
    cookie_session: str = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    employee = _resolve_session_employee(session, cookie_session)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="尚未登入")

    if not verify_password(payload.current_password, employee.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="目前密碼錯誤")

    _validate_new_password(payload.new_password)

    employee.password_hash = hash_password(payload.new_password)
    employee.must_change_password = False
    session.add(employee)
    session.commit()
    return {"message": "密碼已更新"}


@router.post("/reset-password")
def reset_password(
    payload: PasswordResetRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    """管理者重設他人密碼（重設為統一預設密碼並強制改密碼）。僅 owner/admin 可執行。"""
    employee = _employee_by_code(session, payload.employee_code.strip())
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="找不到員工代碼")
    if not _is_backoffice_role(employee):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="此帳號無後台登入權限")
    employee.password_hash = hash_password(settings.default_password)
    employee.must_change_password = True
    employee.failed_login_count = 0
    employee.locked_until = None
    session.add(employee)
    session.commit()
    return {"message": f"{employee.employee_code} 的密碼已重設為預設密碼，下次登入需改密碼", "reset_by": actor.employee_code}


def _resolve_session_employee(
    session: Session,
    cookie_session: str | None,
) -> Employee | None:
    if not cookie_session:
        return None
    raw_token = verify_signed_token(cookie_session)
    if not raw_token:
        return None
    employee = session.exec(
        select(Employee).where(Employee.session_key == raw_token)
    ).first()
    if employee is None:
        return None
    if employee.session_expires_at is None or employee.session_expires_at < _now():
        return None
    return employee


def _validate_new_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密碼至少需要 8 個字元")
    if len(password) > 128:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密碼過長")



