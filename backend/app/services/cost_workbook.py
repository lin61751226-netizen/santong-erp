"""推高機計價活頁簿的讀寫規則與計價引擎。

計價工時表的日期在 A 欄，正常／加班／支援工時分別在 B、D、F 欄，金額
C／E／G 欄由活頁簿公式計算。本模組同時在 Python 端重現「參數」工作表的
計價規則，讓後端在匯入當下就能算出每日與整月費用，不必等 Excel 開檔重算
（openpyxl 寫入後不會刷新公式快取）。

支援兩種匯入：
- 單一日期＋單一標別：``import_cost_hours``
- 整月批次：``import_month_hours``，在同一個活頁簿物件上連續寫入，只存檔一次
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import openpyxl


SHEET_PATTERN = re.compile(r"^(?P<period>\d{5})-(?P<label>.+)$")
HOUR_COLUMNS = {"normal_hours": "B", "overtime_hours": "D", "support_hours": "F"}

# 工作日誌車輛噸位辨識（與前端 index.html 的 signSlipVehicleTypes 完全一致）。
TONNAGE_PATTERNS = {
    "twoPointFive": r"2\s*[.．點]\s*5",
    "threePointZero": r"3\s*[.．點]\s*0",
    "fourPointFive": r"4\s*[.．點]\s*5",
}
TONNAGE_ORDER = ("twoPointFive", "threePointZero", "fourPointFive")

_PARAM_SHEET_NAME = "參數"
_PARAM_LABEL_ROW = 21   # 各標別表頭（B21=45、C21=53……）
_PARAM_RATE_ROWS = {
    "normal_hourly": 22,   # 正常時薪
    "daily": 23,           # 日薪 8H
    "ot_rate": 24,         # 超時費 / HR
    "holiday_rate": 25,    # 假日加班 / HR
    "support_rate": 26,    # 司機支援 / HR
    "rent": 27,            # 月租金
    "rent_start_month": 28,  # Rent 開始月份（1-12）
}


class CostWorkbookError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostSheetTarget:
    worksheet_name: str
    label: str
    row_number: int
    normal_hours: float | int | None
    overtime_hours: float | int | None
    support_hours: float | int | None
    is_holiday: bool = False
    sheet_holiday: bool = False
    c_formula: Optional[str] = None
    e_formula: Optional[str] = None
    g_formula: Optional[str] = None


@dataclass(frozen=True)
class LabelRates:
    """單一標別在「參數」工作表的計價單價。"""

    label: str
    column: str
    normal_hourly: float
    daily: float
    ot_rate: float
    holiday_rate: float
    support_rate: float
    rent: float = 0.0
    rent_start_month: Optional[int] = None


@dataclass(frozen=True)
class PricingParameters:
    tax_rate: float
    safety_rate: float
    labels: dict[str, LabelRates]
    cells: dict[str, float] = field(default_factory=dict)

    def rates_for(self, label: str) -> LabelRates:
        try:
            return self.labels[label]
        except KeyError as exc:
            raise CostWorkbookError(f"參數表找不到標別「{label}」的單價設定") from exc


@dataclass(frozen=True)
class MonthRow:
    """工時表中某個標別某一天的日期列與現有工時。"""

    label: str
    worksheet_name: str
    row_number: int
    day: date
    normal_hours: float | int | None
    overtime_hours: float | int | None
    support_hours: float | int | None
    is_holiday: bool
    c_formula: Optional[str] = None
    e_formula: Optional[str] = None
    g_formula: Optional[str] = None
    sheet_holiday: bool = False


@dataclass(frozen=True)
class MonthCostData:
    period: str
    parameters: PricingParameters
    sheets: dict[str, list[MonthRow]]


@dataclass(frozen=True)
class DayCost:
    normal_amount: float
    overtime_amount: float
    support_amount: float

    @property
    def total(self) -> float:
        return self.normal_amount + self.overtime_amount + self.support_amount


@dataclass(frozen=True)
class MonthHourUpdate:
    label: str
    day: date
    normal_hours: float
    # 加班／支援在工作日誌沒有結構化來源，預設 None 代表保留活頁簿現值。
    overtime_hours: Optional[float] = None
    support_hours: Optional[float] = None
    worksite_id: Optional[int] = None
    forklift_count: int = 0
    force: bool = False


@dataclass
class MonthWriteResult:
    label: str
    worksheet_name: str
    day: date
    row_number: Optional[int] = None
    action: str = "pending"          # written / skipped_exists / no_worksheet / row_not_found
    forklift_count: int = 0
    previous_normal: Optional[float] = None
    previous_overtime: Optional[float] = None
    previous_support: Optional[float] = None
    normal_hours: Optional[float] = None
    overtime_hours: Optional[float] = None
    support_hours: Optional[float] = None
    is_holiday: bool = False
    cost: Optional[DayCost] = None


# ---------------------------------------------------------------------------
# 活頁簿載入與基礎工具
# ---------------------------------------------------------------------------

def roc_period(target_date: date) -> str:
    """將西元日期轉成計價表使用的民國年月，例如 2026-09 -> 11509。"""
    return f"{target_date.year - 1911:03d}{target_date.month:02d}"


def _load_workbook(content: bytes, file_name: str, *, read_only: bool):
    if not content:
        raise CostWorkbookError("計價檔內容為空白")
    return openpyxl.load_workbook(
        io.BytesIO(content),
        read_only=read_only,
        data_only=False,
        keep_vba=Path(file_name).suffix.lower() == ".xlsm",
    )


def _as_date(value: object) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _number(value: object) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _clean_number(value: object) -> Optional[float]:
    """把儲存格值轉成數字，空白／None／空字串回傳 None。"""
    number = _number(value)
    return float(number) if number is not None else None


def _store_value(value: Optional[float]):
    """寫入活頁簿前，整數去掉 .0，其餘保留浮點。"""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else float(value)


def _openpyxl_column_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index)


# ---------------------------------------------------------------------------
# 假日判定：解析每日 E 欄加班金額公式引用參數表第 24（平日超時）或 25（假日）列
# ---------------------------------------------------------------------------

_PARAM_REF = re.compile(r"參數!\$?[A-Z]{1,3}\$?(\d+)")

# 加班假日費率的規則：週日或當年度國定假日使用第 25 列，
# 其餘週一至週六使用第 24 列。國定假日表採政府公告放假日，包含補假與連假中的放假日；
# 民間企業若有不同勞資約定，日後只需調整這份表，不必改計價引擎。
# Python date.weekday()：週一 0 … 週六 5、週日 6。
HOLIDAY_WEEKDAYS = {6}


def _date_span(start: date, end: date) -> frozenset[date]:
    return frozenset(start + timedelta(days=offset) for offset in range((end - start).days + 1))


# 2026（民國 115）政府行政機關辦公日曆表公告放假日。
# 這裡保留日期表而不是把「週六」視為假日，避免一般週六誤用第 25 列。
NATIONAL_HOLIDAYS_BY_YEAR: dict[int, frozenset[date]] = {
    2026: frozenset({date(2026, 1, 1)})
    | _date_span(date(2026, 2, 14), date(2026, 2, 22))
    | _date_span(date(2026, 2, 27), date(2026, 3, 1))
    | _date_span(date(2026, 4, 3), date(2026, 4, 6))
    | _date_span(date(2026, 5, 1), date(2026, 5, 3))
    | _date_span(date(2026, 6, 19), date(2026, 6, 21))
    | _date_span(date(2026, 9, 25), date(2026, 9, 28))
    | _date_span(date(2026, 10, 9), date(2026, 10, 11))
    | _date_span(date(2026, 10, 24), date(2026, 10, 26))
    | _date_span(date(2026, 12, 25), date(2026, 12, 27)),
}


def national_holidays_for_year(year: int) -> frozenset[date]:
    return NATIONAL_HOLIDAYS_BY_YEAR.get(year, frozenset())


def holiday_reason(target_date: date) -> str:
    reasons: list[str] = []
    if target_date.weekday() in HOLIDAY_WEEKDAYS:
        reasons.append("週日")
    if target_date in national_holidays_for_year(target_date.year):
        reasons.append("國定假日")
    return "、".join(reasons)


def is_canonical_holiday(target_date: date) -> bool:
    """系統計價規則：週日或國定假日適用假日加班費率。"""
    return bool(holiday_reason(target_date))


def normalize_overtime_formula(formula: Optional[str], is_holiday: bool) -> Optional[str]:
    """把 E 欄加班公式引用的參數列（24 平日／25 假日）統一到假日規則。

    活頁簿舊公式可能把一般週六寫死引用第 25 列；預覽試算時以此函式對齊規則，
    寫入時另會實際把非假日儲存格的公式由 25 改為 24。
    """
    if not formula:
        return formula
    rate_row = 25 if is_holiday else 24
    other_row = 24 if is_holiday else 25

    def replace_rate(match: re.Match) -> str:
        column = match.group(1)
        row = int(match.group(2))
        return f"參數!${column}${rate_row}" if row == other_row else match.group(0)

    return _PARAM_REF_FULL.sub(replace_rate, formula)


def normalize_normal_formula(formula: Optional[str]) -> Optional[str]:
    """把 C 欄正常工時公式改為「每 8 小時一個日薪」的計價方式。

    只改寫可辨識的標準計價公式；自訂公式（例如人工公式）保留原樣。
    """
    if not formula:
        return formula
    row_match = re.search(r"\$?B\$?(\d+)", formula)
    rate_columns: dict[int, str] = {}
    for rate_row in (22, 23, 24):
        match = re.search(rf"參數!\$?([A-Z]{{1,3}})\$?{rate_row}\b", formula)
        if not match:
            return formula
        rate_columns[rate_row] = match.group(1)
    if not row_match:
        return formula
    row = row_match.group(1)
    prefix = "=" if formula.startswith("=") else ""
    return (
        f'{prefix}IF(B{row}="","",IF(B{row}<8,B{row}*參數!${rate_columns[22]}$22,'
        f'INT(B{row}/8)*參數!${rate_columns[23]}$23+MOD(B{row},8)*參數!${rate_columns[24]}$24))'
    )


def _row_is_holiday(worksheet, row_number: int) -> bool:
    formula = worksheet.cell(row=row_number, column=5).value  # E 欄
    if not isinstance(formula, str):
        return False
    rows = {int(match) for match in _PARAM_REF.findall(formula)}
    return 25 in rows


def _formula_text(value: object) -> Optional[str]:
    """取儲存格公式本體（去掉開頭 =）；非公式回傳 None。"""
    if isinstance(value, str) and value.startswith("="):
        return value[1:]
    return None


# ---------------------------------------------------------------------------
# 參數表讀取
# ---------------------------------------------------------------------------

def _to_float(value: object, default: float = 0.0) -> float:
    number = _number(value)
    return float(number) if number is not None else default


def read_pricing_parameters_from_workbook(workbook) -> PricingParameters:
    if _PARAM_SHEET_NAME not in workbook.sheetnames:
        raise CostWorkbookError("計價檔缺少「參數」工作表，無法讀取計價單價")
    ws = workbook[_PARAM_SHEET_NAME]
    tax_rate = _to_float(ws["B4"].value, 0.05)
    safety_rate = _to_float(ws["B5"].value, 0.006)

    # 建立參數表全部數值儲存格對照（例如 C22 -> 800），供公式求值使用。
    cells: dict[str, float] = {}
    for row in ws.iter_rows():
        for cell in row:
            number = _number(cell.value)
            if number is not None:
                cells[cell.coordinate] = float(number)

    labels: dict[str, LabelRates] = {}
    for column_index in range(2, ws.max_column + 1):
        label_value = ws.cell(row=_PARAM_LABEL_ROW, column=column_index).value
        if label_value is None or str(label_value).strip() == "":
            continue
        label = str(label_value).strip()
        column_letter = _openpyxl_column_letter(column_index)

        def rate(row: int) -> float:
            return _to_float(ws.cell(row=row, column=column_index).value)

        rent_start_raw = _number(ws.cell(row=_PARAM_RATE_ROWS["rent_start_month"], column=column_index).value)
        labels[label] = LabelRates(
            label=label,
            column=column_letter,
            normal_hourly=rate(_PARAM_RATE_ROWS["normal_hourly"]),
            daily=rate(_PARAM_RATE_ROWS["daily"]),
            ot_rate=rate(_PARAM_RATE_ROWS["ot_rate"]),
            holiday_rate=rate(_PARAM_RATE_ROWS["holiday_rate"]),
            support_rate=rate(_PARAM_RATE_ROWS["support_rate"]),
            rent=rate(_PARAM_RATE_ROWS["rent"]),
            rent_start_month=int(rent_start_raw) if rent_start_raw is not None else None,
        )
    if not labels:
        raise CostWorkbookError("「參數」工作表第 21 列找不到任何標別單價")
    return PricingParameters(tax_rate=tax_rate, safety_rate=safety_rate, labels=labels, cells=cells)


def read_pricing_parameters(content: bytes, file_name: str) -> PricingParameters:
    workbook = _load_workbook(content, file_name, read_only=True)
    try:
        return read_pricing_parameters_from_workbook(workbook)
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# Python 計價引擎（重現工時表 C／E／G 欄公式）
# ---------------------------------------------------------------------------

def normal_day_amount(rates: LabelRates, hours: Optional[float]) -> float:
    """計算 C 欄：每滿 8H 算一個日薪，剩餘時數再用超時費率。"""
    if hours is None:
        return 0.0
    if hours < 8:
        return hours * rates.normal_hourly
    full_days = int(hours // 8)
    remainder = hours - full_days * 8
    return full_days * rates.daily + remainder * rates.ot_rate


def overtime_day_amount(rates: LabelRates, hours: Optional[float], is_holiday: bool) -> float:
    """重現 E 欄：加班時數 × 假日費（假日）或超時費（平日）。"""
    if not hours:
        return 0.0
    return hours * (rates.holiday_rate if is_holiday else rates.ot_rate)


def support_day_amount(rates: LabelRates, hours: Optional[float]) -> float:
    """重現 G 欄：支援時數 × 司機支援費。"""
    if not hours:
        return 0.0
    return hours * rates.support_rate


def day_cost(
    rates: LabelRates,
    normal_hours: Optional[float],
    overtime_hours: Optional[float],
    support_hours: Optional[float],
    is_holiday: bool,
) -> DayCost:
    return DayCost(
        normal_amount=normal_day_amount(rates, normal_hours),
        overtime_amount=overtime_day_amount(rates, overtime_hours, is_holiday),
        support_amount=support_day_amount(rates, support_hours),
    )


# ---------------------------------------------------------------------------
# 活頁簿每日 C／E／G 公式求值（直接解析公式，100% 跟隨活頁簿計價方式）
# ---------------------------------------------------------------------------

_PARAM_REF_FULL = re.compile(r"參數!\$?([A-Z]{1,3})\$?(\d+)")
_LOCAL_HOUR_REF = re.compile(r"\$?([BDF])\$?(\d+)")
_TOKEN_RE = re.compile(
    r"""(\s*)(<>|<=|>=|[=+\-*/(),<>]|"[^"]*"|[A-Za-z][A-Za-z0-9]*|\d+(?:\.\d+)?)"""
)


