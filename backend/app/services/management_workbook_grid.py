"""Read and patch only approved monthly input cells in company workbooks."""

from __future__ import annotations

import calendar
import hashlib
import io
import math
import re
from datetime import date, datetime
from html import escape
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import openpyxl
from openpyxl.utils.datetime import to_excel


class WorkbookGridError(ValueError):
    pass


SHEET_SUFFIX = {"finance": "收支明細表", "payroll": "薪資表", "roster": "排休表"}
FINANCE_FIELDS = [
    ("date", "日期", "A", "date"), ("type", "類型", "B", "text"),
    ("category", "類別", "C", "text"), ("summary", "摘要", "D", "text"),
    ("counterparty", "對象", "E", "text"), ("payment_method", "付款方式", "F", "text"),
    ("voucher", "發票／憑證", "G", "text"), ("income", "收入", "H", "number"),
    ("expense", "支出", "I", "number"), ("handled_by", "經辦人", "L", "text"),
    ("note", "備註", "M", "text"), ("account", "收付帳戶", "O", "text"),
]
PAYROLL_FIELDS = [
    (key, key, openpyxl.utils.get_column_letter(index), "number")
    for index, key in enumerate(("底薪", "職務津貼", "交通/伙食", "加班費", "獎金", "其他加給",
                                 "勞保", "健保", "勞退自提", "請假扣款", "其他扣款"), 6)
] + [("status", "發薪狀態", "T", "text"), ("note", "備註", "U", "text")]


def month_sheet(month: str, kind: str) -> str:
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month) or kind not in SHEET_SUFFIX:
        raise WorkbookGridError("請選擇有效的月份與表格")
    year, number = map(int, month.split("-"))
    return f"{year - 1911:03d}年{number:02d}月_{SHEET_SUFFIX[kind]}"


def fields_for(month: str, kind: str) -> list[tuple[str, str, str, str]]:
    if kind == "finance":
        return FINANCE_FIELDS
    if kind == "payroll":
        return PAYROLL_FIELDS
    days = calendar.monthrange(*map(int, month.split("-")))[1]
    return [(str(day), f"{day}日", openpyxl.utils.get_column_letter(day + 4), "text") for day in range(1, days + 1)] + [("note", "備註", "AP", "text")]


def _cell_value(cell, kind: str):
    value = cell.value
    if value is None:
        return None
    if kind == "date" and isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_month_grid(content: bytes, month: str, kind: str) -> dict:
    name = month_sheet(month, kind)
    if len(content) > 30 * 1024 * 1024:
        raise WorkbookGridError("Excel 超過 30MB")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 150 * 1024 * 1024:
                raise WorkbookGridError("Excel 解壓後超過 150MB")
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=False, data_only=False)
    except (BadZipFile, KeyError, ValueError) as exc:
        raise WorkbookGridError("Excel 檔案損毀或格式不正確") from exc
    try:
        if name not in workbook:
            raise WorkbookGridError(f"這份檔案沒有 {name}，請選其他檔案或月份")
        sheet = workbook[name]
        fields = fields_for(month, kind)
        start = 4 if kind == "finance" else 6
        records = []
        last_occupied = start - 1
        for row_number in range(start, sheet.max_row + 1):
            if kind == "finance":
                occupied = any(sheet[f"{col}{row_number}"].value is not None for _, _, col, _ in fields)
            else:
                occupied = sheet[f"{'C' if kind == 'payroll' else 'B'}{row_number}"].value is not None
            if occupied:
                last_occupied = row_number
            elif kind != "finance":
                continue
            values = {key: _cell_value(sheet[f"{col}{row_number}"], typ) for key, _, col, typ in fields}
            locked = [key for key, _, col, _ in fields if sheet[f"{col}{row_number}"].data_type == "f"]
            records.append({"row_number": row_number, "name": str(sheet[f"{'C' if kind == 'payroll' else 'B'}{row_number}"].value or "") if kind != "finance" else "",
                            "values": values, "locked": locked, "occupied": occupied})
        if kind == "finance":
            records = [row for row in records if row["occupied"] or last_occupied < row["row_number"] <= last_occupied + 10]
        return {"month": month, "kind": kind, "sheet_name": name,
                "fields": [{"key": key, "label": label, "type": typ} for key, label, _, typ in fields],
                "rows": records, "sha256": hashlib.sha256(content).hexdigest()}
    finally:
        workbook.close()


