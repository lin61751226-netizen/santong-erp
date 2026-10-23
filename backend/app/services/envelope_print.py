"""Pure overprint calculation and layout for 直式中式信封 (default 100 x 200 mm).

Follows the 中華郵政「國內直式信封書寫範例」:
- 收件人 3+2 郵遞區號 five boxes: top-right
- 收件人地址: right-central vertical columns (writing-mode vertical-rl)
- 收件人姓名 + 收/啟: center, largest
- 寄件人地址 / 姓名 / 電話: left side, small vertical
- 寄件人 3+2 郵遞區號: bottom-left
- 郵票框: top-left (test grid only, not printed on formal envelopes)

Reuses the append-only InvoicePrintRevision table with kind
``envelope_layout`` / ``envelope_draft``; it never writes finance, HR, or
invoice data.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.invoice_print import (
    FieldSpec,
    PAPER_MAX_MM,
    PAPER_MIN_MM,
)

DEFAULT_PAPER_WIDTH = 100
DEFAULT_PAPER_HEIGHT = 200

ENVELOPE_FIELD_KEYS = (
    "receiver_postcode",
    "receiver_company",
    "receiver_address",
    "receiver_name",
    "sender_postcode",
    "sender_address",
    "sender_name",
    "sender_phone",
)
VERTICAL_FIELDS = {
    "receiver_company", "receiver_address", "receiver_name",
    "sender_address", "sender_name", "sender_phone",
}
POSTCODE_FIELDS = {"receiver_postcode", "sender_postcode"}


def default_envelope_fields() -> dict[str, FieldSpec]:
    """Coordinates for a 100 x 200 mm vertical Chinese envelope."""
    return {
        # Recipient postcode five boxes, top-right (3+2 with a small gap).
        "receiver_postcode": FieldSpec(x=52, y=9.5, width=42, height=7),
        # Recipient company (rightmost column, reading order), address, name.
        "receiver_company": FieldSpec(x=84, y=34, width=12, height=118),
        "receiver_address": FieldSpec(x=58, y=34, width=24, height=118),
        "receiver_name": FieldSpec(x=30, y=40, width=24, height=112),
        # Sender zone on the left.
        "sender_postcode": FieldSpec(x=8, y=178, width=30, height=7),
        "sender_address": FieldSpec(x=8, y=40, width=20, height=80),
        "sender_name": FieldSpec(x=8, y=121, width=20, height=41),
        "sender_phone": FieldSpec(x=8, y=164, width=50, height=8),
    }


class EnvelopeLayout(BaseModel):
    paper_width: float = DEFAULT_PAPER_WIDTH
    paper_height: float = DEFAULT_PAPER_HEIGHT
    font: str = "12pt PMingLiU, Microsoft JhengHei, sans-serif"
    font_size: float = 12.5        # recipient address / company
    name_font_size: float = 20     # recipient name
    small_font_size: float = 10    # sender block
    postcode_spacing: float = 6.5
    postcode_gap: float = 3.0      # extra gap between the 3rd and 4th boxes
    offset_x: float = 0.0
    offset_y: float = 0.0
    fields: dict[str, FieldSpec] = Field(default_factory=default_envelope_fields)

    @field_validator("font")
    @classmethod
    def validate_font(cls, value):
        candidate = value.strip()
        if not candidate or len(candidate) > 100:
            raise ValueError("請選擇合理的字體")
        if any(token in candidate.lower() for token in ("url(", "@", "javascript:", "</")):
            raise ValueError("字體設定不可包含外部資源")
        return candidate

    @field_validator("paper_width", "paper_height")
    @classmethod
    def validate_paper_dim(cls, value):
        if not (PAPER_MIN_MM <= value <= PAPER_MAX_MM):
            raise ValueError("紙張寬高必須介於80至297 mm")
        return value

    @field_validator("font_size")
    @classmethod
    def validate_font_size(cls, value):
        if not (6 <= value <= 24):
            raise ValueError("地址字級必須介於6至24 pt")
        return value

    @field_validator("name_font_size")
    @classmethod
    def validate_name_font_size(cls, value):
        if not (8 <= value <= 32):
            raise ValueError("姓名字級必須介於8至32 pt")
        return value

    @field_validator("small_font_size")
    @classmethod
    def validate_small_font_size(cls, value):
        if not (6 <= value <= 18):
            raise ValueError("小字級必須介於6至18 pt")
        return value

    @field_validator("postcode_spacing")
    @classmethod
    def validate_postcode_spacing(cls, value):
        if not (3 <= value <= 12):
            raise ValueError("郵遞區號間距必須介於3至12 mm")
        return value

    @field_validator("postcode_gap")
    @classmethod
    def validate_postcode_gap(cls, value):
        if not (0 <= value <= 8):
            raise ValueError("3+2間隙必須介於0至8 mm")
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
        if set(self.fields) != set(ENVELOPE_FIELD_KEYS):
            raise ValueError("欄位組合不正確")
        for field in self.fields.values():
            if field.x + field.width > self.paper_width or field.y + field.height > self.paper_height:
                raise ValueError("欄位超出紙張範圍")
        return self


class EnvelopeDraft(BaseModel):
    receiver_postcode: str = ""
    receiver_address: str = ""
    receiver_company: str = ""
    receiver_name: str = ""
    receiver_suffix: str = "收"
    sender_postcode: str = "33059"
    sender_address: str = "桃園市桃園區永安路385巷6號8樓之3"
    sender_name: str = "三通工程行 林金生"
    sender_suffix: str = "緘"
    sender_phone: str = "0978-909-078"

    @field_validator("receiver_address", "receiver_company", "receiver_name",
                     "receiver_suffix", "sender_address", "sender_name",
                     "sender_suffix", "sender_phone")
    @classmethod
    def clean_text(cls, value):
        return (value or "").strip()[:120]

    @field_validator("receiver_postcode", "sender_postcode")
    @classmethod
    def validate_postcode(cls, value):
        candidate = (value or "").replace("-", "").strip()
        if not candidate:
            return ""
        if not candidate.isdigit() or len(candidate) > 5:
            raise ValueError("郵遞區號最多5碼數字")
        return candidate

    @model_validator(mode="after")
    def validate_required(self):
        if not self.receiver_address:
            raise ValueError("請填收件人地址")
        if not self.receiver_name:
            raise ValueError("請填收件人姓名或公司")
        if not self.sender_name:
            raise ValueError("請填寄件人姓名或公司")
        return self


def render_lines(draft: EnvelopeDraft) -> dict:
    """Compose the final text for each zone."""
    receiver_name = f"{draft.receiver_name}{draft.receiver_suffix or ''}"
    sender_name = f"{draft.sender_name}{draft.sender_suffix or ''}"
    return {
        "receiver_company": draft.receiver_company,
        "receiver_address": draft.receiver_address,
        "receiver_name": receiver_name,
        "sender_address": draft.sender_address,
        "sender_name": sender_name,
        "sender_phone": draft.sender_phone,
    }