def _excel_round(value: float, digits: int) -> float:
    from decimal import Decimal, ROUND_HALF_UP

    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _bind_formula(formula: str, parameters: PricingParameters, row_number: int, values: dict[str, Optional[float]]) -> str:
    def replace_param(match: re.Match) -> str:
        coord = f"{match.group(1)}{match.group(2)}"
        return repr(parameters.cells.get(coord, 0.0))

    def replace_local(match: re.Match) -> str:
        column, row = match.group(1), int(match.group(2))
        if row != row_number:
            return match.group(0)
        amount = values.get(column)
        return '""' if amount is None else repr(float(amount))

    text = _PARAM_REF_FULL.sub(replace_param, formula)
    text = _LOCAL_HOUR_REF.sub(replace_local, text)
    return text


class _FormulaParser:
    """精簡遞迴下降解析器：先建 AST 再求值，IF 分支採惰性求值。

    支援每日金額公式用到的 IF／ROUND、比較與四則運算。
    """

    def __init__(self, expression: str):
        self.tokens = [
            (group[1] if group[1] else group[2] or group[3])
            for group in _TOKEN_RE.finditer(expression)
            if (group[2] or group[3])
        ]
        self.index = 0

    def _peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _next(self):
        token = self._peek()
        self.index += 1
        return token

    def parse(self):
        node = self._comparison()
        if self._peek() is not None:
            raise CostWorkbookError(f"無法解析的計價公式片段：{self._peek()}")
        return self._eval(node)

    # --- AST 建構 ---
    def _comparison(self):
        left = self._additive()
        token = self._peek()
        if token in ("=", "<", ">", "<=", ">=", "<>"):
            self._next()
            right = self._additive()
            return ("cmp", token, left, right)
        return left

    def _additive(self):
        node = self._term()
        while self._peek() in ("+", "-"):
            operator = self._next()
            node = ("bin", operator, node, self._term())
        return node

    def _term(self):
        node = self._factor_node()
        while self._peek() in ("*", "/"):
            operator = self._next()
            node = ("bin", operator, node, self._factor_node())
        return node

    def _factor_node(self):
        token = self._next()
        if token is None:
            raise CostWorkbookError("計價公式結尾不完整")
        if token == "-":
            return ("neg", self._factor_node())
        if token == "(":
            node = self._comparison()
            if self._next() != ")":
                raise CostWorkbookError("計價公式括號未閉合")
            return node
        if token.startswith('"'):
            return ("str", "" if token == '""' else token.strip('"'))
        try:
            return ("num", float(token) if "." in token else int(token))
        except ValueError:
            pass
        if token.upper() in ("IF", "ROUND", "INT", "MOD") and self._peek() == "(":
            self._next()
            args = []
            if self._peek() != ")":
                args.append(self._comparison())
                while self._peek() == ",":
                    self._next()
                    args.append(self._comparison())
            if self._next() != ")":
                raise CostWorkbookError(f"{token} 函式括號未閉合")
            return ("call", token.upper(), args)
        raise CostWorkbookError(f"不支援的計價公式內容：{token}")

    # --- 求值（IF 惰性） ---
    def _eval(self, node):
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind == "str":
            return node[1]
        if kind == "neg":
            return -self._eval(node[1])
        if kind == "bin":
            _, operator, left_node, right_node = node
            left, right = self._eval(left_node), self._eval(right_node)
            if operator == "+":
                return left + right
            if operator == "-":
                return left - right
            if operator == "*":
                return left * right
            return left / right
        if kind == "cmp":
            _, operator, left_node, right_node = node
            return self._compare(self._eval(left_node), operator, self._eval(right_node))
        if kind == "call":
            _, name, args = node
            if name == "IF":
                if len(args) != 3:
                    raise CostWorkbookError("IF 函式需要三個參數")
                chosen = args[1] if self._truthy(self._eval(args[0])) else args[2]
                return self._eval(chosen)
            if name == "INT":
                if len(args) != 1:
                    raise CostWorkbookError("INT 函式需要一個參數")
                return int(float(self._eval(args[0])))
            if name == "MOD":
                if len(args) != 2:
                    raise CostWorkbookError("MOD 函式需要兩個參數")
                return float(self._eval(args[0])) % float(self._eval(args[1]))
            digits = int(self._eval(args[1])) if len(args) > 1 else 0
            return _excel_round(float(self._eval(args[0])), digits)
        raise CostWorkbookError(f"不支援的公式節點：{kind}")

    @staticmethod
    def _compare(left, operator, right):
        if isinstance(left, str) or isinstance(right, str):
            empty_left, empty_right = left == "", right == ""
            if operator == "=":
                return (empty_left and empty_right) if (empty_left or empty_right) else left == right
            if operator == "<>":
                return not _FormulaParser._compare(left, "=", right)
            # 空白／文字與數字的大小比較不會出現在每日公式的有效分支
            return False
        return {
            "=": left == right,
            "<": left < right,
            ">": left > right,
            "<=": left <= right,
            ">=": left >= right,
            "<>": left != right,
        }[operator]

    @staticmethod
    def _truthy(value) -> bool:
        if isinstance(value, str):
            return value != ""
        return bool(value)


