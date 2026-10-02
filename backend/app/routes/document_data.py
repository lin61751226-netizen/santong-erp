"""Authenticated Excel-derived business data editing and versioned exports."""

import asyncio
from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import (
    AdminAuditLog, BusinessContact, DocumentDataExport, DocumentDataRevision,
    Employee, FinanceEntry, ManagedDocument, Role,
)
from app.schemas_document_data import (
    ContactValues, DocumentDataExportRequest, DocumentDataSave, FinanceValues,
)
from app.services.document_data import FIELDS, MODELS, digest, export_xlsx, serialize, snapshot
from app.services.finance_import import contact_key, finance_dedupe_key
from app.services.google_drive import google_drive_worklog_service as drive


router = APIRouter(prefix="/api/document-data", tags=["document-data"])
admin = require_roles(Role.owner, Role.admin)
Kind = Literal["finance", "contacts"]


def document_or_404(session: Session, document_id: int):
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise HTTPException(404, "找不到來源文件")
    if document.category not in {"通訊錄與年度管理", "收支明細"}:
        raise HTTPException(400, "此入口只處理公司通訊錄與收支明細；計價請使用工時匯入")
    return document


def records(session: Session, document_id: int, kind: str):
    model = MODELS[kind]
    return session.exec(select(model).where(model.source_document_id == document_id).order_by(model.id)).all()


async def backup_result():
    try:
        result = await drive.backup_database()
        return {"status": result.get("status", "failed")}
    except Exception:
        return {"status": "failed"}


def audit(session, actor, action, entity_id, summary):
    session.add(AdminAuditLog(
        actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action=action, entity_type="document_data", entity_id=entity_id, summary=summary,
    ))


