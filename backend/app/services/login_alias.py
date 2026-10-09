"""後台登入名稱。員工代碼不變，登入時代碼或名稱擇一即可。"""
from __future__ import annotations

import unicodedata

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import Employee

LOGIN_ALIAS_MAX_LENGTH = 32


class LoginAliasError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def find_login_employee(session: Session, identifier: str) -> Employee | None:
    """先對員工代碼（區分大小寫），再對登入名稱（不區分大小寫）。"""
    ident = (identifier or "").strip()
    if not ident:
        return None
    by_code = session.exec(select(Employee).where(Employee.employee_code == ident)).first()
    if by_code is not None:
        return by_code
    key = ident.casefold()
    matches = [
        item
        for item in session.exec(select(Employee).where(Employee.login_alias.is_not(None))).all()
        if item.login_alias and item.login_alias.casefold() == key
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def save_login_alias(session: Session, employee: Employee, raw: str | None) -> bool:
    """寫入或清除登入名稱。沒有變更時不寫資料庫。不改密碼、session、LINE 綁定。"""
    alias = _clean_alias(raw)
    if alias is not None:
        _ensure_available(session, employee, alias)
    if employee.login_alias == alias:
        return False
    employee.login_alias = alias
    session.add(employee)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise LoginAliasError("這個登入名稱已經有人使用", 409) from exc
    session.refresh(employee)
    return True


def _clean_alias(raw: str | None) -> str | None:
    if raw is None:
        return None
    alias = str(raw).strip()
    if not alias:
        return None
    if len(alias) > LOGIN_ALIAS_MAX_LENGTH:
        raise LoginAliasError(f"登入名稱最多 {LOGIN_ALIAS_MAX_LENGTH} 個字")
    if any(unicodedata.category(char).startswith("C") for char in alias):
        raise LoginAliasError("登入名稱含有無法使用的字元")
    return alias


def _ensure_available(session: Session, employee: Employee, alias: str) -> None:
    key = alias.casefold()
    for other in session.exec(select(Employee)).all():
        if other.employee_code.casefold() == key:
            raise LoginAliasError("登入名稱不能與員工代碼相同")
        if other.id != employee.id and other.login_alias and other.login_alias.casefold() == key:
            raise LoginAliasError("這個登入名稱已經有人使用", 409)