def _evaluate_formula(formula: str, parameters: PricingParameters, row_number: int, values: dict[str, Optional[float]]):
    bound = _bind_formula(formula, parameters, row_number, values)
    return _FormulaParser(bound).parse()


def evaluate_day_amounts(
    parameters: PricingParameters,
    rates: LabelRates,
    row_number: int,
    normal_hours: Optional[float],
    overtime_hours: Optional[float],
    support_hours: Optional[float],
    *,
    c_formula: Optional[str],
    e_formula: Optional[str],
    g_formula: Optional[str],
    is_holiday: bool,
) -> DayCost:
    """以活頁簿每日公式計算金額；缺公式時退回分段規則。"""
    # 加班假日規則由呼叫端傳入，活頁簿既有 E 欄公式先依第 24／25 列規則正規化。
    normalized_c_formula = normalize_normal_formula(c_formula)
    normalized_e_formula = normalize_overtime_formula(e_formula, is_holiday)
    values = {"B": normal_hours, "D": overtime_hours, "F": support_hours}

    def amount(formula: Optional[str], fallback: float) -> float:
        if not formula:
            return fallback
        result = _evaluate_formula(formula, parameters, row_number, values)
        return 0.0 if result == "" or result is None else float(result)

    return DayCost(
        normal_amount=amount(normalized_c_formula, normal_day_amount(rates, normal_hours)),
        overtime_amount=amount(normalized_e_formula, overtime_day_amount(rates, overtime_hours, is_holiday)),
        support_amount=amount(g_formula, support_day_amount(rates, support_hours)),
    )


