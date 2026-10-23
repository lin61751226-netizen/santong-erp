"""Single-page PDFs for the 100×200 mm vertical envelope.

The browser lays the envelope out in page coordinates (no extra rotation).
Sender columns use the same per-character cells as the preview: every glyph,
including digits, is its own 1.15em box, and the phone string is rotated
-90° underneath with a gap so it does not touch 「電話：」. Receiver columns
follow vertical-rl: upright characters, top to bottom, columns right to left.
Postcode digits stay in their own 3+2 boxes.

The embedded face is AR PL UMing TW, the same public Ming face as the invoice
PDF. Microsoft PMingLiU is not redistributable.
"""

from __future__ import annotations

import math
import re
from io import BytesIO

from reportlab import rl_config
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from app.services.envelope_print import EnvelopeDraft, EnvelopeLayout, render_lines
from app.services.invoice_pdf import (
    FONT_NAME,
    _baseline_centered,
    _draw_text,
    _em_fractions,
    _font_mm,
    ensure_font,
)

GLYPH_EM = 1.15
PHONE_GAP_EM = 0.45
MM_PER_PT = 0.3528  # same constant the preview uses to fit a column
_PHONE_TAIL = re.compile(r"^(.*?)([0-9０-９][0-9０-９\-－—–]*)$")


def _pt_mm(font_pt: float) -> float:
    return font_pt * MM_PER_PT


def _hyphen(ch: str) -> bool:
    return ch in "-－—–"


def column_parts(text: str, sideways_phone: bool = False) -> tuple[list[str], str]:
    source = re.sub(r"電話\s*[:：]", "電話：", text or "")
    if not sideways_phone:
        return list(source), ""
    match = _PHONE_TAIL.match(source)
    if not match or not match.group(2):
        return list(source), ""
    return list(match.group(1)), match.group(2)


def phone_advance_em(phone: str) -> float:
    em = PHONE_GAP_EM + 0.25
    for ch in phone:
        em += 0.45 if _hyphen(ch) else 0.62
    return em


def vertical_advance_em(text: str, sideways_phone: bool = False) -> float:
    glyphs, phone = column_parts(text, sideways_phone)
    em = len(glyphs) * GLYPH_EM
    if phone:
        em += phone_advance_em(phone)
    return em


def vertical_font_size(text: str, height_mm: float, max_pt: float, sideways_phone: bool = False) -> float:
    em = vertical_advance_em(text, sideways_phone)
    cap = float(max_pt)
    if not em or not (height_mm > 0) or not math.isfinite(cap):
        return cap
    usable = max(1.0, float(height_mm) - 1)
    fitted = usable / (em * MM_PER_PT)
    return round(min(cap, fitted) * 10) / 10


def _font_cap(key: str, layout: EnvelopeLayout) -> float:
    if key == "receiver_name":
        return layout.name_font_size
    if key.startswith("sender"):
        return layout.small_font_size
    return layout.font_size


def sender_boxes(text: str, x: float, y: float, width: float, height: float, font_pt: float, *,
                 sideways_phone: bool = False) -> list[dict]:
    """One non-overlapping cell per character, in millimetres, y growing downward."""
    ensure_font()
    size = _pt_mm(font_pt)
    cell = GLYPH_EM * size
    glyphs, phone = column_parts(text, sideways_phone)
    boxes: list[dict] = []
    cursor = y
    center = x + width / 2
    for ch in glyphs:
        boxes.append({
            "text": ch, "x": center - cell / 2, "y": cursor, "w": cell, "h": cell, "sideways": False,
            "font_pt": font_pt,
        })
        cursor += cell
    if not phone:
        return boxes
    # The preview sizes .phone-run from the fit budget, then centers the
    # unrotated string and applies rotate(-90deg). Character pitch is the
    # font advance (letter-spacing 0), not the 0.62em / 0.45em fit budget.
    run_top = cursor + PHONE_GAP_EM * size
    run_h = (phone_advance_em(phone) - PHONE_GAP_EM) * size
    advances = [pdfmetrics.stringWidth(ch, FONT_NAME, _font_mm(font_pt)) for ch in phone]
    ink_h = sum(advances)
    ink_top = run_top + max(0.0, (run_h - ink_h) / 2)
    line = size  # the 1em line box, centered in the 1.15em column
    left = center - line / 2
    top = ink_top + ink_h
    for ch, advance in zip(phone, advances):
        top -= advance
        boxes.append({
            "text": ch, "x": left, "y": top, "w": line, "h": advance, "sideways": True,
            "font_pt": font_pt,
        })
    return boxes


