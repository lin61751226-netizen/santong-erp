"""Authenticated overprint calibration and append-only drafts."""
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, InvoicePrintRevision, Role
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.invoice_print import PrintDraft, PrintLayout, StrictModel, calculate


def no_cache(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["invoice-print"], dependencies=[Depends(no_cache)])
admin = require_roles(Role.owner, Role.admin)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


class LayoutSave(StrictModel):
    expected_revision: int = Field(ge=0)
    content: PrintLayout


class DraftSave(StrictModel):
    expected_revision: int = Field(ge=0)
    record_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{1,64}$")]
    content: PrintDraft


def latest(session, kind, key):
    return session.exec(select(InvoicePrintRevision).where(
        InvoicePrintRevision.kind == kind, InvoicePrintRevision.record_key == key
    ).order_by(InvoicePrintRevision.revision.desc())).first()


def encode(row):
    return {"record_key": row.record_key, "revision": row.revision, "content": row.content,
            "created_at": row.created_at.isoformat() + "Z"}


async def backup():
    try:
        result = await drive.backup_database()
        return {"status": result.get("status", "failed")}
    except Exception:
        return {"status": "failed"}


async def save(session, actor, kind, key, expected, content):
    previous = latest(session, kind, key)
    revision = previous.revision if previous else 0
    if revision != expected:
        raise HTTPException(409, "已被其他人更新，請先重新載入；本次未覆蓋")
    row = InvoicePrintRevision(kind=kind, record_key=key, revision=revision + 1,
        content=content, created_by_id=actor.id)
    session.add(row)
    session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action=f"invoice_print_{kind}_save", entity_type="invoiceprintrevision",
        summary=f"套印{kind} {key} 版本{revision + 1}；保留歷史版本，不改計價或帳目"))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "同時保存衝突，請重新載入；本次未覆蓋") from exc
    session.refresh(row)
    return {**encode(row), "backup": await backup()}


@router.get("/invoice-overprint")
def page(request: Request, actor: Employee = Depends(admin)):
    return templates.TemplateResponse("invoice_overprint.html", {"request": request}, headers={"Cache-Control": "no-store"})


@router.get("/api/invoice-print/template")
def template(session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    row = latest(session, "layout", "shared")
    return encode(row) if row else {"record_key": "shared", "revision": 0, "content": PrintLayout().model_dump()}


@router.put("/api/invoice-print/template")
async def save_template(payload: LayoutSave, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    return await save(session, actor, "layout", "shared", payload.expected_revision, payload.content.model_dump(mode="json"))


@router.post("/api/invoice-print/calculate")
def preview(payload: PrintDraft, actor: Employee = Depends(admin)):
    try:
        return calculate(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/invoice-print/drafts")
def drafts(offset: int = Query(0, ge=0), session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    heads = select(func.max(InvoicePrintRevision.id)).where(InvoicePrintRevision.kind == "draft").group_by(InvoicePrintRevision.record_key)
    rows = session.exec(select(InvoicePrintRevision).where(InvoicePrintRevision.id.in_(heads))
        .order_by(InvoicePrintRevision.id.desc()).offset(offset).limit(51)).all()
    return {"items": [encode(row) for row in rows[:50]], "has_more": len(rows) > 50}


@router.post("/api/invoice-print/drafts")
async def save_draft(payload: DraftSave, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    try:
        calculate(payload.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await save(session, actor, "draft", payload.record_key, payload.expected_revision, payload.content.model_dump(mode="json"))


@router.post("/api/invoice-print/backup")
async def retry_backup(actor: Employee = Depends(admin)):
    return await backup()