def totals_from_day_costs(
    rates: LabelRates,
    daily: list[DayCost],
    month: int,
    tax_rate: float,
    safety_rate: float,
) -> MonthTotals:
    untaxed = sum(item.total for item in daily)
    tax = _excel_round(untaxed * tax_rate, 0)
    taxed = untaxed + tax
    safety = _excel_round(untaxed * safety_rate, 0)
    rent = rent_amount(rates, month)
    return MonthTotals(untaxed=untaxed, tax=tax, taxed=taxed, safety=safety, rent=rent)


def rent_amount(rates: LabelRates, month: int) -> float:
    """月報邏輯：到了 Rent 開始月份才計入月租金（回傳負數，代表支出）。"""
    if rates.rent_start_month is not None and month < rates.rent_start_month:
        return 0.0
    return -rates.rent if rates.rent else 0.0


@dataclass(frozen=True)
class MonthTotals:
    untaxed: float
    tax: float
    taxed: float
    safety: float
    rent: float

    @property
    def net_profit(self) -> float:
        # 月報月淨利＝含稅請款＋安衛費＋月租金（油錢此處無法估算，略）
        return self.taxed + self.safety + self.rent


def summarize_month(
    rates: LabelRates,
    records: list[tuple[Optional[float], Optional[float], Optional[float], bool]],
    month: int,
    tax_rate: float,
    safety_rate: float,
) -> MonthTotals:
    """records 為（正常、加班、支援、是否假日）的每日清單，算出整月金額。"""
    untaxed = 0.0
    for normal, overtime, support, is_holiday in records:
        untaxed += day_cost(rates, normal, overtime, support, is_holiday).total
    tax = round(untaxed * tax_rate)
    taxed = untaxed + tax
    safety = round(untaxed * safety_rate)
    rent = rent_amount(rates, month)
    return MonthTotals(untaxed=untaxed, tax=tax, taxed=taxed, safety=safety, rent=rent)


# ---------------------------------------------------------------------------
# 單日／整月讀取
# ---------------------------------------------------------------------------

def _sheet_targets(workbook, target_date: date) -> list[CostSheetTarget]:
    period = roc_period(target_date)
    targets: list[CostSheetTarget] = []
    for worksheet in workbook.worksheets:
        match = SHEET_PATTERN.match(worksheet.title)
        if not match or match.group("period") != period:
            continue
        label = match.group("label")
        if label.startswith("小計") or label.startswith("總表"):
            continue
        for row_number in range(5, worksheet.max_row + 1):
            if _as_date(worksheet.cell(row=row_number, column=1).value) != target_date:
                continue
            targets.append(CostSheetTarget(
                worksheet_name=worksheet.title,
                label=label,
                row_number=row_number,
                normal_hours=_number(worksheet.cell(row=row_number, column=2).value),
                overtime_hours=_number(worksheet.cell(row=row_number, column=4).value),
                support_hours=_number(worksheet.cell(row=row_number, column=6).value),
                is_holiday=is_canonical_holiday(target_date),
                sheet_holiday=_row_is_holiday(worksheet, row_number),
                c_formula=_formula_text(worksheet.cell(row=row_number, column=3).value),
                e_formula=_formula_text(worksheet.cell(row=row_number, column=5).value),
                g_formula=_formula_text(worksheet.cell(row=row_number, column=7).value),
            ))
            break
    return sorted(targets, key=lambda item: item.label)


def list_cost_sheet_targets(content: bytes, file_name: str, target_date: date) -> list[CostSheetTarget]:
    workbook = _load_workbook(content, file_name, read_only=True)
    try:
        targets = _sheet_targets(workbook, target_date)
    finally:
        workbook.close()
    if not targets:
        raise CostWorkbookError(f"計價表內找不到 {roc_period(target_date)} 月份或 {target_date.isoformat()} 的日期列")
    return targets


def find_cost_sheet_target(content: bytes, file_name: str, target_date: date, label: str) -> CostSheetTarget:
    normalized_label = label.strip()
    targets = list_cost_sheet_targets(content, file_name, target_date)
    for target in targets:
        if target.label == normalized_label:
            return target
    raise CostWorkbookError(f"找不到標別「{normalized_label}」的 {target_date.isoformat()} 計價列")


