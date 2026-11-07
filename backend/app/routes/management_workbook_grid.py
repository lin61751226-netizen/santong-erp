"""Direct monthly editing of approved cells in the uploaded company Excel files."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.core.config import settings
from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, ManagedDocument, Role
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.management_workbook_grid import WorkbookGridError, prepare_changes, read_month_grid


router = APIRouter(prefix="/api/management-workbook-grid", tags=["management-workbook-grid"])
admin = require_roles(Role.owner, Role.admin)
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class RowEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_number: int = Field(ge=1)
    values: dict[str, str | int | float | None]


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: int = Field(gt=0)
    month: str
    kind: Literal["finance", "payroll", "roster"]
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: list[RowEdit] = Field(min_length=1, max_length=200)
    preview_only: bool = True
    confirmed: bool = False


def source_document(session: Session, document_id: int) -> ManagedDocument:
    document = session.get(ManagedDocument, document_id)
    if not document or document.category not in {"通訊錄與年度管理", "收支明細"} or not document.original_file_name.lower().endswith(".xlsx"):
        raise HTTPException(404, "找不到可直接編輯的公司 Excel 原檔")
    return document


async def download(document: ManagedDocument) -> bytes:
    try:
        return await drive.download_file_bytes(document.drive_file_id)
    except Exception as exc:
        raise HTTPException(502, "無法讀取 Google Drive Excel；請稍後再試") from exc


@router.get("")
async def get_grid(
    document_id: int = Query(ge=1), month: str = Query(...),
    kind: Literal["finance", "payroll", "roster"] = Query(...),
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    document = source_document(session, document_id)
    content = await download(document)
    try:
        grid = read_month_grid(content, month, kind)
    except WorkbookGridError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**grid, "document_id": document.id, "document_name": document.original_file_name}


@router.post("")
async def save_grid(
    payload: SaveRequest,
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    document = source_document(session, payload.document_id)
    content = await download(document)
    if hashlib.sha256(content).hexdigest() != payload.expected_sha256:
        raise HTTPException(409, "Excel 來源檔已變更；請重新載入並核對後再保存")
    try:
        changes, revised = prepare_changes(content, payload.month, payload.kind,
                                           [row.model_dump() for row in payload.rows])
    except WorkbookGridError as exc:
        raise HTTPException(400, str(exc)) from exc
    if payload.preview_only or not changes:
        return {"saved": 0, "changed_rows": len(changes), "changes": changes,
                "message": "僅預覽，尚未寫入" if payload.preview_only else "沒有變更，未建立新版本"}
    if not payload.confirmed:
        raise HTTPException(409, "請先預覽差異並確認，才可保存新版本")

    timestamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S")
    file_name = f"{Path(document.original_file_name).stem}_線上編輯_{timestamp}_{document.id}.xlsx"
    try:
        uploaded = await drive.upload_management_document(
            file_name=file_name, content=revised, content_type=CONTENT_TYPE, public_share=False,
        )
    except Exception as exc:
        raise HTTPException(502, "Google Drive 上傳失敗；原檔與資料庫未變更") from exc

    version = ManagedDocument(
        category=document.category, title=f"{document.title} 線上編輯版",
        original_file_name=file_name, stored_file_name=uploaded.file_name,
        drive_file_id=uploaded.file_id, drive_folder_id=uploaded.folder_id,
        drive_url=uploaded.file_url, content_type=CONTENT_TYPE,
        size_bytes=len(revised), uploaded_by_id=actor.id,
    )
    try:
        session.add(version)
        session.flush()
        for change in changes:
            session.add(AdminAuditLog(
                actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
                action="management_workbook_grid_edit", entity_type="managed_document",
                entity_id=version.id,
                summary=json.dumps({"source_document_id": document.id, "sheet": f"{payload.month}/{payload.kind}",
                                    **change}, ensure_ascii=False),
            ))
        session.commit()
        session.refresh(version)
    except Exception as exc:
        session.rollback()
        raise HTTPException(503, "新 Excel 已上傳 Drive，但索引保存失敗；請先查文件庫，勿重複送出") from exc
    try:
        backup = await drive.backup_database()
        backup_status = backup.get("status", "failed")
    except Exception:
        backup_status = "failed"
    return {"saved": len(changes), "document_id": version.id, "document_name": file_name,
            "drive_url": version.drive_url, "backup_status": backup_status,
            "source_document_id": document.id}