def vertical_rl_font_size(text: str, width: float, height: float, max_pt: float) -> float:
    """Shrink upright vertical-rl text until every character stays inside the field."""
    if not text:
        return float(max_pt)
    pt = float(max_pt)
    while pt > 5:
        size = _pt_mm(pt)
        cell_h = size * 1.04
        cell_w = size * 1.12
        if cell_h <= height and cell_w <= width:
            per_column = max(1, int(height // cell_h))
            columns = math.ceil(len(text) / per_column)
            if columns * cell_w <= width + 0.05:
                return round(pt, 1)
        pt = round(pt - 0.1, 1)
    return round(pt, 1)


def vertical_rl_boxes(text: str, x: float, y: float, width: float, height: float, font_pt: float) -> list[dict]:
    size = _pt_mm(font_pt)
    cell_h = size * 1.04
    cell_w = size * 1.12
    per_column = max(1, int(height // cell_h)) if cell_h else 1
    boxes = []
    for index, ch in enumerate(text):
        column, row = divmod(index, per_column)
        left = x + width - (column + 1) * cell_w
        boxes.append({
            "text": ch, "x": left, "y": y + row * cell_h, "w": cell_w, "h": cell_h,
            "sideways": False, "font_pt": font_pt,
        })
    return boxes


def postcode_boxes(raw: str, field_x: float, field_y: float, spacing: float, gap: float,
                   font_pt: float) -> list[dict]:
    chars = re.sub(r"\D", "", raw or "")[:5]
    boxes = []
    line = _pt_mm(font_pt)
    for index, ch in enumerate(chars):
        x = field_x + index * spacing + (gap if index >= 3 else 0)
        boxes.append({
            "text": ch, "x": x, "y": field_y, "w": spacing, "h": line,
            "sideways": False, "font_pt": font_pt,
        })
    return boxes


def layout_boxes(layout: EnvelopeLayout, draft: EnvelopeDraft) -> list[dict]:
    """Every painted character. Empty zones are omitted. Sender phone is not a second column."""
    lines = render_lines(draft)
    ox, oy = layout.offset_x, layout.offset_y
    boxes: list[dict] = []

    def place_postcode(key: str, raw: str):
        field = layout.fields[key]
        boxes.extend(postcode_boxes(
            raw, field.x + ox, field.y + oy,
            layout.postcode_spacing, layout.postcode_gap, _font_cap(key, layout),
        ))

    place_postcode("receiver_postcode", draft.receiver_postcode)
    place_postcode("sender_postcode", draft.sender_postcode)
    for key in ("receiver_company", "receiver_address", "receiver_name"):
        text = lines[key]
        if not text:
            continue
        field = layout.fields[key]
        font_pt = vertical_rl_font_size(text, field.width, field.height, _font_cap(key, layout))
        boxes.extend(vertical_rl_boxes(
            text, field.x + ox, field.y + oy, field.width, field.height, font_pt,
        ))
    address = lines["sender_address"]
    if address:
        field = layout.fields["sender_address"]
        font_pt = vertical_font_size(address, field.height, _font_cap("sender_address", layout))
        boxes.extend(sender_boxes(
            address, field.x + ox, field.y + oy, field.width, field.height, font_pt,
        ))
    name = lines["sender_name"]
    if name:
        field = layout.fields["sender_name"]
        font_pt = vertical_font_size(
            name, field.height, _font_cap("sender_name", layout), sideways_phone=True,
        )
        boxes.extend(sender_boxes(
            name, field.x + ox, field.y + oy, field.width, field.height, font_pt, sideways_phone=True,
        ))
    return boxes


def _draw_upright(pdf: canvas.Canvas, box: dict) -> None:
    ink = pdfmetrics.stringWidth(box["text"], FONT_NAME, _font_mm(box["font_pt"]))
    baseline = _baseline_centered(box["y"], box["h"], box["font_pt"])
    _draw_text(pdf, box["text"], box["x"] + (box["w"] - ink) / 2, baseline, box["font_pt"])


def _draw_sideways(pdf: canvas.Canvas, box: dict) -> None:
    """CSS rotate(-90deg): the start of a horizontal run sits toward the bottom of its slot."""
    font_pt = box["font_pt"]
    size = _font_mm(font_pt)
    ink = pdfmetrics.stringWidth(box["text"], FONT_NAME, size)
    ascent, descent = _em_fractions()
    cx = box["x"] + box["w"] / 2
    cy = box["y"] + box["h"] / 2
    pdf.saveState()
    pdf.translate(cx, -cy)
    pdf.rotate(90)
    text_object = pdf.beginText(-ink / 2, -(ascent + descent) / 2 * size)
    text_object.setFont(FONT_NAME, size)
    text_object.textOut(box["text"])
    pdf.drawText(text_object)
    pdf.restoreState()


def _clip(pdf: canvas.Canvas, layout: EnvelopeLayout) -> None:
    path = pdf.beginPath()
    path.moveTo(0, 0)
    path.lineTo(layout.paper_width, 0)
    path.lineTo(layout.paper_width, -layout.paper_height)
    path.lineTo(0, -layout.paper_height)
    path.close()
    pdf.clipPath(path, stroke=0, fill=0)


def _draw_calibration(pdf: canvas.Canvas, layout: EnvelopeLayout) -> None:
    width, height = layout.paper_width, layout.paper_height
    pdf.setStrokeColorRGB(0.70, 0.75, 0.82)
    pdf.setLineWidth(0.08)
    x = 0.0
    while x <= width + 0.01:
        pdf.line(x, 0, x, -height)
        x += 2
    y = 0.0
    while y <= height + 0.01:
        pdf.line(0, -y, width, -y)
        y += 2
    pdf.setStrokeColorRGB(0.77, 0.42, 0.42)
    pdf.setLineWidth(0.25)
    pdf.setDash(1, 1)
    pdf.rect(8, -8 - 18, 18, 18, stroke=1, fill=0)
    pdf.setDash()
    _draw_text(pdf, "貼郵票處", 9.2, _baseline_centered(13, 8, 9), 9)
    for key in ("receiver_postcode", "sender_postcode"):
        field = layout.fields[key]
        for index in range(5):
            left = field.x + index * layout.postcode_spacing + (layout.postcode_gap if index >= 3 else 0)
            top = field.y - 0.8
            box = layout.postcode_spacing - 0.4
            pdf.rect(left, -(top + box), box, box, stroke=1, fill=0)
    pdf.setStrokeColorRGB(0.2, 0.2, 0.2)
    pdf.setFillColorRGB(0, 0, 0)
    mark = 4
    for x0, y0, x1, y1 in (
        (0, 0, mark, 0), (0, 0, 0, -mark),
        (width - mark, 0, width, 0), (width, 0, width, -mark),
        (0, -height, mark, -height), (0, -height + mark, 0, -height),
        (width - mark, -height, width, -height), (width, -height + mark, width, -height),
    ):
        pdf.line(x0, y0, x1, y1)
    title = f"三通工程行｜直式信封 {width:g}×{height:g} mm 白紙校正"
    _draw_text(pdf, title, 2, _baseline_centered(height - 6, 4, 8), 8)


def render_envelope_pdf(layout: EnvelopeLayout, draft: EnvelopeDraft | None = None, *,
                         calibration: bool = False) -> bytes:
    """One PDF page. Does not save a layout, a draft, or a backup."""
    ensure_font()
    if not calibration and draft is None:
        raise ValueError("正式信封 PDF 需要本次內容")
    buffer = BytesIO()
    previous_ascii85 = rl_config.useA85
    rl_config.useA85 = 0
    try:
        pdf = canvas.Canvas(buffer, pagesize=(layout.paper_width * mm, layout.paper_height * mm))
        kind = "白紙校正" if calibration else "套印"
        pdf.setTitle(f"三通信封{kind} {layout.paper_width:g}x{layout.paper_height:g}mm")
        pdf.setAuthor("三通工程行")
        pdf.saveState()
        pdf.transform(mm, 0, 0, mm, 0, layout.paper_height * mm)
        _clip(pdf, layout)
        if calibration:
            _draw_calibration(pdf, layout)
        else:
            for box in layout_boxes(layout, draft):
                if box["sideways"]:
                    _draw_sideways(pdf, box)
                else:
                    _draw_upright(pdf, box)
        pdf.restoreState()
        pdf.save()
    finally:
        rl_config.useA85 = previous_ascii85
    return buffer.getvalue()