def _normalize(value, typ: str, month: str):
    if value is None or value == "":
        return None
    if typ == "date":
        if not isinstance(value, str):
            raise WorkbookGridError("日期格式不正確")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise WorkbookGridError("日期格式不正確") from exc
        if parsed.strftime("%Y-%m") != month:
            raise WorkbookGridError("日期必須屬於所選月份")
        return parsed.isoformat()
    if typ == "number":
        if isinstance(value, bool):
            raise WorkbookGridError("金額不可為布林值")
        try:
            number = float(value)
        except (ValueError, TypeError) as exc:
            raise WorkbookGridError("金額必須是數字") from exc
        if not math.isfinite(number) or number < 0 or number > 1_000_000_000:
            raise WorkbookGridError("金額須為 0 至十億之間")
        return int(number) if number.is_integer() else round(number, 2)
    if not isinstance(value, str) or len(value) > 500:
        raise WorkbookGridError("文字內容過長或格式不正確")
    value = value.strip()
    if value.startswith(("=", "+", "-", "@")):
        raise WorkbookGridError("文字不可使用公式開頭")
    return value or None


def prepare_changes(content: bytes, month: str, kind: str, incoming: list[dict]) -> tuple[list[dict], bytes]:
    grid = read_month_grid(content, month, kind)
    rows = {row["row_number"]: row for row in grid["rows"]}
    definitions = {key: (col, typ) for key, _, col, typ in fields_for(month, kind)}
    changes = []
    patches = {}
    seen = set()
    for edit in incoming:
        row_number = edit.get("row_number")
        values = edit.get("values")
        if row_number in seen or row_number not in rows or not isinstance(values, dict):
            raise WorkbookGridError("重複或不允許的來源列")
        seen.add(row_number)
        row = rows[row_number]
        before, after = {}, {}
        for key, value in values.items():
            if key not in definitions or key in row["locked"]:
                raise WorkbookGridError(f"{key} 是公式或非資料欄，不能修改")
            col, typ = definitions[key]
            normalized = _normalize(value, typ, month)
            if normalized != row["values"][key]:
                before[key] = row["values"][key]
                after[key] = normalized
                patches[f"{col}{row_number}"] = (normalized, typ)
        if before:
            changes.append({"row_number": row_number, "before": before, "after": after})
    if kind == "finance":
        for change in changes:
            row = rows[change["row_number"]]
            merged = {**row["values"], **change["after"]}
            if any(merged.get(key) for key in ("type", "category", "summary", "income", "expense")) and not merged.get("date"):
                raise WorkbookGridError("收支列有內容時必須填日期")
            if merged.get("income") and merged.get("expense"):
                raise WorkbookGridError("同一列不可同時填收入與支出")
    if not changes:
        return [], content
    return changes, patch_sheet_cells(content, grid["sheet_name"], patches)


def patch_sheet_cells(content: bytes, sheet_name: str, patches: dict[str, tuple[object, str]]) -> bytes:
    with ZipFile(io.BytesIO(content)) as archive:
        files = {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    workbook = ElementTree.fromstring(files["xl/workbook.xml"][1])
    rels = ElementTree.fromstring(files["xl/_rels/workbook.xml.rels"][1])
    targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall(f"{{{pkg_ns}}}Relationship")}
    path = None
    for sheet in workbook.findall(f".//{{{main_ns}}}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            target = targets.get(sheet.attrib.get(f"{{{rel_ns}}}id"))
            if target:
                path = target.lstrip("/") if target.startswith("/") else str(PurePosixPath("xl") / target)
            break
    if not path or path not in files:
        raise WorkbookGridError("找不到對應 Excel 工作表 XML")
    xml = files[path][1].decode("utf-8")
    for coord, (value, typ) in patches.items():
        pattern = re.compile(r'<c\b(?P<attrs>[^>]*\br="' + re.escape(coord) + r'"[^>]*?)(?:>(?P<body>.*?)</c>|/>)', re.S)
        match = pattern.search(xml)
        if not match or (match.group("body") and "<f" in match.group("body")):
            raise WorkbookGridError(f"{coord} 不是可寫入的資料格")
        attrs = re.sub(r'\s+t="[^"]*"', "", match.group("attrs")).rstrip("/")
        if value is None:
            replacement = f"<c{attrs}/>"
        elif typ == "text":
            replacement = f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
        else:
            number = to_excel(date.fromisoformat(value)) if typ == "date" else value
            replacement = f"<c{attrs}><v>{number}</v></c>"
        xml = xml[:match.start()] + replacement + xml[match.end():]
    files[path] = (files[path][0], xml.encode("utf-8"))
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for info, data in files.values():
            archive.writestr(info, data)
    result = output.getvalue()
    # Re-open the changed sheet before offering the workbook for upload.
    period = f"{int(sheet_name[:3]) + 1911}-{sheet_name[4:6]}"
    kind = next(key for key, suffix in SHEET_SUFFIX.items() if sheet_name.endswith(suffix))
    checked = read_month_grid(result, period, kind)
    if checked["sheet_name"] != sheet_name:
        raise WorkbookGridError("Excel 更新後驗證失敗")
    return result
