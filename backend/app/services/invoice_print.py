"""Pure overprint calculation, layout, and draft helpers for the triplicate invoice.

The workbench only prints content onto an existing 三聯式統一發票 (195 x 103 mm):
it never creates an invoice, an invoice number, an account entry, or a tax event.
The real-form coordinate set (2026-10-23 v3) is measured from the actual paper
sample; it adds the 買受人地址 row, drops the non-existent 單位 column, and
places 備註 in the right column.

Short-edge feed (103 mm leading edge) on the Brother MFC-J3940DW uses the
registered Windows form ``Santong_Invoice_103x195`` (103 x 195 portrait). The
browser maps a 195x103 ``@page`` to that portrait form and rotates the job;
when the driver also forces Landscape the page is rotated again and comes out
90° wrong / shrunk. The ``portrait_cw`` / ``portrait_ccw`` feed modes keep the
CSS page exactly 103x195 (matching the form, no fit-to-page scaling) and rotate
the content inside CSS instead.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ITEMS = 5
MAX_AMOUNT = 999_999_999
PAPER_MIN_MM = 80
PAPER_MAX_MM = 297
BASELINE_MARGIN = 1.5
FEED_MODES = ("landscape", "portrait_cw", "portrait_ccw")

# Logical invoice dimensions (the paper as read in landscape).
DEFAULT_PAPER_WIDTH = 195
DEFAULT_PAPER_HEIGHT = 103
# The form physically fed short-edge-first on the MP tray is 103 x 195.
FEED_PAPER_WIDTH = 103
FEED_PAPER_HEIGHT = 195

NUM_WORDS = "零壹貳參肆伍陸柒捌玖"


def _round_money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(value: float | str) -> Decimal:
    """Parse a user-typed amount, preserving pennies until Decimal half-up."""
    return Decimal(str(value).replace(",", "").strip()).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def money_text(value: Decimal) -> str:
    return f"{value:,.0f}"


def uppercase_twd(value: int) -> str:
    """Convert an integer TWD amount to the financial Chinese capital form."""
    if type(value) is not int or isinstance(value, bool):
        raise ValueError("總計須為0至999999999999的整數")
    if value < 0 or value > 999_999_999_999:
        raise ValueError("總計須為0至999999999999的整數")
    if value == 0:
        return "零元整"

    def group(number: int) -> str:
        result, zero = "", False
        for power, unit in ((1000, "仟"), (100, "佰"), (10, "拾"), (1, "")):
            digit, number = divmod(number, power)
            if digit:
                result += ("零" if zero else "") + NUM_WORDS[digit] + unit
                zero = False
            elif result and number:
                zero = True
        return result

    result, gap = "", False
    remaining = value
    for power, unit in ((100_000_000, "億"), (10_000, "萬"), (1, "")):
        part, remaining = divmod(remaining, power)
        if part:
            if result and (gap or part < 1000):
                result += "零"
            result += group(part) + unit
            gap = False
        elif result:
            gap = True
    return result + "元整"


INVOICE_AMOUNT_UNITS = ("億", "仟", "佰", "拾", "萬", "仟", "佰", "拾", "元")


def invoice_uppercase_digits(value: int) -> list[str]:
    """One capitalized digit for each preprinted amount unit on this invoice form."""
    if type(value) is not int or not 0 <= value <= MAX_AMOUNT:
        raise ValueError(f"總計須為0至{MAX_AMOUNT}的整數")
    return [NUM_WORDS[int(digit)] for digit in f"{value:09d}"]


def invoice_uppercase_twd(value: int) -> str:
    return "".join(
        digit + unit for digit, unit in zip(invoice_uppercase_digits(value), INVOICE_AMOUNT_UNITS)
    )


def roc_year(value: date) -> int:
    return value.year - 1911


class LineItem(BaseModel):
    description: str = ""
    quantity: str = ""
    unit: str = ""  # retained for old drafts/imports; the real form has no unit column
    unit_price: str = ""

    @field_validator("description", "quantity", "unit", "unit_price")
    @classmethod
    def clean_text(cls, value):
        return (value or "").strip()[:40]

    @property
    def numeric_quantity(self) -> Decimal | None:
        if not self.quantity:
            return None
        candidate = Decimal(self.quantity.replace(",", "").strip())
        return candidate if candidate > 0 else None

    @property
    def numeric_price(self) -> Decimal | None:
        if not self.unit_price:
            return None
        candidate = Decimal(self.unit_price.replace(",", "").strip())
        return candidate if candidate > 0 else None


class PrintDraft(BaseModel):
    buyer: str = ""
    tax_id: str = ""
    address: str = ""
    invoice_date: str
    items: list[LineItem] = Field(min_length=1, max_length=MAX_ITEMS)
    tax: int = 0
    note: str = ""

    @field_validator("buyer", "tax_id", "address", "note")
    @classmethod
    def clean_text(cls, value):
        return (value or "").strip()[:100]

    @field_validator("tax_id")
    @classmethod
    def validate_tax_id(cls, value):
        if value and not value.isdigit():
            raise ValueError("統一編號必須為數字")
        if value and len(value) != 8:
            raise ValueError("統一編號必須為8碼")
        return value

    @field_validator("invoice_date")
    @classmethod
    def validate_date(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("日期格式必須是YYYY-MM-DD") from exc
        if not (1912 <= parsed.year <= 2100):
            raise ValueError("年份超出可列印範圍")
        return value

    @field_validator("tax")
    @classmethod
    def validate_tax(cls, value):
        if value < 0 or value > MAX_AMOUNT:
            raise ValueError("營業稅超出範圍")
        return value

    @model_validator(mode="after")
    def validate_items(self):
        if not any(item.description for item in self.items):
            raise ValueError("至少要有一個品名")
        return self

    def invoice_date_obj(self) -> date:
        return date.fromisoformat(self.invoice_date)


def calculate(draft: PrintDraft) -> dict:
    """Compute amounts from decimals, then round every line to whole TWD."""
    amounts: list[int] = []
    subtotal_before_tax = Decimal("0")
    for item in draft.items:
        quantity = item.numeric_quantity or Decimal("1")
        price = item.numeric_price or Decimal("0")
        amount = _round_money(quantity * price)
        amounts.append(amount)
        subtotal_before_tax += Decimal(amount)
    total = subtotal_before_tax + Decimal(draft.tax)
    if total > MAX_AMOUNT:
        raise ValueError("總金額超出可列印範圍")
    parsed_date = draft.invoice_date_obj()
    return {
        "amounts": amounts,
        "subtotal": int(subtotal_before_tax),
        "tax": draft.tax,
        "total": int(total),
        "uppercase": invoice_uppercase_twd(int(total)),
        "uppercase_digits": invoice_uppercase_digits(int(total)),
        "roc_year": roc_year(parsed_date),
    }


class FieldSpec(BaseModel):
    x: float = Field(ge=0, le=PAPER_MAX_MM)
    y: float = Field(ge=0, le=PAPER_MAX_MM)
    width: float = Field(gt=0, le=PAPER_MAX_MM)
    height: float = Field(default=0.0, ge=0.0, le=PAPER_MAX_MM)  # 0 = single line
    font_size: float | None = Field(default=None, ge=8, le=24)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.x + self.width > PAPER_MAX_MM or self.y + self.height > PAPER_MAX_MM:
            raise ValueError("field outside paper")
        return self


# Field order matches the real 三聯式發票 (195 x 103 mm), measured from the
# 2026-10-23 sample: 品名 / 數量 / 單價 / 金額 / 備註 (no 單位 column).
FIELD_KEYS = (
    "buyer", "tax_id", "address", "year", "month", "day",
    "description", "quantity", "unit_price", "amount",
    "subtotal", "tax", "total", "uppercase", "note",
)


def real_invoice_fields() -> dict[str, FieldSpec]:
    """Coordinates measured from the real 336251 sample (195 x 103 mm)."""
    return {
        "buyer": FieldSpec(x=35, y=15.5, width=70),
        "tax_id": FieldSpec(x=35, y=20.8, width=48),
        "address": FieldSpec(x=35, y=24.5, width=145),
        "year": FieldSpec(x=105, y=20.8, width=8),
        "month": FieldSpec(x=120, y=20.8, width=8),
        "day": FieldSpec(x=137, y=20.8, width=8),
        "description": FieldSpec(x=20, y=38, width=37),
        "quantity": FieldSpec(x=60, y=38, width=15),
        "unit_price": FieldSpec(x=77, y=38, width=16),
        "amount": FieldSpec(x=95, y=38, width=29),
        "subtotal": FieldSpec(x=95, y=69, width=29),
        "tax": FieldSpec(x=95, y=75, width=29),
        "total": FieldSpec(x=95, y=81, width=29),
        "uppercase": FieldSpec(x=33, y=89, width=90),
        # 備註 is the right column (x125-175), spanning the five item rows and
        # the stamp zone; normally left blank for the seller's invoice stamp.
        "note": FieldSpec(x=127, y=38, width=46, height=52),
    }


class PrintLayout(BaseModel):
    paper_width: float = DEFAULT_PAPER_WIDTH
    paper_height: float = DEFAULT_PAPER_HEIGHT
    # Logical landscape content stays on 195x103; portrait_cw/ccw rotate only
    # for print so the CSS @page matches the registered 103x195 form.
    feed_mode: str = "portrait_cw"
    font: str = "11pt PMingLiU, Microsoft JhengHei, sans-serif"
    font_size: float = 11
    row_pitch: float = 6.1
    tax_id_spacing: float = 6.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    fields: dict[str, FieldSpec] = Field(default_factory=real_invoice_fields)

    @field_validator("font")
    @classmethod
    def validate_font(cls, value):
        candidate = value.strip()
        if not candidate or len(candidate) > 100:
            raise ValueError("請選擇合理的字體")
        if any(token in candidate.lower() for token in ("url(", "@", "javascript:", "</")):
            raise ValueError("字體設定不可包含外部資源")
        return candidate

    @field_validator("feed_mode")
    @classmethod
    def validate_feed_mode(cls, value):
        if value not in FEED_MODES:
            raise ValueError("進紙方向設定不正確")
        return value

    @field_validator("paper_width", "paper_height")
    @classmethod
    def validate_paper_dim(cls, value):
        if not (PAPER_MIN_MM <= value <= PAPER_MAX_MM):
            raise ValueError("紙張寬高必須介於80至297 mm")
        return value

    @field_validator("font_size")
    @classmethod
    def validate_font_size(cls, value):
        if not (8 <= value <= 16):
            raise ValueError("字級必須介於8至16 pt")
        return value

    @field_validator("row_pitch")
    @classmethod
    def validate_row_pitch(cls, value):
        if not (4 <= value <= 8):
            raise ValueError("列距必須介於4至8 mm")
        return value

    @field_validator("tax_id_spacing")
    @classmethod
    def validate_tax_spacing(cls, value):
        if not (0 <= value <= 8):
            raise ValueError("統編間距必須介於0至8 mm")
        return value

    @field_validator("offset_x", "offset_y")
    @classmethod
    def validate_offsets(cls, value):
        if abs(value) > 20:
            raise ValueError("校正位移最多±20 mm")
        return value

    @model_validator(mode="after")
    def validate_paper(self):
        if not (PAPER_MIN_MM <= self.paper_width <= PAPER_MAX_MM
                and PAPER_MIN_MM <= self.paper_height <= PAPER_MAX_MM):
            raise ValueError("紙張寬高必須介於80至300 mm")
        if set(self.fields) != set(FIELD_KEYS):
            raise ValueError("欄位組合不正確")
        for field in self.fields.values():
            # Single-line fields sit on a baseline; reserve a small descent
            # margin so the printed glyph is not clipped at the paper edge.
            bottom = field.y + field.height if field.height > 0 else field.y + BASELINE_MARGIN
            if field.x + field.width > self.paper_width or bottom > self.paper_height:
                raise ValueError("欄位超出紙張範圍")
        return self