def read_month_cost_data(content: bytes, file_name: str, period: str) -> MonthCostData:
    """讀取某民國年月（例如 11509）全部工時表的每日列與參數單價。"""
    workbook = _load_workbook(content, file_name, read_only=True)
    try:
        parameters = read_pricing_parameters_from_workbook(workbook)
        sheets: dict[str, list[MonthRow]] = {}
        for worksheet in workbook.worksheets:
            match = SHEET_PATTERN.match(worksheet.title)
            if not match or match.group("period") != period:
                continue
            label = match.group("label")
            if label.startswith("小計") or label.startswith("總表"):
                continue
            rows: list[MonthRow] = []
            for row_number in range(5, worksheet.max_row + 1):
                day = _as_date(worksheet.cell(row=row_number, column=1).value)
                if day is None:
                    continue
                rows.append(MonthRow(
                    label=label,
                    worksheet_name=worksheet.title,
                    row_number=row_number,
                    day=day,
                    normal_hours=_clean_number(worksheet.cell(row=row_number, column=2).value),
                    overtime_hours=_clean_number(worksheet.cell(row=row_number, column=4).value),
                    support_hours=_clean_number(worksheet.cell(row=row_number, column=6).value),
                    is_holiday=is_canonical_holiday(day),
                    sheet_holiday=_row_is_holiday(worksheet, row_number),
                    c_formula=_formula_text(worksheet.cell(row=row_number, column=3).value),
                    e_formula=_formula_text(worksheet.cell(row=row_number, column=5).value),
                    g_formula=_formula_text(worksheet.cell(row=row_number, column=7).value),
                ))
            if rows:
                sheets[label] = sorted(rows, key=lambda item: item.day)
        if not sheets:
            raise CostWorkbookError(f"計價表內找不到 {period} 月份的工時表")
        return MonthCostData(period=period, parameters=parameters, sheets=sheets)
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# 單日寫入（保留既有行為）
# ---------------------------------------------------------------------------

def import_cost_hours(
    content: bytes,
    file_name: str,
    target_date: date,
    label: str,
    *,
    normal_hours: float,
    overtime_hours: float,
    support_hours: float,
) -> tuple[bytes, CostSheetTarget]:
    """將單筆工時寫入指定日期／標別，保留活頁簿公式與 VBA。"""
    values = {
        "normal_hours": normal_hours,
        "overtime_hours": overtime_hours,
        "support_hours": support_hours,
    }
    if any(value < 0 for value in values.values()):
        raise CostWorkbookError("工時不可為負數")

    workbook = _load_workbook(content, file_name, read_only=False)
    try:
        target = find_cost_sheet_target(content, file_name, target_date, label)
        worksheet = workbook[target.worksheet_name]
        for field_name, column in HOUR_COLUMNS.items():
            value = values[field_name]
            worksheet[f"{column}{target.row_number}"] = _store_value(value)
        period = roc_period(target_date)
        align_overtime_holiday_rules(workbook, period)
        cache_fills = collect_month_formula_cache(workbook, period)
        output = io.BytesIO()
        workbook.save(output)
        return fill_formula_caches(output.getvalue(), cache_fills), target
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# 整月批次寫入（單一活頁簿只開一次、只存檔一次）
# ---------------------------------------------------------------------------

def import_month_hours(
    content: bytes,
    file_name: str,
    period: str,
    updates: list[MonthHourUpdate],
    *,
    overwrite: bool = False,
) -> tuple[bytes, list[MonthWriteResult]]:
    """把整月多筆工時連續寫入同一活頁簿。

    - 活頁簿只下載／開啟／存檔一次，避免多次回寫互相覆蓋。
    - 已有正常工時的日期預設略過（``overwrite=False``），避免蓋掉人工輸入。
    - 加班／支援傳 None 時保留活頁簿現值。
    """
    workbook = _load_workbook(content, file_name, read_only=False)
    results: list[MonthWriteResult] = []
    try:
        parameters = read_pricing_parameters_from_workbook(workbook)
        worksheet_cache: dict[str, object] = {}

        for update in updates:
            if update.normal_hours < 0 or (update.overtime_hours or 0) < 0 or (update.support_hours or 0) < 0:
                raise CostWorkbookError(f"{update.label} {update.day.isoformat()} 工時不可為負數")

            result = MonthWriteResult(
                label=update.label,
                worksheet_name=f"{period}-{update.label}",
                day=update.day,
                forklift_count=update.forklift_count,
            )
            worksheet = worksheet_cache.get(update.label)
            if worksheet is None:
                if result.worksheet_name not in workbook.sheetnames:
                    result.action = "no_worksheet"
                    results.append(result)
                    continue
                worksheet = workbook[result.worksheet_name]
                worksheet_cache[update.label] = worksheet

            matched_row: Optional[int] = None
            previous = (None, None, None)
            holiday = False
            for row_number in range(5, worksheet.max_row + 1):
                if _as_date(worksheet.cell(row=row_number, column=1).value) != update.day:
                    continue
                matched_row = row_number
                previous = (
                    _clean_number(worksheet.cell(row=row_number, column=2).value),
                    _clean_number(worksheet.cell(row=row_number, column=4).value),
                    _clean_number(worksheet.cell(row=row_number, column=6).value),
                )
                holiday = is_canonical_holiday(update.day)
                break

            if matched_row is None:
                result.action = "row_not_found"
                results.append(result)
                continue

            result.row_number = matched_row
            result.is_holiday = holiday
            result.previous_normal, result.previous_overtime, result.previous_support = previous

            if previous[0] is not None and not overwrite and not update.force:
                result.action = "skipped_exists"
                result.normal_hours = previous[0]
                result.overtime_hours = previous[1]
                result.support_hours = previous[2]
            else:
                normal_value = update.normal_hours
                overtime_value = update.overtime_hours if update.overtime_hours is not None else previous[1]
                support_value = update.support_hours if update.support_hours is not None else previous[2]
                worksheet[f"B{matched_row}"] = _store_value(normal_value)
                if update.overtime_hours is not None:
                    worksheet[f"D{matched_row}"] = _store_value(overtime_value)
                if update.support_hours is not None:
                    worksheet[f"F{matched_row}"] = _store_value(support_value)
                result.action = "written"
                result.normal_hours = normal_value
                result.overtime_hours = overtime_value
                result.support_hours = support_value

            rates = parameters.rates_for(update.label)
            result.cost = evaluate_day_amounts(
                parameters,
                rates,
                matched_row,
                result.normal_hours,
                result.overtime_hours,
                result.support_hours,
                c_formula=_formula_text(worksheet.cell(row=matched_row, column=3).value),
                e_formula=_formula_text(worksheet.cell(row=matched_row, column=5).value),
                g_formula=_formula_text(worksheet.cell(row=matched_row, column=7).value),
                is_holiday=result.is_holiday,
            )
            results.append(result)

        align_overtime_holiday_rules(workbook, period)
        cache_fills = collect_month_formula_cache(workbook, period)
        output = io.BytesIO()
        workbook.save(output)
        return fill_formula_caches(output.getvalue(), cache_fills), results
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# 工作日誌車輛台數 → 正常工時（與前端 getVehicleCounts 邏輯一致）
# ---------------------------------------------------------------------------

