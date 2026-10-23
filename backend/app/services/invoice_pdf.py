"""Single-page PDFs for the triplicate invoice overprint.

The browser keeps a 195×103 mm layout and, for short-edge feed, rotates it
onto a 103×195 mm page. This module bakes that same rotation into one PDF
page so a viewer can print at actual size. Toolbar, checkboxes, coordinates
and the reference photo are never drawn.

Microsoft PMingLiU is not redistributable. The embedded face is AR PL UMing TW
(subfont 2 of the vendored ``uming.ttc``), the public Ming face used in place
of PMingLiU. The screen preview still prefers a locally installed PMingLiU.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path

from reportlab import rl_config
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.services.invoice_print import (
    PrintDraft,
    PrintLayout,
    calculate,
)

FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "uming.ttc"
FONT_NAME = "ARPLUMingTW"
FONT_FACE_INDEX = 2  # AR PL UMing TW
LETTER_SPACING_EM = 0.02
PT_TO_MM = 25.4 / 72.0

_FONT_READY = False


def ensure_font() -> None:
    global _FONT_READY
    if _FONT_READY:
        return
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"缺少發票 PDF 字型：{FONT_PATH.name}")
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH), subfontIndex=FONT_FACE_INDEX))
    _FONT_READY = True


def physical_page_mm(layout: PrintLayout) -> tuple[float, float]:
    """Width, height of the CSS @page, in millimetres."""
    if layout.feed_mode in ("portrait_cw", "portrait_ccw"):
        return layout.paper_height, layout.paper_width
    return layout.paper_width, layout.paper_height


def css_page_point(mode: str, x: float, y: float, paper_w: float, paper_h: float) -> tuple[float, float]:
    """Map a logical top-left point onto the physical page (y grows downward).

    Matches the print CSS: CW ``rotate(90deg) translateY(-100%)`` sends
    local (x, y) to (paper_h - y, x); CCW sends it to (y, paper_w - x).
    """
    if mode == "portrait_cw":
        return paper_h - y, x
    if mode == "portrait_ccw":
        return y, paper_w - x
    return x, y


def _install_transform(pdf: canvas.Canvas, layout: PrintLayout) -> None:
    """User space is millimetres, origin at the logical top-left, y upward."""
    page_w, page_h = physical_page_mm(layout)
    paper_w, paper_h = layout.paper_width, layout.paper_height
    if layout.feed_mode == "portrait_cw":
        # pdf = ((paper_h - ly) * mm, (page_h - lx) * mm), ly = -user_y
        pdf.transform(0, -mm, mm, 0, paper_h * mm, page_h * mm)
    elif layout.feed_mode == "portrait_ccw":
        pdf.transform(0, mm, -mm, 0, 0, (page_h - paper_w) * mm)
    else:
        pdf.transform(mm, 0, 0, mm, 0, paper_h * mm)


def _em_fractions() -> tuple[float, float]:
    face = pdfmetrics.getFont(FONT_NAME).face
    return face.ascent / 1000.0, face.descent / 1000.0


def _font_mm(font_pt: float) -> float:
    return font_pt * PT_TO_MM


def _text_width(text: str, font_pt: float) -> float:
    size = _font_mm(font_pt)
    width = pdfmetrics.stringWidth(text, FONT_NAME, size)
    if len(text) > 1:
        width += LETTER_SPACING_EM * size * (len(text) - 1)
    return width


def _baseline_from_top(top: float, font_pt: float, line_height: float) -> float:
    """CSS line box top → baseline, y growing downward."""
    ascent, _descent = _em_fractions()
    size = _font_mm(font_pt)
    half_leading = (line_height - 1) / 2 * size
    return top + half_leading + ascent * size


def _baseline_centered(top: float, height: float, font_pt: float) -> float:
    ascent, descent = _em_fractions()
    size = _font_mm(font_pt)
    center = top + height / 2
    return center + (ascent + descent) / 2 * size


def _money_display(value: str | int | float) -> str:
    text = str(value).replace(",", "").strip()
    if not text:
        return ""
    number = Decimal(text).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(number):,}"


def content_runs(layout: PrintLayout, draft: PrintDraft, result: dict) -> list[dict]:
    """Field runs in logical millimetres. Mirrors invoice_overprint.js positionedFields."""
    parsed = draft.invoice_date_obj()
    fields = layout.fields
    runs: list[dict] = []

    def add(key: str, text: str, *, row: int | None = None, align: str | None = None,
            slots: bool = False, multiline: bool = False):
        if not text:
            return
        field = fields[key]
        y = field.y + layout.offset_y
        if row is not None:
            y += row * layout.row_pitch
        numeric = key in {"unit_price", "amount", "subtotal", "tax", "total", "month", "day"}
        runs.append({
            "key": key,
            "text": text,
            "x": round(field.x + layout.offset_x, 2),
            "y": round(y, 2),
            "width": field.width,
            "height": field.height or 0,
            "font_pt": field.font_size or layout.font_size,
            "align": align or ("end" if numeric else "start"),
            "slots": slots,
            "multiline": multiline,
        })

    add("buyer", draft.buyer)
    if draft.tax_id:
        for index, char in enumerate(draft.tax_id):
            field = fields["tax_id"]
            runs.append({
                "key": "tax_id",
                "text": char,
                "x": round(field.x + layout.offset_x + index * layout.tax_id_spacing, 2),
                "y": round(field.y + layout.offset_y, 2),
                "width": layout.tax_id_spacing,
                "height": field.height or 0,
                "font_pt": field.font_size or layout.font_size,
                "align": "center",
                "slots": False,
                "multiline": False,
            })
    add("address", draft.address)
    add("month", str(parsed.month))
    add("day", str(parsed.day))
    for index, item in enumerate(draft.items):
        add("description", item.description, row=index)
        add("quantity", item.quantity, row=index)
        if item.unit_price:
            add("unit_price", _money_display(item.unit_price), row=index)
        amount = result["amounts"][index]
        if amount:
            add("amount", _money_display(amount), row=index)
    add("subtotal", _money_display(result["subtotal"]))
    add("tax", _money_display(result["tax"]))
    add("total", _money_display(result["total"]))
    add("uppercase", "".join(result["uppercase_digits"]), slots=True)
    add("note", draft.note, multiline=True)
    return runs


def _draw_text(pdf: canvas.Canvas, text: str, x: float, baseline: float, font_pt: float) -> None:
    size = _font_mm(font_pt)
    pdf.setFillColorRGB(0, 0, 0)
    text_object = pdf.beginText(x, -baseline)
    text_object.setFont(FONT_NAME, size)
    text_object.setCharSpace(LETTER_SPACING_EM * size if len(text) > 1 else 0)
    text_object.textOut(text)
    pdf.drawText(text_object)


def _draw_run(pdf: canvas.Canvas, run: dict) -> None:
    font_pt = run["font_pt"]
    if run["slots"]:
        baseline = _baseline_from_top(run["y"], font_pt, 1.2)
        slot = run["width"] / 9
        for index, char in enumerate(run["text"][:9]):
            width = _text_width(char, font_pt)
            _draw_text(pdf, char, run["x"] + (index + 0.5) * slot - width / 2, baseline, font_pt)
        return
    if run["multiline"]:
        _draw_note(pdf, run)
        return
    if run["height"]:
        baseline = _baseline_centered(run["y"], run["height"], font_pt)
    else:
        baseline = _baseline_from_top(run["y"], font_pt, 1.2)
    width = _text_width(run["text"], font_pt)
    x = run["x"]
    if run["align"] == "end":
        x = run["x"] + run["width"] - width
    elif run["align"] == "center":
        x = run["x"] + (run["width"] - width) / 2
    _draw_text(pdf, run["text"], x, baseline, font_pt)


def _wrap_note(text: str, width_mm: float, font_pt: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        trial = current + char
        if current and _text_width(trial, font_pt) > width_mm:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _draw_note(pdf: canvas.Canvas, run: dict) -> None:
    font_pt = run["font_pt"]
    line = 1.25 * _font_mm(font_pt)
    y = run["y"]
    bottom = run["y"] + (run["height"] or line)
    for part in _wrap_note(run["text"], run["width"], font_pt):
        if y >= bottom:
            break
        baseline = _baseline_from_top(y, font_pt, 1.25)
        _draw_text(pdf, part, run["x"], baseline, font_pt)
        y += line


def _draw_calibration(pdf: canvas.Canvas, layout: PrintLayout) -> None:
    paper_w, paper_h = layout.paper_width, layout.paper_height
    pdf.setStrokeColorRGB(0.70, 0.75, 0.82)
    pdf.setLineWidth(0.08)
    x = 0.0
    while x <= paper_w + 0.01:
        pdf.line(x, 0, x, -paper_h)
        x += 2
    y = 0.0
    while y <= paper_h + 0.01:
        pdf.line(0, -y, paper_w, -y)
        y += 2
    pdf.setFillColorRGB(0.48, 0.53, 0.61)
    label_pt = 6
    cells_x = round(paper_w / 2)
    cells_y = round(paper_h / 2)
    for index in range(0, cells_x + 1, 5):
        _draw_text(pdf, str(index), index * 2 + 0.3, _baseline_from_top(0.2, label_pt, 1), label_pt)
    for index in range(5, cells_y + 1, 5):
        _draw_text(pdf, str(index), 0.3, _baseline_from_top(index * 2, label_pt, 1), label_pt)
    pdf.setStrokeColorRGB(0.2, 0.2, 0.2)
    pdf.setLineWidth(0.2)
    mark = 4
    for x0, y0, x1, y1 in (
        (0, 0, mark, 0), (0, 0, 0, -mark),
        (paper_w - mark, 0, paper_w, 0), (paper_w, 0, paper_w, -mark),
        (0, -paper_h, mark, -paper_h), (0, -paper_h + mark, 0, -paper_h),
        (paper_w - mark, -paper_h, paper_w, -paper_h), (paper_w, -paper_h + mark, paper_w, -paper_h),
    ):
        pdf.line(x0, y0, x1, y1)
    title = f"三通工程行｜{paper_w:g} × {paper_h:g} mm 白紙校正 v3"
    if layout.feed_mode == "landscape":
        subtitle = "橫式直出／非正式發票／不可請款"
    else:
        subtitle = "直式103×195（已旋轉）／非正式發票／不可請款"
    pdf.setFillColorRGB(0, 0, 0)
    _draw_text(pdf, title, 2, _baseline_from_top(1, 12, 1.2), 12)
    subtitle_x = paper_w - 2 - _text_width(subtitle, 8)
    _draw_text(pdf, subtitle, subtitle_x, _baseline_from_top(1.5, 8, 1.2), 8)


def _clip_logical(pdf: canvas.Canvas, layout: PrintLayout) -> None:
    paper_w, paper_h = layout.paper_width, layout.paper_height
    path = pdf.beginPath()
    path.moveTo(0, 0)
    path.lineTo(paper_w, 0)
    path.lineTo(paper_w, -paper_h)
    path.lineTo(0, -paper_h)
    path.close()
    pdf.clipPath(path, stroke=0, fill=0)


def render_invoice_pdf(layout: PrintLayout, draft: PrintDraft | None = None, *,
                        calibration: bool = False) -> bytes:
    """Return one PDF page. Does not touch drafts, layouts, or the database."""
    ensure_font()
    result = None
    if not calibration:
        if draft is None:
            raise ValueError("正式發票 PDF 需要本次內容")
        result = calculate(draft)
    page_w, page_h = physical_page_mm(layout)
    buffer = BytesIO()
    previous_ascii85 = rl_config.useA85
    rl_config.useA85 = 0
    try:
        pdf = canvas.Canvas(buffer, pagesize=(page_w * mm, page_h * mm))
        if calibration:
            pdf.setTitle(f"三通發票白紙校正 {page_w:g}x{page_h:g}mm")
        else:
            pdf.setTitle(f"三通發票套印 {page_w:g}x{page_h:g}mm")
        pdf.setAuthor("三通工程行")
        pdf.saveState()
        _install_transform(pdf, layout)
        _clip_logical(pdf, layout)
        if calibration:
            _draw_calibration(pdf, layout)
        else:
            for run in content_runs(layout, draft, result):
                _draw_run(pdf, run)
        pdf.restoreState()
        pdf.save()
    finally:
        rl_config.useA85 = previous_ascii85
    return buffer.getvalue()
