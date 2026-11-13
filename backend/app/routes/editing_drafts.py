"""Account-scoped editor recovery with optimistic, append-only revisions."""
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import EditingDraftRevision, Employee, Role
from app.services.google_drive import google_drive_worklog_service

router = APIRouter(prefix="/api/editing-drafts", tags=["editing-drafts"])
Kind = Literal["sign-slip", "cost-grid"]
SIGN_FIELDS = {
    "signSlipId", "signSlipNo", "signSlipDate", "signSlipCustomer", "signSlipSiteCode",
    "signSlipLocation", "signSlipV25", "signSlipV30", "signSlipV45", "signSlipTruck",
    "signSlipNormal", "signSlipOvertime", "signSlipTotal", "signSlipStart", "signSlipEnd",
    "signSlipAmount", "signSlipDrivers", "signSlipSignature", "signSlipContent",
    "signSlipSource", "signSlipNotes",
}


class DraftWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    active: bool = True
    payload: dict = Field(default_factory=dict)


def latest(session, actor_id, kind):
    return session.exec(select(EditingDraftRevision).where(
        EditingDraftRevision.actor_id == actor_id, EditingDraftRevision.kind == kind,
    ).order_by(EditingDraftRevision.revision.desc()).limit(1)).first()


def serialize(item):
    return {"revision": item.revision if item else 0,
            "active": item.is_active if item else False,
            "payload": item.payload if item and item.is_active else {},
            "saved_at": item.created_at.isoformat() if item else None}


def validate_payload(kind, payload):
    try:
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 150_000:
            raise ValueError()
        allowed = ({"fields", "vehicle_hours", "base_updated_at"} if kind == "sign-slip" else
                   {"document_id", "year", "month", "label", "sha256", "edits", "source_slip", "reason", "adjustment"})
        if set(payload) - allowed:
            raise ValueError()
        if kind == "sign-slip":
            fields = payload.get("fields")
            if not isinstance(fields, dict) or set(fields) - SIGN_FIELDS:
                raise ValueError()
            if any(not isinstance(v, str) or len(v) > 30_000 for v in fields.values()):
                raise ValueError()
        else:
            if not isinstance(payload.get("document_id"), int) or not isinstance(payload.get("edits"), list):
                raise ValueError()
            if len(payload["edits"]) > 31 or len(str(payload.get("sha256", ""))) != 64:
                raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, "草稿欄位或大小不正確；未修改正式資料。")


@router.get("/{kind}")
def read_draft(kind: Kind, response: Response, session: Session = Depends(get_session),
               actor: Employee = Depends(require_roles(Role.owner, Role.admin))):
    response.headers["Cache-Control"] = "no-store"
    return serialize(latest(session, actor.id, kind))


@router.put("/{kind}")
def save_draft(kind: Kind, payload: DraftWrite, response: Response,
               session: Session = Depends(get_session),
               actor: Employee = Depends(require_roles(Role.owner, Role.admin))):
    response.headers["Cache-Control"] = "no-store"
    item = latest(session, actor.id, kind)
    if payload.expected_revision != (item.revision if item else 0):
        raise HTTPException(409, "另一分頁已更新草稿，請重新載入草稿後再處理。")
    if payload.active:
        validate_payload(kind, payload.payload)
    if item and item.is_active == payload.active and item.payload == (payload.payload if payload.active else {}):
        return {**serialize(item), "backup_status": "pending"}
    revision = EditingDraftRevision(actor_id=actor.id, kind=kind,
        revision=payload.expected_revision + 1, is_active=payload.active,
        payload=payload.payload if payload.active else {})
    session.add(revision)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "另一分頁已更新草稿，未覆蓋；請重新載入。")
    session.refresh(revision)
    return {**serialize(revision), "backup_status": "pending"}


@router.post("/backup/cloud")
async def backup_drafts(actor: Employee = Depends(require_roles(Role.owner, Role.admin))):
    result = await google_drive_worklog_service.backup_database()
    return {"backup_status": result.get("status", "failed")}
