"""Read-only parsing for company contact and cash-ledger Excel workbooks."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from io import BytesIO
from typing import Any

import openpyxl


CONTACT_CATEGORY_VALUES = {"業主", "合作夥伴", "供應商", "客戶", "廠商"}
FINANCE_SHEET_PATTERN = re.compile(r"^\d{3}年\d{2}月_收支明細表$")


class FinanceWorkbookError(ValueError):
    pass


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _normalized_name(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def contact_key(*, name: str, tax_id: str | None) -> str:
    return f"tax:{tax_id}" if tax_id else f"name:{_normalized_name(name)}"


def finance_dedupe_key(record: dict[str, Any]) -> str:
    values = (
        record["entry_date"].isoformat(),
        record["entry_type"],
        record["category"],
        record["summary"],
        record["counterparty"],
        f"{record['income_amount']:.2f}",
        f"{record['expense_amount']:.2f}",
        record["voucher_number"],
        record["payment_method"],
    )
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def _is_bank_only_contact(name: str, row_values: list[str]) -> bool:
    joined = "|".join([name, *row_values])
    return any(marker in joined for marker in ("分行", "帳號", "合庫", "銀行帳戶"))


def _parse_contacts(workbook) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if "公司通訊錄" not in workbook.sheetnames:
        return [], []
    sheet = workbook["公司通訊錄"]
    contacts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    header_email_column = _text(sheet.cell(3, 9).value).lower()
    uses_shifted_contact_layout = "email" in header_email_column

    # These workbooks share columns A:H. Two generations use a different
    # I:M layout, so the later fields are resolved from the row values below.
    for row in range(4, sheet.max_row + 1):
        name = _text(sheet.cell(row, 2).value)
        if not name:
            continue
        row_values = [_text(sheet.cell(row, column).value) for column in range(1, min(sheet.max_column, 14) + 1)]
        if _is_bank_only_contact(name, row_values):
            warnings.append({
                "scope": "contact",
                "sheet": sheet.title,
                "row": row,
                "reason": "疑似付款帳號資料，未匯入一般通訊錄",
            })
            continue

        tax_id = re.sub(r"\D", "", _text(sheet.cell(row, 3).value)) or None
        department = _text(sheet.cell(row, 4).value) or None
        title = _text(sheet.cell(row, 5).value) or None
        phone = _text(sheet.cell(row, 6).value) or None
        col_g = _text(sheet.cell(row, 7).value)
        col_h = _text(sheet.cell(row, 8).value)
        col_i = _text(sheet.cell(row, 9).value)
        col_j = _text(sheet.cell(row, 10).value)
        col_k = _text(sheet.cell(row, 11).value)
        col_l = _text(sheet.cell(row, 12).value)

        # The Liuhe workbook retains an older layout: I is address and J is
        # category even though its header was later shifted one column right.
        legacy_shifted = uses_shifted_contact_layout and col_j in CONTACT_CATEGORY_VALUES
        if legacy_shifted:
            address, category, contact_person, note = col_i, col_j, col_k, col_l
            mobile = col_h or col_g
            email = ""
        else:
            address, category, contact_person, note = col_i, col_j, col_k, col_l
            mobile = col_g
            email = col_h if "@" in col_h else ""

        extra_contacts = []
        if col_g and col_g != mobile:
            extra_contacts.append(col_g)
        if col_h and col_h != mobile and "@" not in col_h:
            extra_contacts.append(col_h)
        if extra_contacts:
            note = "；".join(filter(None, [note, f"其他聯絡：{'、'.join(extra_contacts)}"]))

        contacts.append({
            "name": name,
            "tax_id": tax_id,
            "normalized_key": contact_key(name=name, tax_id=tax_id),
            "category": category or "未分類",
            "department": department,
            "title": title,
            "contact_person": contact_person or None,
            "phone": phone,
            "mobile": mobile or None,
            "email": email or None,
            "address": address or None,
            "note": note or None,
            "source_sheet": sheet.title,
            "source_row": row,
        })
    return contacts, warnings


def _normalize_entry_type(raw_type: str) -> str | None:
    if raw_type in {"收款", "收入"}:
        return "收入"
    if raw_type in {"付款", "支出"}:
        return "支出"
    if raw_type == "轉帳":
        return "轉帳"
    return None


def _parse_finance_sheets(workbook, *, file_name: str, document_hash: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    project_name = "六和" if "六和" in file_name else None

    for sheet in workbook.worksheets:
        if not FINANCE_SHEET_PATTERN.match(sheet.title):
            continue
        for row in range(4, sheet.max_row + 1):
            entry_date = _date_value(sheet.cell(row, 1).value)
            if entry_date is None:
                continue
            raw_type = _text(sheet.cell(row, 2).value)
            category = _text(sheet.cell(row, 3).value)
            income_amount = _number(sheet.cell(row, 8).value)
            expense_amount = _number(sheet.cell(row, 9).value)
            base_warning = {"scope": "finance", "sheet": sheet.title, "row": row, "date": entry_date.isoformat()}

            if not income_amount and not expense_amount:
                warnings.append({**base_warning, "reason": "已有日期但未填收入或支出金額"})
                continue
            if income_amount and expense_amount:
                warnings.append({**base_warning, "reason": "同列同時有收入與支出，需確認是否為手續費或拆分交易"})
                continue

            entry_type = _normalize_entry_type(raw_type)
            if entry_type is None:
                warnings.append({**base_warning, "reason": "未填或無法辨識收支類型"})
                continue
            if entry_type == "轉帳":
                warnings.append({**base_warning, "reason": "轉帳缺少來源與目的帳戶，需人工確認"})
                continue
            if not category:
                warnings.append({**base_warning, "reason": "未填收支類別"})
                continue

            record = {
                "entry_date": entry_date,
                "entry_type": entry_type,
                "category": category,
                "summary": _text(sheet.cell(row, 4).value),
                "counterparty": _text(sheet.cell(row, 5).value),
                "payment_method": _text(sheet.cell(row, 6).value),
                "voucher_number": _text(sheet.cell(row, 7).value),
                "income_amount": income_amount,
                "expense_amount": expense_amount,
                "handled_by": _text(sheet.cell(row, 12).value),
                "note": _text(sheet.cell(row, 13).value),
                "account_name": _text(sheet.cell(row, 15).value),
                "transaction_status": _text(sheet.cell(row, 16).value),
                "voucher_date": _date_value(sheet.cell(row, 17).value),
                "voucher_type": _text(sheet.cell(row, 20).value),
                "tag": _text(sheet.cell(row, 21).value),
                "project_name": project_name,
                "source_sheet": sheet.title,
                "source_row": row,
                "source_key": f"{document_hash}:{sheet.title}:{row}",
            }
            record["dedupe_key"] = finance_dedupe_key(record)
            records.append(record)
    return records, warnings


def analyze_finance_workbook(content: bytes, file_name: str) -> dict[str, Any]:
    if not file_name.lower().endswith((".xlsx", ".xlsm")):
        raise FinanceWorkbookError("目前只支援 .xlsx 或 .xlsm 格式的資料匯入")
    try:
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=False, read_only=False)
    except Exception as exc:
        raise FinanceWorkbookError(f"無法讀取 Excel：{exc}") from exc

    try:
        document_hash = content_sha256(content)
        contacts, contact_warnings = _parse_contacts(workbook)
        finance_records, finance_warnings = _parse_finance_sheets(
            workbook, file_name=file_name, document_hash=document_hash
        )
        warnings = [*contact_warnings, *finance_warnings]
        return {
            "content_sha256": document_hash,
            "contact_candidates": contacts,
            "finance_candidates": finance_records,
            "warnings": warnings,
            "sheets": workbook.sheetnames,
        }
    finally:
        workbook.close()