def vehicle_quantity(value: object, tonnage_pattern: str) -> int:
    """複刻前端 vehicleQuantity：抓到噸位後解析數量，沒寫數量就視為 1 台。"""
    text = str(value or "")
    if not re.search(tonnage_pattern, text):
        return 0
    marker = re.search(
        tonnage_pattern + r"[^0-9]{0,12}(?:數量|台數|x|X|×|\*)\s*(\d+)",
        text,
    )
    after_unit = re.search(
        tonnage_pattern + r"[^0-9]{0,12}(\d+)\s*(?:台|部|輛)",
        text,
    )
    before_unit = re.search(
        r"(\d+)\s*(?:台|部|輛)[^0-9]{0,12}" + tonnage_pattern,
        text,
    )
    for match in (marker, after_unit, before_unit):
        if match:
            return int(match.group(1))
    return 1


def count_forklift_units(assigned_texts: list[str], inspected_models: list[str]) -> dict[str, int]:
    """各噸位取 max(派工車輛, 點檢車型) 後回傳數量；total 為合計台數。"""
    counts: dict[str, int] = {}
    for key, pattern in TONNAGE_PATTERNS.items():
        assigned = sum(vehicle_quantity(text, pattern) for text in assigned_texts)
        inspected = sum(vehicle_quantity(text, pattern) for text in inspected_models)
        counts[key] = max(assigned, inspected)
    counts["total"] = sum(counts[key] for key in TONNAGE_ORDER)
    return counts


def normal_hours_from_units(unit_counts: dict[str, int]) -> int:
    """正常工時＝堆高機台數 × 8 小時。"""
    return unit_counts.get("total", 0) * 8


# ---------------------------------------------------------------------------
# 工地（Worksite）對應計價標別
# ---------------------------------------------------------------------------

def match_label_for_site(site_code: Optional[str], site_name: Optional[str], labels: list[str]) -> Optional[str]:
    """以工地代號／名稱比對計價標別。

    - 中文標別（新竹寶山1、金駿76）：工地代號或名稱包含標別即可。
    - 純數字標別（45、53）：要求數字前後不是其他數字，避免 45 誤判 450。
    比對順序由長到短，避免「寶山1」與「寶山10」互相吃掉。
    """
    haystack = f"{site_code or ''} {site_name or ''}".replace(" ", "")
    for label in sorted(labels, key=len, reverse=True):
        candidate = label.replace(" ", "")
        if candidate.isdigit():
            if re.search(rf"(?<!\d){re.escape(candidate)}(?!\d)", haystack):
                return label
        elif candidate and candidate in haystack:
            return label
    return None


# ---------------------------------------------------------------------------
# 活頁簿級公式求值（跨工作表、SUM、IFERROR），用於彙總列與公式快取回填
# ---------------------------------------------------------------------------

import html as _html
import zipfile as _zipfile
from openpyxl.utils import column_index_from_string as _col_index, range_boundaries as _range_bounds

