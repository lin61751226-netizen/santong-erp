"""Admin-only routes for the triplicate invoice overprint workbench."""

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
from app.services.invoice_print import (
    FIELD_KEYS,
    PrintDraft,
    PrintLayout,
    calculate,
    real_invoice_fields,
)

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


def _normalize_legacy(content: dict) -> dict:
    """Read-only view of an old layout that fills missing fields from real-form defaults.

    Never writes the revision; unknown fields (e.g. the removed 單位 column) are
    hidden and new fields (買受人地址) appear at their real-form coordinates.
    """
    if not isinstance(content, dict) or not isinstance(content.get("fields"), dict):
        return content
    defaults = real_invoice_fields()
    stored = content["fields"]
    normalized = {key: (stored[key] if key in stored else defaults[key].model_dump())
                  for key in FIELD_KEYS}
    view = {**content, "fields": normalized}
    # Earliest revisions used an implicit 190 x 105 mm sheet; never resize them.
    view.setdefault("paper_width", 190)
    view.setdefault("paper_height", 105)
    view.setdefault("feed_mode", PrintLayout().feed_mode)
    view.setdefault("tax_id_spacing", 6.0)
    view.setdefault("row_pitch", 6.1)
    return view


def _audit(session: Session, actor: Employee, action: str, detail: str):
    session.add(AdminAuditLog(
        actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action=action, entity_type="invoiceprintrevision", summary=detail))


def _layout_view(session: Session) -> dict:
    revision, content = _load_content(session, "layout", LAYOUT_RECORD_KEY)
    if content is None:
        return {"revision": 0, "content": PrintLayout().model_dump()}
    return {"revision": revision, "content": _normalize_legacy(content)}


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


@router.get("/invoice-overprint", response_class=HTMLResponse)
def invoice_overprint_page(request: Request, actor: Employee = Depends(admin)):
    response = templates.TemplateResponse("invoice_overprint.html", {"request": request})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/invoice-print/template")
def get_template(response: Response, session: Session = Depends(get_session),
                 actor: Employee = Depends(admin)):
    response.headers["Cache-Control"] = "no-store"
    return _layout_view(session)


@router.put("/api/invoice-print/template")
async def save_template(payload: dict, session: Session = Depends(get_session),
                        actor: Employee = Depends(admin)):
    try:
        content = PrintLayout(**payload.get("content", {})).model_dump()
        expected_revision = int(payload.get("expected_revision"))
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(422, detail=f"定位內容不正確：{exc}") from exc
    return await _save_revision(
        session, actor, "layout", LAYOUT_RECORD_KEY, content,
        expected_revision, "invoice_print_layout_save",
    )


@router.get("/api/invoice-print/sample-layout")
def sample_layout(response: Response, actor: Employee = Depends(admin)):
    """Real-form 195 x 103 mm coordinates, measured from the actual sample."""
    response.headers["Cache-Control"] = "no-store"
    return PrintLayout().model_dump()


@router.post("/api/invoice-print/calculate")
def calculate_draft(payload: dict, actor: Employee = Depends(admin)):
    try:
        draft = PrintDraft(**payload)
        return calculate(draft)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post("/api/invoice-print/drafts")
async def save_draft(payload: dict, session: Session = Depends(get_session),
                     actor: Employee = Depends(admin)):
    record_key = (payload.get("record_key") or "").strip()
    if not record_key or len(record_key) > 80:
        raise HTTPException(422, detail="草稿鍵不正確")
    try:
        draft = PrintDraft(**payload.get("content", {})).model_dump()
        expected_revision = int(payload.get("expected_revision"))
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return await _save_revision(
        session, actor, "draft", record_key, draft,
        expected_revision, "invoice_print_draft_save",
    )


@router.get("/api/invoice-print/drafts")
def list_drafts(limit: int = DRAFT_LIMIT, offset: int = 0,
                session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    limit = max(1, min(limit, DRAFT_LIMIT))
    offset = max(0, offset)
    heads = (
        select(func.max(InvoicePrintRevision.id))
        .where(InvoicePrintRevision.kind == "draft")
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


@router.post("/api/invoice-print/backup")
async def retry_backup(actor: Employee = Depends(admin)):
    try:
        return await drive.backup_database()
    except Exception as exc:
        raise HTTPException(500, detail=f"備份失敗：{exc}") from exc
