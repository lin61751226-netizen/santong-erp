from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.db import init_db, session_scope
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.forklift import router as forklift_router
from app.routes.line_management import api_router as line_management_api_router
from app.routes.line_management import page_router as line_management_page_router
from app.routes.line_webhook import router as line_router
from app.services.bootstrap import seed_demo_data
from app.services.google_drive import google_drive_worklog_service
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.line_platform import deploy_default_rich_menus


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as session:
        seed_demo_data(session)
        try:
            restore_result = await google_drive_worklog_service.restore_line_bindings(session)
            if restore_result.get("status") in {"restored", "not_found"}:
                await google_drive_worklog_service.backup_line_bindings(session)
        except Exception as exc:
            logger.warning("LINE binding Drive sync failed during startup: %s", exc)
    start_scheduler()

    # 服務啟動時自動佈署 Rich Menu（確保按鈕配置為最新版本）
    if settings.environment == "production" and settings.line_channel_access_token:
        try:
            logger.info("啟動時自動佈署 Rich Menu...")
            result = await deploy_default_rich_menus(settings.public_base_url.rstrip("/"))
            logger.info(
                "Rich Menu 自動佈署成功：main=%s, tools=%s",
                result.get("main_rich_menu_id"),
                result.get("tools_rich_menu_id"),
            )
        except Exception as exc:
            logger.error("Rich Menu 自動佈署失敗：%s: %s", type(exc).__name__, exc, exc_info=True)

    yield
    stop_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(line_router)
app.include_router(line_management_api_router)
app.include_router(line_management_page_router)
app.include_router(forklift_router)


@app.get("/", response_class=HTMLResponse)
def admin_home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "default_actor_code": settings.default_actor_code,
        },
    )


@app.get("/forklift", response_class=HTMLResponse)
def forklift_home(request: Request):
    return templates.TemplateResponse(
        "forklift.html",
        {
            "request": request,
            "app_name": settings.app_name,
        },
    )


@app.get("/health")
def health():
    return {"ok": True, "app": settings.app_name}