_CROSS_REF_RE = re.compile(
    r"(?:'(?P<qsheet>[^']+)'|(?P<sheet>[一-鿿A-Za-z0-9_.]+))!"
    r"(?P<ref>\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)"
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]*")
_LOCAL_REF_RE = re.compile(r"\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_STRING_RE = re.compile(r'"[^"]*"')


class _WorkbookCalculator:
    """在已寫入工時的活頁簿上，對公式儲存格做跨表求值。

    支援 IF（惰性）、ROUND、SUM（範圍）、IFERROR、四則、比較、同表／跨表
    參照。遇到不支援的函式或語法會擲 CostWorkbookError，由呼叫端決定略過，
    確保「算不準就不回填快取」，不寫入任何猜測值。
    """

    SUPPORTED_FUNCTIONS = {"IF", "ROUND", "INT", "MOD", "SUM", "IFERROR"}

    def __init__(self, workbook):
        self.workbook = workbook
        self.memo: dict[tuple[str, str], object] = {}
        self.visiting: set[tuple[str, str]] = set()

    # --- 對外入口 ---
    def numeric_value(self, sheet_name: str, coord: str):
        value = self.resolve(sheet_name, coord)
        if isinstance(value, str):
            return 0.0 if value == "" else float(value)
        return float(value)

    def resolve(self, sheet_name: str, coord: str) -> object:
        key = (sheet_name, coord.upper())
        if key in self.memo:
            return self.memo[key]
        if sheet_name not in self.workbook.sheetnames:
            raise CostWorkbookError(f"找不到工作表「{sheet_name}」")
        cell = self.workbook[sheet_name][coord]
        value = cell.value
        result = self._cell_value(sheet_name, coord, value)
        self.memo[key] = result
        return result

    def _cell_value(self, sheet_name: str, coord: str, value: object) -> object:
        if isinstance(value, str) and value.startswith("="):
            if (sheet_name, coord.upper()) in self.visiting:
                raise CostWorkbookError(f"公式循環參照：{sheet_name}!{coord}")
            self.visiting.add((sheet_name, coord.upper()))
            try:
                return self.evaluate_expression(value[1:], sheet_name)
            finally:
                self.visiting.discard((sheet_name, coord.upper()))
        if value is None:
            return ""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        return str(value)

    # --- 分詞 ---
    def _tokenize(self, expression: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        index = 0
        while index < len(expression):
            char = expression[index]
            if char.isspace():
                index += 1
                continue
            cross = _CROSS_REF_RE.match(expression, index)
            if cross:
                tokens.append(("cross", cross.group(0)))
                index = cross.end()
                continue
            string = _STRING_RE.match(expression, index)
            if string:
                tokens.append(("str", string.group(0)))
                index = string.end()
                continue
            number = _NUMBER_RE.match(expression, index)
            if number:
                tokens.append(("num", number.group(0)))
                index = number.end()
                continue
            local = _LOCAL_REF_RE.match(expression, index)
            if local:
                tokens.append(("ref", local.group(0)))
                index = local.end()
                continue
            if char in "+-*/(),=<>":
                if expression[index:index + 2] in ("<>", "<=", ">="):
                    tokens.append(("op", expression[index:index + 2]))
                    index += 2
                else:
                    tokens.append(("op", char))
                    index += 1
                continue
            word = _WORD_RE.match(expression, index)
            if word:
                tokens.append(("name", word.group(0).upper()))
                index = word.end()
                continue
            raise CostWorkbookError(f"無法辨識的公式內容：{expression[index:index + 10]}")
        return tokens



def _openpyxl_letter(column_number: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(column_number)


def is_truthy(value) -> bool:
    if isinstance(value, str):
        return value != ""
    return bool(value)


def compare_values(left, operator: str, right):
    if isinstance(left, str) or isinstance(right, str):
        empty_left, empty_right = left == "", right == ""
        if operator == "=":
            if empty_left or empty_right:
                return empty_left and empty_right
            return left == right
        if operator == "<>":
            return not compare_values(left, "=", right)
        return False
    return {
        "=": left == right,
        "<": left < right,
        ">": left > right,
        "<=": left <= right,
        ">=": left >= right,
        "<>": left != right,
    }[operator]


# SUM 需要惰性與範圍支援，以獨立求值器重做（取代上面簡易版的盲點）。
class _WorkbookFormulaCalculator(_WorkbookCalculator):
    def evaluate_expression(self, expression: str, current_sheet: str):
        tokens = self._tokenize(expression)
        position = [0]

        def peek():
            return tokens[position[0]] if position[0] < len(tokens) else (None, None)

        def take():
            token = peek()
            position[0] += 1
            return token

        def parse_comparison():
            left = parse_additive()
            kind, value = peek()
            if kind == "op" and value in ("=", "<", ">", "<=", ">=", "<>"):
                take()
                right = parse_additive()
                return compare_values(left, value, right)
            return left

        def parse_additive():
            node = parse_term()
            while peek()[0] == "op" and peek()[1] in ("+", "-"):
                _, operator = take()
                rhs = parse_term()
                node = (node + rhs) if operator == "+" else (node - rhs)
            return node

        def parse_term():
            node = parse_factor()
            while peek()[0] == "op" and peek()[1] in ("*", "/"):
                _, operator = take()
                rhs = parse_factor()
                node = (node * rhs) if operator == "*" else (node / rhs)
            return node

        def parse_factor():
            kind, value = take()
            if kind == "op" and value == "-":
                return -as_number(parse_factor())
            if kind == "op" and value == "(":
                node = parse_comparison()
                if take() != ("op", ")"):
                    raise CostWorkbookError("公式括號未閉合")
                return node
            if kind == "num":
                return float(value) if "." in value else int(value)
            if kind == "str":
                return "" if value == '""' else value.strip('"')
            if kind in ("ref", "cross"):
                return resolve_reference(value)
            if kind == "name" and peek() == ("op", "("):
                return parse_function(value)
            if kind == "name":
                raise CostWorkbookError(f"不支援的名稱：{value}")
            raise CostWorkbookError(f"非預期的公式片段：{value}")

        def skip_argument():
            depth = 0
            while True:
                kind, value = peek()
                if kind is None:
                    raise CostWorkbookError("公式參數未閉合")
                if kind == "op" and value == "(":
                    depth += 1
                    take()
                elif kind == "op" and value == ")":
                    if depth == 0:
                        return
                    depth -= 1
                    take()
                elif kind == "op" and value == "," and depth == 0:
                    return
                else:
                    take()

        def expect_close(function_name: str):
            if take() != ("op", ")"):
                raise CostWorkbookError(f"{function_name} 括號未閉合")

        def parse_function(name: str):
            take()  # (
            if name not in self.SUPPORTED_FUNCTIONS:
                raise CostWorkbookError(f"不支援的函式：{name}")
            if name == "IF":
                condition = parse_comparison()
                if peek() != ("op", ","):
                    raise CostWorkbookError("IF 需要三個參數")
                take()
                if is_truthy(condition):
                    chosen = parse_comparison()
                    if peek() == ("op", ","):
                        take()
                        skip_argument()
                else:
                    skip_argument()
                    if peek() == ("op", ","):
                        take()
                        chosen = parse_comparison()
                    else:
                        chosen = False
                expect_close("IF")
                return chosen
            if name == "IFERROR":
                start = position[0]
                try:
                    first = parse_comparison()
                except (CostWorkbookError, TypeError, ZeroDivisionError, KeyError, ValueError):
                    position[0] = start
                    skip_argument()
                    if peek() == ("op", ","):
                        take()
                        fallback = parse_comparison()
                    else:
                        fallback = 0
                    expect_close("IFERROR")
                    return fallback
                if peek() == ("op", ","):
                    take()
                    skip_argument()
                expect_close("IFERROR")
                return first
            raw_args = []
            if peek() != ("op", ")"):
                raw_args.append(parse_comparison())
                while peek() == ("op", ","):
                    take()
                    raw_args.append(parse_comparison())
            expect_close(name)
            if name == "ROUND":
                digits = int(raw_args[1]) if len(raw_args) > 1 else 0
                return _excel_round(as_number(raw_args[0]), digits)
            if name == "INT":
                if len(raw_args) != 1:
                    raise CostWorkbookError("INT 函式需要一個參數")
                return int(as_number(raw_args[0]))
            if name == "MOD":
                if len(raw_args) != 2:
                    raise CostWorkbookError("MOD 函式需要兩個參數")
                return as_number(raw_args[0]) % as_number(raw_args[1])
            if name == "SUM":
                return sum(as_number(item) for item in raw_args)
            raise CostWorkbookError(f"不支援的函式：{name}")

        def resolve_reference(token: str):
            cross = _CROSS_REF_RE.match(token)
            if cross:
                sheet = cross.group("qsheet") or cross.group("sheet")
                ref = cross.group("ref")
            else:
                sheet, ref = current_sheet, token
            if ":" in ref:
                return sum_reference(sheet, ref)
            return self.resolve(sheet, ref.replace("$", ""))

        def sum_reference(sheet: str, ref: str) -> float:
            start, end = ref.split(":")
            min_col, min_row, max_col, max_row = _range_bounds(
                f"{start.replace('$', '')}:{end.replace('$', '')}"
            )
            if sheet not in self.workbook.sheetnames:
                raise CostWorkbookError(f"找不到工作表「{sheet}」")
            worksheet = self.workbook[sheet]
            total = 0.0
            for row_number in range(min_row, max_row + 1):
                for column_number in range(min_col, max_col + 1):
                    coord = f"{_openpyxl_letter(column_number)}{row_number}"
                    value = self.resolve(sheet, coord)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        total += float(value)
            return total

        def as_number(value):
            if isinstance(value, bool):
                return 1.0 if value else 0.0
            if isinstance(value, (int, float)):
                return float(value)
            if value is None or value == "":
                return 0.0
            return float(value)

        result = parse_comparison()
        if peek()[0] is not None:
            raise CostWorkbookError("公式還有未解析片段")
        return result


def align_overtime_holiday_rules(workbook, period: str) -> int:
    """把加班 E 欄公式統一到「週日或國定假日」規則。

    凡是非假日卻引用假日費率（參數第 25 列）的儲存格，一律改回平日超時費（第 24 列）；
    週日與國定假日的第 25 列保留。如此活頁簿公式、Excel 重算與回填快取會完全一致，
    不會在 Excel 開啟後金額跳動。回傳校正的儲存格數。
    """
    changed = 0
    for worksheet in workbook.worksheets:
        match = SHEET_PATTERN.match(worksheet.title)
        if not match or match.group("period") != period:
            continue
        for row_number in range(5, worksheet.max_row + 1):
            day = _as_date(worksheet.cell(row=row_number, column=1).value)
            if day is None:
                continue
            normal_cell = worksheet.cell(row=row_number, column=3)
            normal_formula = normal_cell.value
            if isinstance(normal_formula, str) and "參數!" in normal_formula:
                normalized_normal = normalize_normal_formula(normal_formula)
                if normalized_normal != normal_formula:
                    normal_cell.value = normalized_normal
                    changed += 1
            cell = worksheet.cell(row=row_number, column=5)
            formula = cell.value
            if isinstance(formula, str) and "參數!" in formula:
                normalized = normalize_overtime_formula(formula, is_canonical_holiday(day))
                if normalized != formula:
                    cell.value = normalized
                    changed += 1
    return changed


def _month_target_sheet_names(workbook, period: str) -> list[str]:
    """當月需要回填快取的工作表：工時表、計價（請款）表、月報。"""
    names = []
    for worksheet in workbook.worksheets:
        title = worksheet.title
        match = SHEET_PATTERN.match(title)
        if match and match.group("period") == period:
            names.append(title)
        elif title.startswith(f"計價-{period}-") or title == f"月報-{period}":
            names.append(title)
    return names


def collect_month_formula_cache(workbook, period: str) -> dict[str, dict[str, float]]:
    """計算當月工時表、計價表、月報所有公式格的數值快取。

    算不準（不支援函式、缺工作表等）的儲存格直接略過，絕不回填猜測值。
    """
    calculator = _WorkbookFormulaCalculator(workbook)
    fills: dict[str, dict[str, float]] = {}
    for sheet_name in _month_target_sheet_names(workbook, period):
        worksheet = workbook[sheet_name]
        sheet_fills: dict[str, float] = {}
        for row in worksheet.iter_rows():
            for cell in row:
                if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                    continue
                try:
                    value = calculator.resolve(sheet_name, cell.coordinate)
                except (CostWorkbookError, TypeError, ZeroDivisionError, KeyError, ValueError):
                    continue
                if isinstance(value, bool) or isinstance(value, str):
                    continue
                if isinstance(value, (int, float)):
                    number = float(value)
                    if number == int(number):
                        number = int(number)
                    sheet_fills[cell.coordinate] = number
        if sheet_fills:
            fills[sheet_name] = sheet_fills
    return fills


_CELL_TAG_RE = re.compile(r'(<c r="(?P<coord>[A-Z]+\d+)"(?P<attrs>[^>]*)>)(?P<inner>.*?)(</c>)', re.S)


def _inject_cache_value(xml_text: str, coord: str, number) -> str:
    pattern = re.compile(
        r'(<c r="' + re.escape(coord) + r'"(?P<attrs>[^>]*)>)(?P<inner>.*?)(</c>)',
        re.S,
    )
    match = pattern.search(xml_text)
    if not match:
        return xml_text
    inner = match.group("inner")
    if "<f" not in inner and "<f" not in match.group(0):
        return xml_text  # 只回填公式格
    # 移除舊快取值（含自我封閉 <v/>）與數值不相容的型別標記
    inner = re.sub(r"<v\b[^>]*/>", "", inner)
    inner = re.sub(r"<v\b[^>]*>.*?</v>", "", inner, flags=re.S)
    opening = match.group(1)
    opening = re.sub(r'\s+t="(?:str|b|e)"', "", opening)
    if isinstance(number, bool):
        value_text = "1" if number else "0"
    elif isinstance(number, int):
        value_text = str(number)
    elif isinstance(number, float):
        value_text = str(int(number)) if number.is_integer() else repr(number)
    else:
        value_text = str(number)
    replacement = f"{opening}{inner}<v>{value_text}</v></c>"
    return xml_text[:match.start()] + replacement + xml_text[match.end():]


def fill_formula_caches(content: bytes, fills_by_sheet: dict[str, dict[str, float]]) -> bytes:
    """openpyxl 存檔後，對指定工作表的公式格注入計算快取 <v>，讓 Google 試算表／
    手機預覽不必開 Excel 重算即可顯示金額；公式本身完全保留。
    """
    if not fills_by_sheet:
        return content
    with _zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}

    workbook_xml = entries["xl/workbook.xml"][1].decode("utf-8")
    rels_xml = entries["xl/_rels/workbook.xml.rels"][1].decode("utf-8")
    rid_to_target: dict[str, str] = {}
    for rel_match in re.finditer(r"<Relationship\b[^>]*>", rels_xml):
        tag = rel_match.group(0)
        id_match = re.search(r'\bId="([^"]+)"', tag)
        target_match = re.search(r'\bTarget="([^"]+)"', tag)
        if id_match and target_match:
            rid_to_target[id_match.group(1)] = target_match.group(1)
    sheet_paths: dict[str, str] = {}
    for sheet_tag_match in re.finditer(r"<sheet\b[^>]*>", workbook_xml):
        tag = sheet_tag_match.group(0)
        name_match = re.search(r'\bname="([^"]+)"', tag)
        rid_match = re.search(r'\br:id="([^"]+)"', tag)
        if not name_match or not rid_match:
            continue
        target = rid_to_target.get(rid_match.group(1))
        if not target:
            continue
        name = _html.unescape(name_match.group(1))
        path = target.lstrip("/")
        if not path.startswith("xl/"):
            path = f"xl/{path}"
        sheet_paths[name] = path

    for sheet_name, coord_fills in fills_by_sheet.items():
        path = sheet_paths.get(sheet_name)
        if not path or path not in entries:
            continue
        xml_text = entries[path][1].decode("utf-8")
        for coord, number in coord_fills.items():
            xml_text = _inject_cache_value(xml_text, coord, number)
        entries[path] = (entries[path][0], xml_text.encode("utf-8"))

    output = io.BytesIO()
    with _zipfile.ZipFile(output, "w", _zipfile.ZIP_DEFLATED) as archive:
        for filename, (info, data) in entries.items():
            archive.writestr(info, data)
    return output.getvalue()
