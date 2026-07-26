from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.core.config import settings
from app.core.db import get_session
from app.deps import get_current_actor, require_roles
from app.models import Employee, Role
from app.schemas import LineRichMenuDeployRequest, LineWebhookConfigureRequest
from app.services.line_platform import (
    LinePlatformError,
    deploy_default_rich_menus,
    line_platform_service,
    prepare_account_link_redirect,
)


BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

api_router = APIRouter(prefix="/api/line-management", tags=["line-management"])
page_router = APIRouter(tags=["line-pages"])


@api_router.get("/status")
async def line_status(
    actor: Employee = Depends(require_roles(Role.owner, Role.admin, Role.site_manager)),
):
    webhook_info = {}
    webhook_error = None
    if settings.line_channel_access_token:
        try:
            webhook_info = await line_platform_service.get_webhook_endpoint()
        except LinePlatformError as exc:
            webhook_error = str(exc)
    return {
        "configured": {
            "channel_secret": bool(settings.line_channel_secret),
            "channel_access_token": bool(settings.line_channel_access_token),
            "public_base_url": settings.public_base_url,
        },
        "recommended_webhook_url": f"{settings.public_base_url.rstrip('/')}/api/line/webhook",
        "webhook_info": webhook_info,
        "webhook_error": webhook_error,
    }


@api_router.post("/webhook/configure")
async def configure_webhook(
    payload: LineWebhookConfigureRequest,
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    base_url = (payload.base_url or settings.public_base_url).rstrip("/")
    endpoint = f"{base_url}/api/line/webhook"
    await line_platform_service.set_webhook_endpoint(endpoint)
    result = {"webhook_endpoint": endpoint}
    if payload.test_after_set:
        result["test_result"] = await line_platform_service.test_webhook_endpoint(endpoint)
    return result


@api_router.post("/webhook/test")
async def test_webhook(
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    return await line_platform_service.test_webhook_endpoint()


@api_router.post("/richmenu/deploy")
async def deploy_rich_menu(
    payload: LineRichMenuDeployRequest,
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    base_url = (payload.base_url or settings.public_base_url).rstrip("/")
    return await deploy_default_rich_menus(base_url)


@page_router.get("/line/account-link", response_class=HTMLResponse)
def account_link_form(request: Request, linkToken: str, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        "account_link.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "link_token": linkToken,
            "error": None,
        },
    )


@page_router.post("/line/account-link", response_class=HTMLResponse)
def account_link_submit(
    request: Request,
    link_token: str = Form(...),
    employee_code: str = Form(...),
    bind_token: str = Form(...),
    session: Session = Depends(get_session),
):
    try:
        redirect_url = prepare_account_link_redirect(
            session=session,
            link_token=link_token.strip(),
            employee_code=employee_code.strip(),
            bind_token=bind_token.strip(),
        )
    except LinePlatformError as exc:
        return templates.TemplateResponse(
            "account_link.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "link_token": link_token,
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(url=redirect_url, status_code=303)
