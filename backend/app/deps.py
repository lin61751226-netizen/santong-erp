from collections.abc import Callable
from typing import Optional

from fastapi import Depends, Header, HTTPException, Query, status
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import get_session
from app.models import Employee, Role


def get_current_actor(
    session: Session = Depends(get_session),
    actor_code_query: Optional[str] = Query(default=None, alias="actor_code"),
    actor_code_header: Optional[str] = Header(default=None, alias="X-Actor-Code"),
) -> Employee:
    actor_code = actor_code_header or actor_code_query or settings.default_actor_code
    actor = session.exec(select(Employee).where(Employee.employee_code == actor_code)).first()
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="找不到操作身分")
    return actor


def require_roles(*allowed_roles: Role | str) -> Callable:
    allowed_values = {role.value if isinstance(role, Role) else role for role in allowed_roles}

    def dependency(actor: Employee = Depends(get_current_actor)) -> Employee:
        if actor.role.value not in allowed_values:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="此角色無權限執行此操作")
        return actor

    return dependency


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