@router.get("/{document_id}")
def list_data(
    document_id: int, kind: Kind = "finance", sheet: str = "", keyword: str = "",
    offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    document = document_or_404(session, document_id)
    rows = records(session, document_id, kind)
    dataset_hash = digest([snapshot(row) for row in rows])
    sheets = sorted({row.source_sheet or "線上新增" for row in rows})
    selected = [row for row in rows if not sheet or (row.source_sheet or "線上新增") == sheet]
    if keyword.strip():
        needle = keyword.strip().casefold()
        selected = [row for row in selected if any(
            needle in str(getattr(row, key) or "").casefold() for key, _, _ in FIELDS[kind]
        )]
    history = session.exec(select(DocumentDataRevision).where(
        DocumentDataRevision.source_document_id == document_id,
        DocumentDataRevision.data_kind == kind,
    ).order_by(DocumentDataRevision.id.desc()).limit(10)).all()
    exports = session.exec(select(DocumentDataExport).where(
        DocumentDataExport.source_document_id == document_id,
        DocumentDataExport.data_kind == kind,
    ).order_by(DocumentDataExport.id.desc()).limit(10)).all()
    exported_docs = {item.exported_document_id: session.get(ManagedDocument, item.exported_document_id) for item in exports}
    return {
        "document": {"id": document.id, "name": document.original_file_name},
        "kind": kind, "fields": [{"key": key, "label": label, "type": typ} for key, label, typ in FIELDS[kind]],
        "rows": [serialize(row, kind) for row in selected[offset:offset + limit]],
        "total": len(selected), "dataset_total": len(rows), "offset": offset,
        "sheets": sheets, "dataset_hash": dataset_hash,
        "history": [{"id": item.id, "record_id": item.record_id, "created_at": item.created_at.isoformat(),
                     "actor_id": item.actor_id, "action": "修改" if item.before_data else "新增",
                     "changed_fields": [key for key, _, _ in FIELDS[kind]
                                        if item.before_data.get(key) != item.after_data.get(key)]} for item in history],
        "exports": [{"id": item.id, "created_at": item.created_at.isoformat(),
                     "name": exported_docs[item.exported_document_id].original_file_name,
                     "url": exported_docs[item.exported_document_id].drive_url} for item in exports
                    if exported_docs[item.exported_document_id]],
    }


def validate_values(payload: DocumentDataSave):
    try:
        schema = FinanceValues if payload.kind == "finance" else ContactValues
        values = schema.model_validate(payload.values).model_dump()
    except ValidationError as exc:
        raise HTTPException(422, "；".join(error["msg"] for error in exc.errors())) from exc
    if payload.kind == "finance":
        values["amount"] = round(values["income_amount"] - values["expense_amount"], 2)
        values["transaction_status"] = values["payment_status"]
        values["dedupe_key"] = finance_dedupe_key({
            **values, "counterparty": values["vendor_name"], "voucher_number": values["invoice_number"],
        })
    else:
        values["normalized_key"] = contact_key(name=values["name"], tax_id=values["tax_id"])
    # Preserve nullable model semantics, while required names/categories remain text.
    for key in values:
        if values[key] == "" and key not in {"name", "category"}:
            values[key] = None
    return values


@router.post("/{document_id}/save")
async def save_data(
    document_id: int, payload: DocumentDataSave,
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    document_or_404(session, document_id)
    request_hash = digest({"document_id": document_id, "actor_id": actor.id, **payload.model_dump(mode="json")})
    prior = session.exec(select(DocumentDataRevision).where(
        DocumentDataRevision.request_id == str(payload.request_id)
    )).first()
    if prior:
        if prior.payload_hash != request_hash:
            raise HTTPException(409, "此存檔識別碼已使用，內容不同，請重新載入")
        return {"saved": True, "replayed": True, "revision_id": prior.id,
                "record_id": prior.record_id, "backup": await backup_result()}
    values = validate_values(payload)
    model = MODELS[payload.kind]
    row = session.get(model, payload.record_id) if payload.record_id else None
    if payload.record_id and (row is None or row.source_document_id != document_id):
        raise HTTPException(404, "找不到此來源文件的資料")
    before = snapshot(row) if row else {}
    if row and digest(before) != payload.expected_revision:
        raise HTTPException(409, "資料已由其他人修改，請重新載入後再保存；本次未覆蓋")
    if not row and payload.expected_revision:
        raise HTTPException(400, "新增資料不應帶有舊版本")
    if row and all(getattr(row, key) == value for key, value in values.items()):
        return {"saved": True, "unchanged": True, "record_id": row.id, "backup": await backup_result()}
    unique_field = "dedupe_key" if payload.kind == "finance" else "normalized_key"
    duplicate = session.exec(select(model).where(getattr(model, unique_field) == values[unique_field])).first()
    if duplicate and (not row or duplicate.id != row.id):
        raise HTTPException(409, "已有相同交易或公司資料，請開啟原紀錄修改")
    # Keep historical identities reserved, not just their current edited keys.
    revisions = session.exec(select(DocumentDataRevision).where(DocumentDataRevision.data_kind == payload.kind)).all()
    if any(item.record_id != payload.record_id and any(
        data.get(unique_field) == values[unique_field] for data in (item.before_data, item.after_data)
    ) for item in revisions):
        raise HTTPException(409, "相同交易或公司已存在歷史紀錄，請修改原紀錄")
    if payload.kind == "contacts":
        values["updated_at"] = datetime.utcnow()
    try:
        if row:
            # A conditional UPDATE closes the race between checking a token and writing.
            conditions = [model.id == row.id]
            for column in model.__table__.columns:
                if column.name != "attachment_urls":
                    conditions.append(getattr(model, column.name) == getattr(row, column.name))
            result = session.execute(update(model).where(*conditions).values(**values).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                session.rollback()
                raise HTTPException(409, "資料已更新，請重新載入；本次未覆蓋")
            session.expire(row)
            session.refresh(row)
        else:
            row = model(**values, source_document_id=document_id, source_sheet="線上新增")
            session.add(row)
            session.flush()
        revision = DocumentDataRevision(
            request_id=str(payload.request_id), payload_hash=request_hash,
            source_document_id=document_id, data_kind=payload.kind, record_id=row.id,
            actor_id=actor.id, before_data=before, after_data=snapshot(row),
        )
        session.add(revision)
        audit(session, actor, "update" if before else "create", row.id,
              f"線上{'修改' if before else '新增'} {'收支' if payload.kind == 'finance' else '通訊錄'} ID {row.id}，來源文件 {document_id}")
        session.commit()
        session.refresh(revision)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "重複資料或同時存檔衝突，請重新載入確認") from exc
    return {"saved": True, "record_id": row.id, "revision_id": revision.id, "backup": await backup_result()}


@router.post("/{document_id}/export")
async def export_data(
    document_id: int, payload: DocumentDataExportRequest,
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    document = document_or_404(session, document_id)
    rows = records(session, document_id, payload.kind)
    dataset_hash = digest([snapshot(row) for row in rows])
    if dataset_hash != payload.expected_dataset:
        raise HTTPException(409, "資料已變更，請重新載入後匯出")
    if not rows:
        raise HTTPException(400, "沒有可匯出的資料，請先匯入或新增")
    file_name = f"{document.title[:80]}_線上資料匯出_{payload.kind}_{uuid4().hex[:12]}.xlsx"
    content = await asyncio.to_thread(export_xlsx, rows, payload.kind)
    try:
        uploaded = await drive.upload_management_document(
            file_name=file_name, content=content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            public_share=False,
        )
    except Exception as exc:
        raise HTTPException(502, "Excel 匯出未完成：Google Drive 上傳失敗；資料庫與原始 Excel 未變更") from exc
    exported = ManagedDocument(
        category="線上資料匯出", title=file_name[:-5], original_file_name=file_name,
        stored_file_name=uploaded.file_name, drive_file_id=uploaded.file_id,
        drive_folder_id=uploaded.folder_id, drive_url=uploaded.file_url,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=len(content), uploaded_by_id=actor.id,
    )
    try:
        session.add(exported)
        session.flush()
        version = DocumentDataExport(source_document_id=document_id, data_kind=payload.kind,
                                     dataset_hash=dataset_hash, exported_document_id=exported.id, created_by_id=actor.id)
        session.add(version)
        audit(session, actor, "export", exported.id, f"匯出線上資料 {len(rows)} 筆，來源文件 {document_id}，保留原檔")
        session.commit()
        session.refresh(exported)
    except Exception as exc:
        session.rollback()
        raise HTTPException(503, "檔案已上傳 Drive，但版本索引保存失敗；請先重新整理文件庫，不要重複匯出") from exc
    return {"exported": True, "count": len(rows), "file_name": file_name,
            "url": exported.drive_url, "backup": await backup_result()}


@router.post("/{document_id}/backup")
async def retry_backup(
    document_id: int, session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    document_or_404(session, document_id)
    return {"backup": await backup_result()}
