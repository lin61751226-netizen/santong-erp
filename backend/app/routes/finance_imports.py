"""Preview and commit controlled imports from company Excel workbooks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import (
    AdminAuditLog,
    BusinessContact,
    Employee,
    FinanceEntry,
    FinanceImportBatch,
    ManagedDocument,
    Role,
)
from app.schemas import FinanceImportCommitRequest, FinanceImportPreviewRequest
from app.services.finance_import import FinanceWorkbookError, analyze_finance_workbook
from app.services.google_drive import google_drive_worklog_service


router = APIRouter(prefix="/api/finance-imports", tags=["finance-imports"])


def _document_or_404(session: Session, document_id: int) -> ManagedDocument:
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="找不到公司 Excel 文件")
    if not document.original_file_name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="此檔案不是可匯入的 .xlsx 或 .xlsm Excel")
    return document


async def _analyze_document(document: ManagedDocument) -> dict[str, Any]:
    try:
        content = await google_drive_worklog_service.download_file_bytes(document.drive_file_id)
        return analyze_finance_workbook(content, document.original_file_name)
    except FinanceWorkbookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"無法從 Google Drive 讀取 Excel：{exc}") from exc


def _preview_response(document: ManagedDocument, analysis: dict[str, Any]) -> dict[str, Any]:
    contacts = analysis["contact_candidates"]
    finance = analysis["finance_candidates"]
    warnings = analysis["warnings"]
    return {
        "document": {
            "id": document.id,
            "file_name": document.original_file_name,
            "drive_url": document.drive_url,
        },
        "content_sha256": analysis["content_sha256"],
        "summary": {
            "contact_candidates": len(contacts),
            "finance_candidates": len(finance),
            "warning_rows": len(warnings),
        },
        "contacts": contacts[:30],
        "finance_rows": [
            {
                "sheet": item["source_sheet"],
                "row": item["source_row"],
                "date": item["entry_date"].isoformat(),
                "type": item["entry_type"],
                "category": item["category"],
                "counterparty": item["counterparty"],
                "income": item["income_amount"],
                "expense": item["expense_amount"],
                "project_name": item["project_name"],
            }
            for item in finance[:50]
        ],
        "warnings": warnings[:100],
    }


@router.post("/preview")
async def preview_finance_import(
    payload: FinanceImportPreviewRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    document = _document_or_404(session, payload.document_id)
    analysis = await _analyze_document(document)
    return _preview_response(document, analysis)


def _fill_missing(target: BusinessContact, source: dict[str, Any]) -> bool:
    changed = False
    for field in (
        "tax_id", "department", "title", "contact_person", "phone", "mobile", "email", "address", "note"
    ):
        source_value = source.get(field) or None
        if source_value and not getattr(target, field):
            setattr(target, field, source_value)
            changed = True
    if target.category == "未分類" and source["category"] != "未分類":
        target.category = source["category"]
        changed = True
    if changed:
        target.updated_at = datetime.utcnow()
    return changed


def _import_contacts(
    session: Session,
    *,
    document: ManagedDocument,
    batch_id: int,
    contacts: list[dict[str, Any]],
) -> tuple[int, int, int]:
    existing = session.exec(select(BusinessContact)).all()
    by_key = {item.normalized_key: item for item in existing}
    by_tax_id = {item.tax_id: item for item in existing if item.tax_id}
    created = updated = skipped = 0
    for item in contacts:
        contact = by_tax_id.get(item["tax_id"]) if item["tax_id"] else None
        contact = contact or by_key.get(item["normalized_key"])
        if contact is None:
            contact = BusinessContact(
                **{key: item[key] for key in (
                    "normalized_key", "name", "tax_id", "category", "department", "title", "contact_person",
                    "phone", "mobile", "email", "address", "note", "source_sheet", "source_row",
                )},
                source_document_id=document.id,
            )
            session.add(contact)
            by_key[contact.normalized_key] = contact
            if contact.tax_id:
                by_tax_id[contact.tax_id] = contact
            created += 1
            continue
        if _fill_missing(contact, item):
            contact.source_document_id = document.id
            contact.source_sheet = item["source_sheet"]
            contact.source_row = item["source_row"]
            session.add(contact)
            updated += 1
        else:
            skipped += 1
    return created, updated, skipped


def _import_finance(
    session: Session,
    *,
    document: ManagedDocument,
    batch_id: int,
    records: list[dict[str, Any]],
) -> tuple[int, int]:
    imported = skipped_duplicate = 0
    known_keys = {
        item.dedupe_key
        for item in session.exec(select(FinanceEntry)).all()
        if item.dedupe_key
    }
    for item in records:
        if item["dedupe_key"] in known_keys:
            skipped_duplicate += 1
            continue
        entry = FinanceEntry(
            entry_date=item["entry_date"],
            entry_type=item["entry_type"],
            category=item["category"],
            vendor_name=item["counterparty"] or None,
            amount=item["income_amount"] - item["expense_amount"],
            invoice_number=item["voucher_number"] or None,
            invoice_date=item["voucher_date"],
            payment_status=item["transaction_status"] or None,
            payment_method=item["payment_method"] or None,
            handled_by=item["handled_by"] or None,
            note=item["note"] or None,
            summary=item["summary"] or None,
            income_amount=item["income_amount"],
            expense_amount=item["expense_amount"],
            account_name=item["account_name"] or None,
            transaction_status=item["transaction_status"] or None,
            voucher_type=item["voucher_type"] or None,
            tag=item["tag"] or None,
            project_name=item["project_name"],
            source_document_id=document.id,
            source_sheet=item["source_sheet"],
            source_row=item["source_row"],
            source_key=item["source_key"],
            dedupe_key=item["dedupe_key"],
            import_batch_id=batch_id,
        )
        session.add(entry)
        known_keys.add(item["dedupe_key"])
        imported += 1
    return imported, skipped_duplicate


@router.post("/commit", status_code=status.HTTP_201_CREATED)
async def commit_finance_import(
    payload: FinanceImportCommitRequest,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    if not payload.import_contacts and not payload.import_finance:
        raise HTTPException(status_code=400, detail="請至少選擇匯入通訊錄或收支明細")
    document = _document_or_404(session, payload.document_id)
    analysis = await _analyze_document(document)
    if analysis["content_sha256"] != payload.expected_content_sha256:
        raise HTTPException(status_code=409, detail="原始 Excel 已變更，請重新執行匯入預覽")

    prior = session.exec(
        select(FinanceImportBatch).where(
            FinanceImportBatch.managed_document_id == document.id,
            FinanceImportBatch.content_sha256 == analysis["content_sha256"],
            FinanceImportBatch.status == "completed",
        )
    ).first()
    if prior:
        raise HTTPException(status_code=409, detail="此版本已完成匯入；系統已保護既有資料，未重複寫入")

    batch = FinanceImportBatch(
        managed_document_id=document.id,
        source_file_name=document.original_file_name,
        content_sha256=analysis["content_sha256"],
        finance_pending_review=len(analysis["warnings"]),
        warning_rows=analysis["warnings"],
        imported_by_id=actor.id,
    )
    session.add(batch)
    session.flush()

    if payload.import_contacts:
        batch.contact_created, batch.contact_updated, batch.contact_skipped = _import_contacts(
            session, document=document, batch_id=batch.id, contacts=analysis["contact_candidates"]
        )
    if payload.import_finance:
        batch.finance_imported, batch.finance_skipped_duplicate = _import_finance(
            session, document=document, batch_id=batch.id, records=analysis["finance_candidates"]
        )

    session.add(AdminAuditLog(
        actor_id=actor.id,
        actor_code=actor.employee_code,
        actor_name=actor.name,
        action="import",
        entity_type="finance_workbook",
        entity_id=batch.id,
        summary=(
            f"匯入 Excel：{document.original_file_name}｜收支新增 {batch.finance_imported}、"
            f"重複略過 {batch.finance_skipped_duplicate}、待確認 {batch.finance_pending_review}、"
            f"通訊錄新增 {batch.contact_created}/補齊 {batch.contact_updated}"
        ),
    ))
    session.commit()
    session.refresh(batch)

    backup = {"status": "not_run"}
    try:
        backup = await google_drive_worklog_service.backup_database()
    except Exception as exc:
        backup = {"status": "failed", "message": str(exc)}

    return {
        "message": "Excel 資料已寫入系統；待確認列未匯入，請保留在預覽清單人工處理",
        "batch": {
            "id": batch.id,
            "finance_imported": batch.finance_imported,
            "finance_skipped_duplicate": batch.finance_skipped_duplicate,
            "finance_pending_review": batch.finance_pending_review,
            "contact_created": batch.contact_created,
            "contact_updated": batch.contact_updated,
            "contact_skipped": batch.contact_skipped,
        },
        "backup": backup,
    }


@router.get("/batches")
def list_finance_import_batches(
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    rows = session.exec(select(FinanceImportBatch).order_by(FinanceImportBatch.created_at.desc()).limit(50)).all()
    documents = {document.id: document for document in session.exec(select(ManagedDocument)).all()}
    return [
        {
            "id": row.id,
            "file_name": documents.get(row.managed_document_id).original_file_name if documents.get(row.managed_document_id) else row.source_file_name,
            "finance_imported": row.finance_imported,
            "finance_skipped_duplicate": row.finance_skipped_duplicate,
            "finance_pending_review": row.finance_pending_review,
            "contact_created": row.contact_created,
            "contact_updated": row.contact_updated,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/entries")
def list_imported_finance_entries(
    limit: int = 100,
    session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    safe_limit = max(1, min(limit, 500))
    rows = session.exec(
        select(FinanceEntry).order_by(FinanceEntry.entry_date.desc(), FinanceEntry.id.desc()).limit(safe_limit)
    ).all()
    documents = {document.id: document for document in session.exec(select(ManagedDocument)).all()}
    return [
        {
            "id": row.id,
            "entry_date": row.entry_date.isoformat(),
            "entry_type": row.entry_type,
            "category": row.category,
            "summary": row.summary,
            "counterparty": row.vendor_name,
            "income_amount": row.income_amount,
            "expense_amount": row.expense_amount,
            "project_name": row.project_name,
            "payment_status": row.payment_status,
            "source": documents.get(row.source_document_id).original_file_name if documents.get(row.source_document_id) else None,
            "source_sheet": row.source_sheet,
            "source_row": row.source_row,
        }
        for row in rows
    ]
