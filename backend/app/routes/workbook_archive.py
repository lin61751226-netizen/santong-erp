"""Store complete source Excel versions without booking unreviewed money rows."""

import hashlib
from io import BytesIO
from typing import Literal
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, ManagedDocument, Role, WorkbookArchive
from app.services.google_drive import google_drive_worklog_service as drive


router = APIRouter(prefix="/api/workbook-archive", tags=["workbook-archive"])
admin = require_roles(Role.owner, Role.admin)
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024


class ArchiveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: int = Field(gt=0)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_scope: Literal["forklift", "liuhe"]
    source_role: Literal["primary", "reference", "independent"]


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ArchiveItem] = Field(min_length=1, max_length=10)


def inspect_excel(content: bytes) -> list[str]:
    if not content or len(content) > MAX_FILE_BYTES:
        raise HTTPException(422, "Excel 檔案為空或超過 20 MB")
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(part.file_size for part in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(422, "Excel 解壓後超過 150 MB")
            if "[Content_Types].xml" not in archive.namelist() or "xl/workbook.xml" not in archive.namelist():
                raise HTTPException(422, "檔案不是有效的 Excel 工作簿")
            from openpyxl import load_workbook
            workbook = load_workbook(BytesIO(content), read_only=True, data_only=False)
            try:
                return workbook.sheetnames
            finally:
                workbook.close()
    except (BadZipFile, ValueError, OSError) as exc:
        raise HTTPException(422, f"Excel 檔案無法讀取：{exc}") from exc


@router.get("")
def list_archives(session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    rows = session.exec(select(WorkbookArchive).order_by(WorkbookArchive.id.desc())).all()
    return [{"id": row.id, "source_document_id": row.source_document_id,
             "file_name": row.file_name, "ledger_scope": row.ledger_scope,
             "source_role": row.source_role, "content_sha256": row.content_sha256,
             "size_bytes": row.size_bytes, "sheet_count": len(row.sheet_names),
             "created_at": row.created_at.isoformat()} for row in rows]


@router.get("/{archive_id}/download")
def download_archive(archive_id: int, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    row = session.get(WorkbookArchive, archive_id)
    if row is None:
        raise HTTPException(404, "找不到封存的 Excel")
    return Response(content=row.content,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.file_name)}"})


@router.post("")
async def archive_workbooks(payload: ArchiveRequest, session: Session = Depends(get_session),
                            actor: Employee = Depends(admin)):
    if len({item.document_id for item in payload.items}) != len(payload.items):
        raise HTTPException(422, "同一份來源文件不可重複列入")
    prepared = []
    for item in payload.items:
        doc = session.get(ManagedDocument, item.document_id)
        if doc is None or not doc.original_file_name.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(404, "找不到 Excel 來源文件")
        if item.ledger_scope == "liuhe" and ("六和" not in doc.original_file_name or item.source_role != "independent"):
            raise HTTPException(422, "六和來源須作為獨立帳本封存")
        if item.ledger_scope == "forklift" and ("全年管理版" not in doc.original_file_name or item.source_role == "independent"):
            raise HTTPException(422, "堆高機來源須使用全年管理版")
        try:
            content = await drive.download_file_bytes(doc.drive_file_id)
        except Exception as exc:
            raise HTTPException(502, "Google Drive 來源讀取失敗；未寫入資料庫") from exc
        digest = hashlib.sha256(content).hexdigest()
        if digest != item.expected_content_sha256:
            raise HTTPException(409, "來源 Excel 已變更；請重新比對後再封存")
        sheet_names = inspect_excel(content)
        previous = session.exec(select(WorkbookArchive).where(
            WorkbookArchive.source_document_id == doc.id,
            WorkbookArchive.content_sha256 == digest,
        )).first()
        prepared.append((item, doc, content, digest, sheet_names, previous))
    if all(previous is not None for *_, previous in prepared):
        return {"saved": 0, "replayed": len(prepared), "backup": "not_needed"}
    try:
        before = await drive.backup_database()
    except Exception as exc:
        raise HTTPException(503, "正式資料庫備份失敗；未封存 Excel") from exc
    if before.get("status") != "saved":
        raise HTTPException(503, "正式資料庫備份未成功；未封存 Excel")
    saved = 0
    for item, doc, content, digest, sheet_names, previous in prepared:
        if previous is not None:
            continue
        archive = WorkbookArchive(source_document_id=doc.id, content_sha256=digest,
                                  ledger_scope=item.ledger_scope, source_role=item.source_role,
                                  file_name=doc.original_file_name, size_bytes=len(content),
                                  sheet_names=sheet_names, content=content, archived_by_id=actor.id)
        session.add(archive)
        session.flush()
        session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
                                  action="workbook_archive", entity_type="workbook_archive", entity_id=archive.id,
                                  summary=f"封存完整 Excel：{doc.original_file_name}；不修改正式收支"))
        saved += 1
    session.commit()
    try:
        after = await drive.backup_database()
        backup_status = after.get("status", "failed")
    except Exception:
        backup_status = "failed"
    return {"saved": saved, "replayed": len(prepared) - saved, "backup": backup_status}
