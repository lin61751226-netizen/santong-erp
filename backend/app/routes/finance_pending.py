"""Stage unclassified Excel transactions without changing the booked ledger."""

from datetime import date, datetime
from io import BytesIO
import re

from fastapi import APIRouter, Depends, HTTPException
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, FinanceEntry, FinancePendingReview, Role, WorkbookArchive
from app.services.finance_import import finance_dedupe_key
from app.services.google_drive import google_drive_worklog_service as drive


router = APIRouter(prefix="/api/finance-pending", tags=["finance-pending"])
admin = require_roles(Role.owner, Role.admin)


class StageRequest(BaseModel):
    archive_id: int = Field(gt=0)
    month: str = Field(pattern=r"^20\d\d-(0[1-9]|1[0-2])$")


class CategoryRequest(BaseModel):
    category: str = Field(min_length=1, max_length=80)


def _row_data(row):
    values = [cell.value for cell in row]
    values.extend([None] * max(0, 9 - len(values)))
    return values


def _stage_candidates(archive: WorkbookArchive, month: str):
    year, number = map(int, month.split("-"))
    sheet_name = f"{year - 1911:03d}年{number:02d}月_收支明細表"
    workbook = load_workbook(BytesIO(archive.content), read_only=True, data_only=True)
    try:
        if sheet_name not in workbook:
            raise HTTPException(404, "原始 Excel 沒有此月份收支表")
        sheet = workbook[sheet_name]
        candidates = []
        for row_number, row in enumerate(sheet.iter_rows(min_row=4), start=4):
            values = _row_data(row)
            raw_date = values[0]
            entry_date = raw_date.date() if isinstance(raw_date, datetime) else raw_date
            if not isinstance(entry_date, date) or (entry_date.year, entry_date.month) != (year, number):
                continue
            entry_type = str(values[1] or "").strip()
            if entry_type not in {"收入", "支出", "收款", "付款"} or str(values[2] or "").strip():
                continue
            income = values[7] if isinstance(values[7], (float, int)) and not isinstance(values[7], bool) else 0
            expense = values[8] if isinstance(values[8], (float, int)) and not isinstance(values[8], bool) else 0
            if (income > 0) == (expense > 0):
                continue
            if (entry_type in {"收入", "收款"}) != (income > 0):
                continue
            candidates.append(FinancePendingReview(
                archive_id=archive.id, source_sheet=sheet_name, source_row=row_number,
                entry_date=entry_date, entry_type="收入" if income else "支出",
                summary=str(values[3] or "").strip() or None,
                counterparty=str(values[4] or "").strip() or None,
                payment_method=str(values[5] or "").strip() or None,
                income_amount=float(income), expense_amount=float(expense),
            ))
        return candidates
    finally:
        workbook.close()


def _serialize(row: FinancePendingReview):
    return {
        "id": row.id, "archive_id": row.archive_id, "source_sheet": row.source_sheet,
        "source_row": row.source_row, "date": row.entry_date.isoformat(),
        "type": row.entry_type, "category": row.category,
        "summary": row.summary, "counterparty": row.counterparty,
        "payment_method": row.payment_method, "income": row.income_amount,
        "expense": row.expense_amount, "status": row.status,
    }


@router.get("")
def list_pending(month: str = "2026-10", session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "月份格式錯誤")
    return [_serialize(row) for row in session.exec(
        select(FinancePendingReview).where(
            FinancePendingReview.entry_date >= date.fromisoformat(month + "-01"),
            FinancePendingReview.entry_date < date(int(month[:4]) + (month[5:] == "12"), int(month[5:]) % 12 + 1, 1),
        ).order_by(FinancePendingReview.entry_date, FinancePendingReview.source_row)
    ).all()]


