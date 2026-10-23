"""Millimetre layout and integer-TWD draft calculations, not invoice issuance."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Position(StrictModel):
    x: float = Field(ge=0, le=296)
    y: float = Field(ge=0, le=296)
    width: float = Field(gt=0, le=297)


def default_fields():
    coordinates = {
        "buyer": (35, 13.5, 70), "tax_id": (35, 20, 65),
        "year": (125, 22, 12), "month": (145, 22, 8), "day": (160, 22, 8),
        "description": (12, 38, 80), "quantity": (96, 38, 13), "unit": (112, 38, 9),
        "unit_price": (124, 38, 24), "amount": (151, 38, 27),
        "subtotal": (151, 70, 27), "tax": (151, 76, 27), "total": (151, 82, 27),
        "uppercase": (25, 82, 120), "note": (12, 94, 165),
    }
    return {key: Position(x=x, y=y, width=w) for key, (x, y, w) in coordinates.items()}


class PrintLayout(StrictModel):
    paper_width: float = Field(default=195, ge=80, le=297)
    paper_height: float = Field(default=103, ge=80, le=297)
    offset_x: float = Field(default=0, ge=-20, le=20)
    offset_y: float = Field(default=0, ge=-20, le=20)
    font: Literal["PMingLiU", "Microsoft JhengHei"] = "PMingLiU"
    font_size: float = Field(default=11, ge=8, le=16)
    tax_id_spacing: float = Field(default=3, ge=0, le=8)
    row_pitch: float = Field(default=6, ge=4, le=8)
    fields: dict[str, Position] = Field(default_factory=default_fields)

    @field_validator("fields")
    @classmethod
    def exact_fields(cls, fields):
        if set(fields) != set(default_fields()):
            raise ValueError("欄位清單不完整")
        return fields

    @model_validator(mode="after")
    def paper_bounds(self):
        if any(p.x + p.width > self.paper_width or p.y >= self.paper_height for p in self.fields.values()):
            raise ValueError("欄位超出目前紙張範圍")
        return self


class PrintItem(StrictModel):
    description: str = Field(min_length=1, max_length=40)
    quantity: Decimal = Field(default=Decimal(1), gt=0, le=1000000, decimal_places=3)
    unit: str = Field(default="", max_length=4)
    unit_price: Decimal = Field(default=Decimal(0), ge=0, le=999999999, decimal_places=2)


class PrintDraft(StrictModel):
    buyer: str = Field(default="", max_length=50)
    tax_id: str = Field(default="", pattern=r"^(?:[0-9]{8})?$")
    invoice_date: date
    items: list[PrintItem] = Field(min_length=1, max_length=5)
    tax: int = Field(default=0, ge=0, le=99999999999, strict=True)
    note: str = Field(default="", max_length=60)

    @field_validator("invoice_date")
    @classmethod
    def roc_date(cls, value):
        if value.year < 1912:
            raise ValueError("開立日期需在民國元年以後")
        return value


def uppercase_twd(value: int) -> str:
    if type(value) is not int or not 0 <= value <= 999999999999:
        raise ValueError("總計須為0至999999999999的整數")
    if value == 0:
        return "零元整"
    digits = "零壹貳參肆伍陸柒捌玖"

    def group(number):
        result, zero = "", False
        for power, unit in ((1000, "仟"), (100, "佰"), (10, "拾"), (1, "")):
            digit, number = divmod(number, power)
            if digit:
                result += ("零" if zero else "") + digits[digit] + unit
                zero = False
            elif result and number:
                zero = True
        return result

    result, gap = "", False
    for power, unit in ((100000000, "億"), (10000, "萬"), (1, "")):
        part, value = divmod(value, power)
        if part:
            if result and (gap or part < 1000):
                result += "零"
            result += group(part) + unit
            gap = False
        elif result:
            gap = True
    return result + "元整"


def calculate(draft: PrintDraft) -> dict:
    amounts = [int((row.quantity * row.unit_price).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for row in draft.items]
    subtotal = sum(amounts)
    total = subtotal + draft.tax
    return {"amounts": amounts, "subtotal": subtotal, "tax": draft.tax, "total": total,
            "uppercase": uppercase_twd(total), "roc_year": draft.invoice_date.year - 1911}
