"""工作日誌草稿、簽單預填與計價核對。僅 owner／admin，回應不快取。"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import get_session
from app.deps import require_roles
from app.models import (
    AiBillingCheck,
    AiJournalDraft,
    AiSignSlipDraft,
    Employee,
    Role,
)
from app.services.ai_ops import (
    OpsGuard,
    confirm_sign_slip_draft,
    create_billing_check,
    create_journal_draft,
    create_sign_slip_draft,
    serialize_billing,
    serialize_journal,
    serialize_sign_slip_draft,
    set_journal_status,
    update_journal_draft,
    update_sign_slip_draft,
)
from app.services.google_drive import google_drive_worklog_service


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/api/ai-ops",
    tags=["ai-ops"],
    dependencies=[Depends(private_response)],
)
admin = require_roles(Role.owner, Role.admin)


class JournalDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_date: date
    worksite_id: int = Field(gt=0)
    open_new: bool = False


class JournalEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_summary: str = ""
    workers: list[str] = Field(default_factory=list)
    equipment: list[str] = Field(default_factory=list)
    work_items: list[str] = Field(default_factory=list)
    quantities: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    normal_hours: float = Field(ge=0, le=10000)
    overtime_hours: float = Field(ge=0, le=10000)
    support_hours: float = Field(ge=0, le=10000)


class SignSlipDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_date: date
    worksite_id: int = Field(gt=0)
    document_id: int | None = Field(default=None, gt=0)
    open_new: bool = False


class SignSlipEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_name: str | None = None
    work_content: str = ""
    driver_names: str | None = None
    normal_hours: float = Field(ge=0, le=10000)
    overtime_hours: float = Field(ge=0, le=10000)
    support_hours: float = Field(ge=0, le=10000)
    start_time: str | None = None
    end_time: str | None = None
    amount: int | None = Field(default=None, ge=0)
    vehicles: dict = Field(default_factory=dict)


class SignSlipConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slip_no: str = Field(min_length=1, max_length=40)
    acknowledge_uncertainties: bool = False


class BillingCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    document_id: int | None = Field(default=None, gt=0)


def _guard(exc: OpsGuard):
    from fastapi import HTTPException
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _backup() -> str:
    backup = await google_drive_worklog_service.backup_database()
    return backup.get("status", "failed")


@router.get("/status")
def ai_ops_status(actor: Employee = Depends(admin)) -> dict:
    del actor
    return {
        "ai_ops_enabled": settings.ai_ops_enabled,
        "ai_ops_schedule_enabled": settings.ai_ops_schedule_enabled,
        "schedule_time": f"{settings.ai_ops_schedule_hour:02d}:{settings.ai_ops_schedule_minute:02d}",
        "model": settings.openai_model,
        "has_api_key": bool(settings.openai_api_key.strip()),
    }


@router.get("/journals/drafts")
def get_journal_draft(
    work_date: date,
    worksite_id: int = Query(gt=0),
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    del actor
    latest = session.exec(
        select(AiJournalDraft).where(
            AiJournalDraft.work_date == work_date,
            AiJournalDraft.worksite_id == worksite_id,
        ).order_by(AiJournalDraft.id.desc())
    ).first()
    approved = session.exec(
        select(AiJournalDraft).where(
            AiJournalDraft.work_date == work_date,
            AiJournalDraft.worksite_id == worksite_id,
            AiJournalDraft.status == "approved",
        ).order_by(AiJournalDraft.id.desc())
    ).first()
    return {
        "draft": None if latest is None else serialize_journal(latest),
        "approved": None if approved is None or (latest and approved.id == latest.id) else serialize_journal(approved),
    }


@router.post("/journals/drafts")
async def post_journal_draft(
    payload: JournalDraftRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft = await create_journal_draft(
            session, actor, payload.work_date, payload.worksite_id, open_new=payload.open_new,
        )
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "工作日誌草稿已保存。尚未核准，也沒有寫入計價工時。",
        "draft": serialize_journal(draft),
        "backup_status": await _backup(),
    }


@router.put("/journals/drafts/{draft_id}")
async def put_journal_draft(
    draft_id: int,
    payload: JournalEditRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft = update_journal_draft(session, actor, draft_id, payload.model_dump())
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "草稿已更新。原始派工、打卡與計價工時沒有改。",
        "draft": serialize_journal(draft),
        "backup_status": await _backup(),
    }


@router.post("/journals/drafts/{draft_id}/approve")
async def approve_journal_draft(
    draft_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft = set_journal_status(session, actor, draft_id, "approved")
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "草稿已核准。沒有改派工、打卡、簽單或計價工時；若要寫入計價請用既有的儲存工時。",
        "draft": serialize_journal(draft),
        "backup_status": await _backup(),
    }


@router.post("/journals/drafts/{draft_id}/reject")
async def reject_journal_draft(
    draft_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft = set_journal_status(session, actor, draft_id, "rejected")
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "草稿已退回，原始資料沒有刪除。",
        "draft": serialize_journal(draft),
        "backup_status": await _backup(),
    }


@router.get("/sign-slips/drafts")
def get_sign_slip_draft(
    work_date: date,
    worksite_id: int = Query(gt=0),
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    del actor
    latest = session.exec(
        select(AiSignSlipDraft).where(
            AiSignSlipDraft.work_date == work_date,
            AiSignSlipDraft.worksite_id == worksite_id,
        ).order_by(AiSignSlipDraft.id.desc())
    ).first()
    return {"draft": None if latest is None else serialize_sign_slip_draft(latest)}


@router.post("/sign-slips/drafts")
async def post_sign_slip_draft(
    payload: SignSlipDraftRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft = await create_sign_slip_draft(
            session, actor, payload.work_date, payload.worksite_id,
            document_id=payload.document_id, open_new=payload.open_new,
        )
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "簽單預填已保存。尚未建立正式簽單。",
        "draft": serialize_sign_slip_draft(draft),
        "backup_status": await _backup(),
    }


@router.put("/sign-slips/drafts/{draft_id}")
async def put_sign_slip_draft(
    draft_id: int,
    payload: SignSlipEditRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft = update_sign_slip_draft(session, actor, draft_id, payload.model_dump())
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "簽單草稿已更新，尚未建立正式簽單。",
        "draft": serialize_sign_slip_draft(draft),
        "backup_status": await _backup(),
    }


@router.post("/sign-slips/drafts/{draft_id}/confirm")
async def post_confirm_sign_slip(
    draft_id: int,
    payload: SignSlipConfirmRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        draft, slip = confirm_sign_slip_draft(
            session, actor, draft_id, payload.slip_no, payload.acknowledge_uncertainties,
        )
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": f"已新增簽單 {slip.slip_no}。沒有修改其他簽單或計價檔。",
        "draft": serialize_sign_slip_draft(draft),
        "sign_slip_id": slip.id,
        "slip_no": slip.slip_no,
        "backup_status": await _backup(),
    }


@router.get("/billing/checks")
def get_billing_check(
    month: str,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    del actor
    latest = session.exec(
        select(AiBillingCheck).where(AiBillingCheck.month == month).order_by(AiBillingCheck.id.desc())
    ).first()
    return {"check": None if latest is None else serialize_billing(latest)}


@router.post("/billing/checks")
async def post_billing_check(
    payload: BillingCheckRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(admin),
) -> dict:
    try:
        item = await create_billing_check(session, actor, payload.month, payload.document_id)
    except OpsGuard as exc:
        _guard(exc)
    return {
        "message": "計價核對報告已保存。沒有修改工時、金額或計價檔。",
        "check": serialize_billing(item),
        "backup_status": await _backup(),
    }
