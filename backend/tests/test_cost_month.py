from datetime import date
from io import BytesIO

import openpyxl

from app.services.cost_workbook import (
    MonthHourUpdate,
    count_forklift_units,
    day_cost,
    evaluate_day_amounts,
    import_month_hours,
    match_label_for_site,
    normal_hours_from_units,
    read_month_cost_data,
    read_pricing_parameters,
)

# 標別在參數表的欄位：45 -> B、新竹寶山1 -> I（與真實活頁簿相同）
LABEL_COLUMNS = {"45": "B", "新竹寶山1": "I"}


def _normal_formula(column: str, row: int) -> str:
    return (
        f'=IF(B{row}="","",IF(B{row}<8,B{row}*參數!${column}$22,'
        f'IF(B{row}=8,參數!${column}$23,參數!${column}$23+(B{row}-8)*參數!${column}$24)))'
    )


def _overtime_formula(column: str, row: int, *, holiday: bool) -> str:
    rate_row = 25 if holiday else 24
    return f'=IF(D{row}="","",D{row}*參數!${column}${rate_row})'


def _support_formula(column: str, row: int) -> str:
    return f'=IF(F{row}="","",F{row}*參數!${column}$26)'


def _add_hours_sheet(workbook, label: str, days: list[date], holiday_days: set[date], prefilled: dict[date, int] | None = None):
    worksheet = workbook.create_sheet(f"11509-{label}")
    column = LABEL_COLUMNS[label]
    prefilled = prefilled or {}
    for index, day in enumerate(days, start=5):
        worksheet.cell(row=index, column=1, value=day)
        worksheet.cell(row=index, column=3, value=_normal_formula(column, index))
        worksheet.cell(row=index, column=5, value=_overtime_formula(column, index, holiday=day in holiday_days))
        worksheet.cell(row=index, column=7, value=_support_formula(column, index))
        if day in prefilled:
            worksheet.cell(row=index, column=2, value=prefilled[day])
    return worksheet


