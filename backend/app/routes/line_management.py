from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

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


@api_router.get("/richmenu/debug")
async def debug_rich_menu():
    """公開診斷端點：逐步測試 Rich Menu 佈署過程，不需登入（臨時排查用）"""
    from app.services.line_platform import (
        generate_default_rich_menu_images,
        build_default_rich_menu_payloads,
    )
    steps = []

    # Step 1: 檢查環境變數
    has_secret = bool(settings.line_channel_secret)
    has_token = bool(settings.line_channel_access_token)
    steps.append({
        "step": 1,
        "name": "環境變數檢查",
        "ok": has_token,
        "detail": {
            "channel_secret_set": has_secret,
            "channel_access_token_set": has_token,
            "token_prefix": settings.line_channel_access_token[:20] + "..." if has_token else None,
            "environment": settings.environment,
            "public_base_url": settings.public_base_url,
        },
    })
    if not has_token:
        return {"ok": False, "steps": steps, "error": "LINE_CHANNEL_ACCESS_TOKEN 未設定"}

    # Step 2: 列出現有 Rich Menu
    try:
        existing = await line_platform_service.list_rich_menus()
        steps.append({
            "step": 2,
            "name": "列出現有 Rich Menu",
            "ok": True,
            "detail": {"count": len(existing), "menus": [
                {"id": m.get("richMenuId"), "name": m.get("name")} for m in existing[:10]
            ]},
        })
    except Exception as e:
        steps.append({"step": 2, "name": "列出現有 Rich Menu", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 3: 生成圖片
    try:
        images = generate_default_rich_menu_images()
        steps.append({
            "step": 3,
            "name": "生成 Rich Menu 圖片",
            "ok": True,
            "detail": {name: str(path) for name, path in images.items()},
        })
    except Exception as e:
        steps.append({"step": 3, "name": "生成 Rich Menu 圖片", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 4: 建立 payload
    try:
        base_url = settings.public_base_url.rstrip("/")
        payloads = build_default_rich_menu_payloads(base_url)
        steps.append({
            "step": 4,
            "name": "建立 Rich Menu payload",
            "ok": True,
            "detail": {
                "main_size": payloads["main"]["size"],
                "main_areas_count": len(payloads["main"]["areas"]),
                "tools_size": payloads["tools"]["size"],
                "tools_areas_count": len(payloads["tools"]["areas"]),
                "tools_area_labels": [a.get("action", {}).get("label") for a in payloads["tools"]["areas"]],
            },
        })
    except Exception as e:
        steps.append({"step": 4, "name": "建立 Rich Menu payload", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 5: 建立 main Rich Menu
    main_id = None
    try:
        main_id = await line_platform_service.create_rich_menu(payloads["main"])
        steps.append({"step": 5, "name": "建立 main Rich Menu", "ok": True, "detail": {"richMenuId": main_id}})
    except Exception as e:
        steps.append({"step": 5, "name": "建立 main Rich Menu", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 6: 上傳 main 圖片
    try:
        await line_platform_service.upload_rich_menu_image(main_id, images["main"])
        steps.append({"step": 6, "name": "上傳 main 圖片", "ok": True})
    except Exception as e:
        steps.append({"step": 6, "name": "上傳 main 圖片", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 7: 建立 tools Rich Menu
    tools_id = None
    try:
        tools_id = await line_platform_service.create_rich_menu(payloads["tools"])
        steps.append({"step": 7, "name": "建立 tools Rich Menu", "ok": True, "detail": {"richMenuId": tools_id}})
    except Exception as e:
        steps.append({"step": 7, "name": "建立 tools Rich Menu", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 8: 上傳 tools 圖片
    try:
        await line_platform_service.upload_rich_menu_image(tools_id, images["tools"])
        steps.append({"step": 8, "name": "上傳 tools 圖片", "ok": True})
    except Exception as e:
        steps.append({"step": 8, "name": "上傳 tools 圖片", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 9: 設定預設 Rich Menu
    try:
        await line_platform_service.set_default_rich_menu(main_id)
        steps.append({"step": 9, "name": "設定預設 Rich Menu", "ok": True})
    except Exception as e:
        steps.append({"step": 9, "name": "設定預設 Rich Menu", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    # Step 10: 建立別名
    try:
        await line_platform_service.create_or_update_alias("santong-main", main_id)
        await line_platform_service.create_or_update_alias("santong-tools", tools_id)
        steps.append({"step": 10, "name": "建立 Rich Menu 別名", "ok": True})
    except Exception as e:
        steps.append({"step": 10, "name": "建立 Rich Menu 別名", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "steps": steps}

    return {
        "ok": True,
        "main_rich_menu_id": main_id,
        "tools_rich_menu_id": tools_id,
        "steps": steps,
    }


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
            "google_drive_worklog_folder_id": settings.google_drive_worklog_folder_id,
            "google_drive_configured": bool(
                settings.google_drive_worklog_folder_id and settings.google_service_account_json
            ),
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
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    base_url = (payload.base_url or settings.public_base_url).rstrip("/")
    print(f"[deploy_rich_menu] 開始佈署，base_url={base_url}")
    try:
        result = await deploy_default_rich_menus(base_url)
        print(f"[deploy_rich_menu] 佈署成功，main_id={result.get('main_rich_menu_id')}, tools_id={result.get('tools_rich_menu_id')}")
    except Exception as exc:
        print(f"[deploy_rich_menu] 佈署失敗: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"佈署失敗: {type(exc).__name__}: {exc}")

    # Existing users can have an explicit old menu assignment, so update only
    # the menu pointer for bound users without touching their LINE identity.
    bound_employees = [
        employee
        for employee in session.exec(select(Employee)).all()
        if employee.line_user_id
    ]
    assignment_errors: list[str] = []
    assigned_count = 0
    for employee in bound_employees:
        try:
            await line_platform_service.link_rich_menu_to_user(
                employee.line_user_id,
                result["main_rich_menu_id"],
            )
            assigned_count += 1
        except LinePlatformError as exc:
            assignment_errors.append(f"{employee.employee_code}: {exc}")

    result["assigned_user_count"] = assigned_count
    result["assignment_errors"] = assignment_errors
    return result


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