@router.post("/stage")
async def stage_pending(payload: StageRequest, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    archive = session.get(WorkbookArchive, payload.archive_id)
    if archive is None or archive.ledger_scope != "liuhe" or archive.source_role != "independent":
        raise HTTPException(404, "找不到六和獨立原始檔")
    candidates = _stage_candidates(archive, payload.month)
    if not candidates:
        raise HTTPException(422, "此月份沒有可待分類的交易")
    existing = {(row.archive_id, row.source_sheet, row.source_row) for row in session.exec(
        select(FinancePendingReview).where(FinancePendingReview.archive_id == archive.id)
    ).all()}
    new_rows = [row for row in candidates if (row.archive_id, row.source_sheet, row.source_row) not in existing]
    if new_rows:
        backup = await drive.backup_database()
        if backup.get("status") != "saved":
            raise HTTPException(503, "正式資料庫備份未成功，未建立待確認清單")
        for row in new_rows:
            session.add(row)
        session.add(AdminAuditLog(
            actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
            action="stage", entity_type="finance_pending", entity_id=archive.id,
            summary=f"六和 {payload.month} 待分類 {len(new_rows)} 筆；未入正式帳",
        ))
        session.commit()
        try:
            after = await drive.backup_database()
            backup_status = after.get("status", "failed")
        except Exception:
            backup_status = "failed"
    else:
        backup_status = "not_needed"
    return {"staged": len(new_rows), "existing": len(candidates) - len(new_rows), "month": payload.month,
            "backup": backup_status}


@router.patch("/{pending_id}/category")
async def set_category(pending_id: int, payload: CategoryRequest, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    row = session.get(FinancePendingReview, pending_id)
    if row is None:
        raise HTTPException(404, "找不到待確認交易")
    if row.status != "pending":
        raise HTTPException(409, "此交易已入帳，不可在待確認清單修改")
    category = payload.category.strip()
    if not category or category == "待分類":
        raise HTTPException(422, "請填寫實際收支類別")
    row.category = category
    session.add(row)
    session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
                              action="classify", entity_type="finance_pending", entity_id=row.id,
                              summary=f"六和 {row.entry_date} 原表第 {row.source_row} 列設定類別：{category}"))
    session.commit()
    try:
        await drive.backup_database()
    except Exception:
        pass
    return _serialize(row)


@router.post("/{pending_id}/book")
async def book_pending(pending_id: int, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    row = session.get(FinancePendingReview, pending_id)
    if row is None:
        raise HTTPException(404, "找不到待確認交易")
    if row.status != "pending" or not row.category:
        raise HTTPException(409, "請先補類別，且此交易不可重複入帳")
    archive = session.get(WorkbookArchive, row.archive_id)
    if archive is None:
        raise HTTPException(409, "找不到對應原始檔")
    source_key = f"{archive.content_sha256}:{row.source_sheet}:{row.source_row}"
    if session.exec(select(FinanceEntry).where(FinanceEntry.source_key == source_key)).first():
        raise HTTPException(409, "此 Excel 來源列已入帳")
    record = {"entry_date": row.entry_date, "entry_type": row.entry_type,
              "category": row.category, "summary": row.summary or "",
              "counterparty": row.counterparty or "", "income_amount": row.income_amount,
              "expense_amount": row.expense_amount, "voucher_number": "",
              "payment_method": row.payment_method or ""}
    dedupe_key = finance_dedupe_key(record)
    # Separate source rows can be legitimate equal payments; only the same row is a replay.
    backup = await drive.backup_database()
    if backup.get("status") != "saved":
        raise HTTPException(503, "正式資料庫備份未成功，未入帳")
    entry = FinanceEntry(entry_date=row.entry_date, entry_type=row.entry_type,
                         category=row.category, vendor_name=row.counterparty,
                         amount=row.income_amount - row.expense_amount,
                         summary=row.summary, income_amount=row.income_amount,
                         expense_amount=row.expense_amount, payment_method=row.payment_method,
                         project_name="六和", source_document_id=archive.source_document_id,
                         source_sheet=row.source_sheet, source_row=row.source_row,
                         source_key=source_key, dedupe_key=dedupe_key)
    session.add(entry)
    session.flush()
    row.status = "booked"
    row.finance_entry_id = entry.id
    session.add(row)
    session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
                              action="book", entity_type="finance_pending", entity_id=row.id,
                              summary=f"六和 {row.entry_date} 原表第 {row.source_row} 列入帳 {row.category} {entry.amount}"))
    session.commit()
    try:
        after = await drive.backup_database()
        backup_status = after.get("status", "failed")
    except Exception:
        backup_status = "failed"
    return {"finance_entry_id": entry.id, "status": "booked", "backup": backup_status}