def _workbook_bytes(prefilled_45: dict[date, int] | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    params = workbook.create_sheet("參數")
    params["B4"] = 0.05
    params["B5"] = 0.006
    params["B21"] = "45"
    params["I21"] = "新竹寶山1"
    # 45：時薪 800、日薪 6000、超時 1000、假日 1200、支援 800、租金 18900
    params["B22"], params["B23"], params["B24"], params["B25"], params["B26"] = 800, 6000, 1000, 1200, 800
    params["B27"], params["B28"] = 18900, 1
    # 新竹寶山1：時薪 1000、日薪 8000、超時 1100、假日 1200、支援 800
    params["I22"], params["I23"], params["I24"], params["I25"], params["I26"] = 1000, 8000, 1100, 1200, 800
    params["I27"], params["I28"] = 0, 9

    days = [date(2026, 9, d) for d in range(1, 4)]  # 9/1..9/3，9/2 為假日
    _add_hours_sheet(workbook, "45", days, {date(2026, 9, 2)}, prefilled=prefilled_45)
    _add_hours_sheet(workbook, "新竹寶山1", days, {date(2026, 9, 2)})

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_read_pricing_parameters_per_label_rates():
    parameters = read_pricing_parameters(_workbook_bytes(), "計價表.xlsx")
    assert parameters.tax_rate == 0.05
    assert parameters.safety_rate == 0.006
    rate_45 = parameters.rates_for("45")
    assert (rate_45.normal_hourly, rate_45.daily, rate_45.ot_rate, rate_45.holiday_rate, rate_45.support_rate) == (
        800, 6000, 1000, 1200, 800,
    )
    rate_baoshan = parameters.rates_for("新竹寶山1")
    assert (rate_baoshan.normal_hourly, rate_baoshan.daily, rate_baoshan.ot_rate) == (1000, 8000, 1100)


def test_day_pricing_rules():
    parameters = read_pricing_parameters(_workbook_bytes(), "計價表.xlsx")
    rates = parameters.rates_for("45")
    assert day_cost(rates, 7, 0, 0, False).normal_amount == 5600      # 未滿 8 以時薪計
    assert day_cost(rates, 8, 0, 0, False).normal_amount == 6000      # 剛好 8 計日薪
    assert day_cost(rates, 10, 0, 0, False).normal_amount == 8000     # 超過 8：日薪 + 超時
    assert day_cost(rates, 8, 2, 0, False).overtime_amount == 2000    # 平日超時費
    assert day_cost(rates, 8, 2, 0, True).overtime_amount == 2400     # 假日加班費
    assert day_cost(rates, 8, 0, 2, False).support_amount == 1600    # 司機支援


def test_formula_evaluation_matches_rules():
    content = _workbook_bytes()
    data = read_month_cost_data(content, "計價表.xlsx", "11509")
    parameters = data.parameters
    rates = parameters.rates_for("45")
    row = next(item for item in data.sheets["45"] if item.day == date(2026, 9, 1))
    cost = evaluate_day_amounts(
        parameters, rates, row.row_number, 10, 2, 1,
        c_formula=row.c_formula, e_formula=row.e_formula, g_formula=row.g_formula,
        is_holiday=False,
    )
    assert cost.normal_amount == 8000   # 6000 + 2*1000
    assert cost.overtime_amount == 2000
    assert cost.support_amount == 800
    assert cost.total == 10800


def test_month_import_writes_multiple_days_and_labels_once():
    content = _workbook_bytes()
    updates = [
        MonthHourUpdate(label="45", day=date(2026, 9, 1), normal_hours=8),
        MonthHourUpdate(label="45", day=date(2026, 9, 2), normal_hours=9),
        MonthHourUpdate(label="新竹寶山1", day=date(2026, 9, 1), normal_hours=8),
    ]
    updated, results = import_month_hours(content, "計價表.xlsx", "11509", updates)

    assert [result.action for result in results] == ["written", "written", "written"]
    by_key = {(result.label, result.day): result for result in results}
    assert by_key[("45", date(2026, 9, 1))].cost.normal_amount == 6000
    assert by_key[("45", date(2026, 9, 2))].cost.normal_amount == 7000       # 6000 + 1*1000
    assert by_key[("新竹寶山1", date(2026, 9, 1))].cost.normal_amount == 8000

    workbook = openpyxl.load_workbook(BytesIO(updated), data_only=False)
    assert workbook["11509-45"]["B5"].value == 8
    assert workbook["11509-45"]["B6"].value == 9
    assert workbook["11509-新竹寶山1"]["B5"].value == 8
    # 公式保留
    assert workbook["11509-45"]["C5"].value.startswith("=IF(")


def test_month_import_skips_existing_by_default_and_overwrites_when_asked():
    existing = {date(2026, 9, 1): 8}
    updates = [MonthHourUpdate(label="45", day=date(2026, 9, 1), normal_hours=16)]

    skipped_content, skipped_results = import_month_hours(
        _workbook_bytes(existing), "計價表.xlsx", "11509", updates, overwrite=False
    )
    assert skipped_results[0].action == "skipped_exists"
    wb_skipped = openpyxl.load_workbook(BytesIO(skipped_content), data_only=False)
    assert wb_skipped["11509-45"]["B5"].value == 8

    overwritten_content, overwritten_results = import_month_hours(
        _workbook_bytes(existing), "計價表.xlsx", "11509", updates, overwrite=True
    )
    assert overwritten_results[0].action == "written"
    wb_overwritten = openpyxl.load_workbook(BytesIO(overwritten_content), data_only=False)
    assert wb_overwritten["11509-45"]["B5"].value == 16


def test_month_import_reports_missing_worksheet():
    updates = [MonthHourUpdate(label="99", day=date(2026, 9, 1), normal_hours=8)]
    _, results = import_month_hours(_workbook_bytes(), "計價表.xlsx", "11509", updates)
    assert results[0].action == "no_worksheet"


def test_forklift_units_and_normal_hours_match_frontend_logic():
    assigned = ["2.5噸", "3.0噸 數量2"]
    inspected = ["自排 4.5噸柴油車"]
    units = count_forklift_units(assigned, inspected)
    assert units["twoPointFive"] == 1
    assert units["threePointZero"] == 2
    assert units["fourPointFive"] == 1
    assert units["total"] == 4
    assert normal_hours_from_units(units) == 32   # 4 台 × 8 小時

    # 派工與點檢同噸位取 max，不重複相加
    units_same = count_forklift_units(["2.5噸"], ["2.5噸"])
    assert units_same["twoPointFive"] == 1


def test_match_label_for_site():
    labels = ["45", "53", "56", "47", "金駿76", "桃園28", "桃園29", "新竹寶山1", "新竹寶山2", "新竹寶山3"]
    assert match_label_for_site("53", "齊裕53", labels) == "53"
    assert match_label_for_site("善捷47", "善捷47", labels) == "47"
    assert match_label_for_site("新竹寶山1", "新竹寶山1", labels) == "新竹寶山1"
    assert match_label_for_site("金駿76", "金駿76", labels) == "金駿76"
    # 純數字標別不得誤判（23 兩側仍有數字）
    assert match_label_for_site("X123", "X123", labels) is None


def _weekend_workbook_bytes() -> bytes:
    """9/5（週六）、9/6（週日），E 欄皆寫死第 25 列（模擬舊活頁簿週六週日都算假日）。"""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    params = workbook.create_sheet("參數")
    params["B4"], params["B5"] = 0.05, 0.006
    params["B21"] = "45"
    params["B22"], params["B23"], params["B24"], params["B25"], params["B26"] = 800, 6000, 1000, 1200, 800
    params["B27"], params["B28"] = 0, 1
    sheet = workbook.create_sheet("11509-45")
    for index, day in enumerate([date(2026, 9, 5), date(2026, 9, 6)], start=5):
        sheet.cell(row=index, column=1, value=day)
        sheet.cell(row=index, column=3, value=_normal_formula("B", index))
        sheet.cell(row=index, column=5, value=_overtime_formula("B", index, holiday=True))
        sheet.cell(row=index, column=7, value=_support_formula("B", index))
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_saturday_uses_weekday_rate_but_sunday_keeps_holiday_rate():
    updated, results = import_month_hours(
        _weekend_workbook_bytes(), "計價表.xlsx", "11509",
        [
            MonthHourUpdate(label="45", day=date(2026, 9, 5), normal_hours=8, overtime_hours=2),
            MonthHourUpdate(label="45", day=date(2026, 9, 6), normal_hours=8, overtime_hours=2),
        ],
    )
    saturday = next(item for item in results if item.day == date(2026, 9, 5))
    sunday = next(item for item in results if item.day == date(2026, 9, 6))
    assert saturday.cost.normal_amount == 6000
    assert saturday.cost.overtime_amount == 2000   # 週六改用平日超時費 1000*2
    assert sunday.cost.normal_amount == 6000
    assert sunday.cost.overtime_amount == 2400      # 週日維持假日費 1200*2

    workbook = openpyxl.load_workbook(BytesIO(updated), data_only=False)
    assert "$24" in workbook["11509-45"]["E5"].value   # 週六 E 公式由 25 校正為 24
    assert "$25" in workbook["11509-45"]["E6"].value   # 週日保留 25

    cached = openpyxl.load_workbook(BytesIO(updated), data_only=True)
    assert cached["11509-45"]["C5"].value == 6000
    assert cached["11509-45"]["E5"].value == 2000
    assert cached["11509-45"]["E6"].value == 2400


def test_formula_caches_are_filled_after_month_import():
    updated, _ = import_month_hours(
        _workbook_bytes(), "計價表.xlsx", "11509",
        [MonthHourUpdate(label="45", day=date(2026, 9, 1), normal_hours=8)],
    )
    cached = openpyxl.load_workbook(BytesIO(updated), data_only=True)
    assert cached["11509-45"]["C5"].value == 6000   # 未開 Excel 重算也看得到金額


def test_force_update_overrides_existing_without_global_overwrite():
    existing = {date(2026, 9, 1): 8}
    updated, results = import_month_hours(
        _workbook_bytes(existing), "計價表.xlsx", "11509",
        [MonthHourUpdate(label="45", day=date(2026, 9, 1), normal_hours=16, overtime_hours=2, force=True)],
        overwrite=False,
    )
    assert results[0].action == "written"
    workbook = openpyxl.load_workbook(BytesIO(updated), data_only=False)
    assert workbook["11509-45"]["B5"].value == 16
    assert workbook["11509-45"]["D5"].value == 2
