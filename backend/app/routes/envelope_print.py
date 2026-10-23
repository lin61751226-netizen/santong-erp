"""Admin-only routes for the vertical envelope (直式信封) overprint workbench."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, InvoicePrintRevision, Role
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.envelope_print import EnvelopeDraft, EnvelopeLayout, render_lines

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
admin = require_roles(Role.owner, Role.admin)
LAYOUT_RECORD_KEY = "shared"
DRAFT_LIMIT = 50


def _load_content(session: Session, kind: str, record_key: str):
    row = session.exec(
        select(InvoicePrintRevision)
        .where(
            InvoicePrintRevision.kind == kind,
            InvoicePrintRevision.record_key == record_key,
        )
        .order_by(InvoicePrintRevision.revision.desc())
    ).first()
    return (row.revision, row.content) if row else (0, None)


def _audit(session: Session, actor: Employee, action: str, detail: str):
    session.add(AdminAuditLog(
        actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action=action, entity_type="invoiceprintrevision", summary=detail))


def _layout_view(session: Session) -> dict:
    revision, content = _load_content(session, "envelope_layout", LAYOUT_RECORD_KEY)
    if content is None:
        return {"revision": 0, "content": EnvelopeLayout().model_dump()}
    return {"revision": revision, "content": content}


async def _save_revision(session: Session, actor: Employee, kind: str, record_key: str,
                         content: dict, expected_revision: int, audit_action: str) -> dict:
    current_revision, _ = _load_content(session, kind, record_key)
    if expected_revision != current_revision:
        raise HTTPException(409, detail="版本已改變，請重新載入後再儲存")
    row = InvoicePrintRevision(
        kind=kind,
        record_key=record_key,
        revision=current_revision + 1,
        content=content,
        created_by_id=actor.id,
    )
    session.add(row)
    _audit(session, actor, audit_action, f"{kind}:{record_key} 修訂為第 {current_revision + 1} 版")
    session.commit()
    try:
        backup = await drive.backup_database()
    except Exception:
        backup = {"status": "failed", "message": "資料庫已保存，Google Drive 備份失敗"}
    return {"revision": row.revision, "backup": backup}


@router.get("/envelope-print", response_class=HTMLResponse)
def envelope_print_page(request: Request, actor: Employee = Depends(admin)):
    response = templates.TemplateResponse("envelope_overprint.html", {"request": request})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/envelope-print/template")
def get_template(response: Response, session: Session = Depends(get_session),
                 actor: Employee = Depends(admin)):
    response.headers["Cache-Control"] = "no-store"
    return _layout_view(session)


@router.put("/api/envelope-print/template")
async def save_template(payload: dict, session: Session = Depends(get_session),
                        actor: Employee = Depends(admin)):
    try:
        content = EnvelopeLayout(**payload.get("content", {})).model_dump()
        expected_revision = int(payload.get("expected_revision"))
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(422, detail=f"定位內容不正確：{exc}") from exc
    return await _save_revision(
        session, actor, "envelope_layout", LAYOUT_RECORD_KEY, content,
        expected_revision, "envelope_print_layout_save",
    )


@router.get("/api/envelope-print/sample-layout")
def sample_layout(response: Response, actor: Employee = Depends(admin)):
    response.headers["Cache-Control"] = "no-store"
    return EnvelopeLayout().model_dump()


@router.post("/api/envelope-print/validate")
def validate_draft(payload: dict, actor: Employee = Depends(admin)):
    try:
        draft = EnvelopeDraft(**payload)
        return {"ok": True, "lines": render_lines(draft)}
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post("/api/envelope-print/drafts")
async def save_draft(payload: dict, session: Session = Depends(get_session),
                     actor: Employee = Depends(admin)):
    record_key = (payload.get("record_key") or "").strip()
    if not record_key or len(record_key) > 80:
        raise HTTPException(422, detail="草稿鍵不正確")
    try:
        draft = EnvelopeDraft(**payload.get("content", {})).model_dump()
        expected_revision = int(payload.get("expected_revision"))
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return await _save_revision(
        session, actor, "envelope_draft", record_key, draft,
        expected_revision, "envelope_print_draft_save",
    )


@router.get("/api/envelope-print/drafts")
def list_drafts(limit: int = DRAFT_LIMIT, offset: int = 0,
                session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    limit = max(1, min(limit, DRAFT_LIMIT))
    offset = max(0, offset)
    heads = (
        select(func.max(InvoicePrintRevision.id))
        .where(InvoicePrintRevision.kind == "envelope_draft")
        .group_by(InvoicePrintRevision.record_key)
    )
    rows = session.exec(
        select(InvoicePrintRevision)
        .where(InvoicePrintRevision.id.in_(heads))
        .order_by(InvoicePrintRevision.id.desc())
        .offset(offset).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    items = [
        {
            "record_key": row.record_key,
            "revision": row.revision,
            "content": row.content,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows[:limit]
    ]
    return {"items": items, "has_more": has_more, "limit": limit, "offset": offset}


@router.post("/api/envelope-print/backup")
async def retry_backup(actor: Employee = Depends(admin)):
    try:
        return await drive.backup_database()
    except Exception as exc:
        raise HTTPException(500, detail=f"備份失敗：{exc}") from exc
