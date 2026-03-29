from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

import qrcode


def _wrap_text(text: str, font_name: str, font_size: int, max_width: float) -> list[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _shadow_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    radius: float = 12,
    shadow_dx: float = 2.5,
    shadow_dy: float = -2.5,
    shadow_color: HexColor = HexColor("#cbd5e1"),
    fill_color: HexColor = HexColor("#ffffff"),
    stroke_color: HexColor = HexColor("#e2e8f0"),
) -> None:
    c.setFillColor(shadow_color)
    c.setStrokeColor(shadow_color)
    c.roundRect(x + shadow_dx, y + shadow_dy, w, h, radius=radius, stroke=0, fill=1)
    c.setFillColor(fill_color)
    c.setStrokeColor(stroke_color)
    c.roundRect(x, y, w, h, radius=radius, stroke=1, fill=1)


def _badge(
    c: canvas.Canvas,
    x: float,
    y: float,
    text: str,
    *,
    font_size: int = 8,
    fg: HexColor = HexColor("#334155"),
    bg: HexColor = HexColor("#f1f5f9"),
    border: HexColor = HexColor("#e2e8f0"),
    pad_x: float = 6,
    pad_y: float = 3,
    radius: float = 9,
) -> float:
    c.setFont("Helvetica", font_size)
    text_w = stringWidth(text, "Helvetica", font_size)
    w = text_w + (pad_x * 2)
    h = font_size + (pad_y * 2)
    c.setFillColor(bg)
    c.setStrokeColor(border)
    c.roundRect(x, y - h + 2, w, h, radius=radius, stroke=1, fill=1)
    c.setFillColor(fg)
    c.drawString(x + pad_x, y - h + pad_y + 3, text)
    return w


def _draw_wrapped(
    c: canvas.Canvas,
    text: str,
    x: float,
    y_top: float,
    max_width: float,
    *,
    font_name: str,
    font_size: int,
    leading: Optional[float] = None,
    color: Optional[HexColor] = None,
) -> float:
    c.setFont(font_name, font_size)
    if color is not None:
        c.setFillColor(color)
    line_height = leading if leading is not None else (font_size * 1.25)
    y = y_top
    for para in (text or "").split("\n"):
        lines = _wrap_text(para.strip(), font_name, font_size, max_width)
        for line in lines:
            c.drawString(x, y, line)
            y -= line_height
        y -= (line_height * 0.3)
    return y


def _draw_bullets(
    c: canvas.Canvas,
    items: list[str],
    x: float,
    y_top: float,
    max_width: float,
    *,
    font_name: str = "Helvetica",
    font_size: int = 9,
    bullet_color: HexColor = HexColor("#64748b"),
    text_color: HexColor = HexColor("#0f172a"),
    leading: Optional[float] = None,
) -> float:
    c.setFont(font_name, font_size)
    c.setFillColor(text_color)
    line_height = leading if leading is not None else (font_size * 1.35)
    y = y_top
    bullet_r = 1.4
    gap = 6
    for item in items:
        # Reserve a small bullet column.
        bullet_x = x + bullet_r
        text_x = x + (bullet_r * 2) + gap
        lines = _wrap_text(item, font_name, font_size, max_width - (text_x - x))
        c.setFillColor(bullet_color)
        c.circle(bullet_x, y - (font_size * 0.25), bullet_r, stroke=0, fill=1)
        c.setFillColor(text_color)
        for idx, line in enumerate(lines):
            c.drawString(text_x, y - (idx * line_height), line)
        y -= max(line_height, line_height * len(lines)) + (line_height * 0.2)
    return y


def generate_partner_pack_pdf(
    *,
    site_base_url: str,
    partner_code: Optional[str] = None,
    property_name: Optional[str] = None,
    commission_percent: int = 20,
) -> bytes:
    """
    Returns a one-page corporate "Partner Pack" PDF brochure (A4).
    This is intentionally not a website print/export; it is a designed document.
    """

    partner_code = (partner_code or "").strip() or None
    property_name = (property_name or "").strip() or None
    safe_site = (site_base_url or "").strip().rstrip("/") or "https://www.jetskiandmore.com"

    params: dict[str, str] = {
        "utm_source": "partner",
        "utm_medium": "qr",
        "utm_campaign": "partner_pack",
    }
    if partner_code:
        params["partnerCode"] = partner_code
        params["utm_content"] = partner_code
    if property_name:
        params["property"] = property_name

    qr_url = f"{safe_site}/Bookings?{urlencode(params)}"

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=8)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_pil = qr_img.get_image() if hasattr(qr_img, "get_image") else qr_img
    qr_reader = ImageReader(qr_pil)

    logo_path = Path(__file__).resolve().parent / "assets" / "JetSkiLogo.png"
    logo_reader = ImageReader(str(logo_path)) if logo_path.exists() else None

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    page_margin = 10 * mm
    margin = 14 * mm
    accent = HexColor("#7c3aed")
    slate_900 = HexColor("#0f172a")
    slate_700 = HexColor("#334155")
    slate_600 = HexColor("#475569")
    slate_500 = HexColor("#64748b")
    border = HexColor("#e2e8f0")
    panel = HexColor("#f8fafc")
    page_bg = HexColor("#f1f5f9")

    # Page background
    c.setFillColor(page_bg)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    # Main card
    card_x = page_margin
    card_y = page_margin
    card_w = width - (page_margin * 2)
    card_h = height - (page_margin * 2)
    _shadow_card(c, card_x, card_y, card_w, card_h, radius=14)

    # Accent bar inside the card
    c.setFillColor(accent)
    c.roundRect(card_x, card_y + card_h - 6, card_w, 6, radius=14, stroke=0, fill=1)

    inner_pad = 12 * mm
    inner_left = card_x + inner_pad
    inner_right = card_x + card_w - inner_pad
    inner_top = card_y + card_h - inner_pad - 2
    inner_bottom = card_y + inner_pad

    # Header
    header_top = inner_top
    left_x = inner_left
    right_x = inner_right

    logo_h = 13 * mm
    logo_w = 36 * mm
    logo_y = header_top - logo_h
    if logo_reader is not None:
        c.drawImage(logo_reader, left_x, logo_y, width=logo_w, height=logo_h, mask="auto", preserveAspectRatio=True)

    title_x = left_x + (logo_w + 8) if logo_reader is not None else left_x
    c.setFillColor(slate_900)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(title_x, header_top - 2, "Jet Ski & More")
    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(accent)
    c.drawString(title_x, header_top - 18, "Corporate Partner Pack")
    c.setFont("Helvetica", 10)
    c.setFillColor(slate_600)
    c.drawString(title_x, header_top - 32, "Tourism-ready guided water activities from Gordon's Bay Harbour (False Bay).")
    c.setFont("Helvetica", 9)
    c.setFillColor(slate_500)
    c.drawString(title_x, header_top - 45, "Professional briefing, onboarding, and weather-led operating procedures.")

    if property_name:
        # Small "Prepared for" badge
        badge_y = header_top - 60
        badge_x = title_x
        w1 = _badge(c, badge_x, badge_y, "Prepared for", fg=HexColor("#475569"), bg=HexColor("#ffffff"), border=border)
        _badge(
            c,
            badge_x + w1 + 6,
            badge_y,
            property_name,
            fg=slate_900,
            bg=HexColor("#ffffff"),
            border=border,
        )

    # QR card on the right
    qr_size = 32 * mm
    qr_card_w = 74 * mm
    qr_card_h = 42 * mm
    qr_card_x = right_x - qr_card_w
    qr_card_y = header_top - qr_card_h
    c.setFillColor(HexColor("#ffffff"))
    c.setStrokeColor(border)
    c.roundRect(qr_card_x, qr_card_y, qr_card_w, qr_card_h, radius=10, stroke=1, fill=1)

    # Subtle top stripe
    c.setFillColor(panel)
    c.roundRect(qr_card_x, qr_card_y + qr_card_h - 12, qr_card_w, 12, radius=10, stroke=0, fill=1)
    c.setFillColor(slate_700)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(qr_card_x + 10, qr_card_y + qr_card_h - 9, "Scan to book / enquire")

    c.drawImage(qr_reader, qr_card_x + 10, qr_card_y + 6, width=qr_size, height=qr_size, mask="auto")
    text_block_x = qr_card_x + 8 + qr_size + 8
    text_block_w = qr_card_w - (text_block_x - qr_card_x) - 8

    c.setFont("Helvetica", 8)
    c.setFillColor(slate_600)
    _draw_wrapped(
        c,
        "Booking link:",
        text_block_x,
        qr_card_y + qr_card_h - 22,
        text_block_w,
        font_name="Helvetica",
        font_size=8,
        leading=10,
        color=slate_600,
    )
    c.setFont("Courier", 8)
    c.setFillColor(slate_900)
    _draw_wrapped(
        c,
        f"{safe_site.replace('https://', '').replace('http://', '')}/Bookings",
        text_block_x,
        qr_card_y + qr_card_h - 34,
        text_block_w,
        font_name="Courier",
        font_size=8,
        leading=10,
        color=slate_900,
    )
    c.setFont("Helvetica", 8)
    c.setFillColor(slate_500)
    c.drawString(text_block_x, qr_card_y + 14, "+27 (074) 658 8885")
    c.drawString(text_block_x, qr_card_y + 4, "info@jetskiandmore.com")

    # Divider under header
    badge_row_y = qr_card_y - 18
    bx = left_x
    bw = _badge(c, bx, badge_row_y, "Tourism-ready", fg=slate_700, bg=panel, border=border)
    bx += bw + 6
    bw = _badge(c, bx, badge_row_y, "Safety-led procedures", fg=slate_700, bg=panel, border=border)
    bx += bw + 6
    bw = _badge(c, bx, badge_row_y, "Weather dependent", fg=slate_700, bg=panel, border=border)
    bx += bw + 6
    _badge(c, bx, badge_row_y, "Gordon's Bay Harbour", fg=slate_700, bg=panel, border=border)

    y = badge_row_y - 16
    c.setStrokeColor(border)
    c.line(inner_left, y, inner_right, y)

    # 3-column panels
    panel_gap = 8 * mm
    col_w = (card_w - (inner_pad * 2) - (panel_gap * 2)) / 3.0
    panel_h = 96 * mm
    panel_y = y - panel_h - 8

    def draw_panel(x: float, title: str) -> float:
        # Small shadow for panel
        c.setFillColor(HexColor("#e2e8f0"))
        c.roundRect(x + 1.5, panel_y - 1.5, col_w, panel_h, radius=12, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.setStrokeColor(border)
        c.roundRect(x, panel_y, col_w, panel_h, radius=12, stroke=1, fill=1)
        # Accent indicator
        c.setFillColor(accent)
        c.roundRect(x + 10, panel_y + panel_h - 16, 24, 4, radius=2, stroke=0, fill=1)
        c.setFillColor(slate_900)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x + 10, panel_y + panel_h - 28, title)
        c.setStrokeColor(border)
        c.line(x + 10, panel_y + panel_h - 32, x + col_w - 10, panel_y + panel_h - 32)
        return panel_y + panel_h - 46

    col1_x = inner_left
    col2_x = inner_left + col_w + panel_gap
    col3_x = inner_left + (col_w + panel_gap) * 2

    y1 = draw_panel(col1_x, "Experiences guests can book")
    y1 = _draw_bullets(
        c,
        [
            "Guided jet ski rides (seasonal; weather-dependent).",
            "Boat rides (spectator / harbour + bay).",
            "Fishing charter enquiries (availability-confirmed).",
        ],
        col1_x + 10,
        y1,
        col_w - 20,
        font_size=9,
    )
    _draw_wrapped(
        c,
        "If jet ski bookings are closed (winter/conditions), guests can still scan the QR to enquire and join early access.",
        col1_x + 10,
        panel_y + 22,
        col_w - 20,
        font_name="Helvetica",
        font_size=8,
        leading=10,
        color=slate_600,
    )

    y2 = draw_panel(col2_x, "Safety & Compliance (factual)")
    _draw_bullets(
        c,
        [
            "Structured customer briefing process before sessions.",
            "Ride onboarding steps and operating rules explained.",
            "Swim competency requirement (where applicable).",
            "Operator requirements / age rules communicated pre-ride.",
            "Safety equipment used (life jackets mandatory; additional gear as required).",
            "Weather and sea-condition stop/reschedule rules.",
            "Operating procedures designed around commercial safety requirements.",
        ],
        col2_x + 10,
        y2,
        col_w - 20,
        font_size=8,
        bullet_color=HexColor("#10b981"),
    )
    c.setFont("Helvetica", 8)
    c.setFillColor(slate_600)
    _draw_wrapped(
        c,
        f"Full detail: {safe_site.replace('https://', '').replace('http://', '')}/safety",
        col2_x + 10,
        panel_y + 22,
        col_w - 20,
        font_name="Helvetica",
        font_size=8,
        leading=10,
        color=slate_600,
    )

    y3 = draw_panel(col3_x, "Booking process (partner)")
    c.setFillColor(slate_700)
    c.setFont("Helvetica", 9)
    y3 = _draw_wrapped(
        c,
        "1) Guest scans QR and selects an experience (or submits an enquiry).\n"
        "2) We confirm availability, onboarding steps, and meeting point.\n"
        "3) Payment handled online; confirmations sent to guest.",
        col3_x + 10,
        y3,
        col_w - 20,
        font_name="Helvetica",
        font_size=9,
        leading=12,
        color=slate_700,
    )
    c.setFillColor(slate_600)
    c.setFont("Helvetica", 8)
    c.drawString(col3_x + 10, panel_y + 50, "Launch point:")
    c.setFillColor(slate_900)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(col3_x + 10, panel_y + 38, "Gordon's Bay Harbour, Western Cape")
    c.setFillColor(slate_600)
    c.setFont("Helvetica", 8)
    c.drawString(col3_x + 10, panel_y + 24, "Check-in details sent on confirmation; arrive 15 minutes early.")

    # Commission box
    box_y = panel_y - (26 * mm)
    box_h = 22 * mm
    c.setFillColor(HexColor("#e2e8f0"))
    c.roundRect(inner_left + 1.5, box_y - 1.5, (inner_right - inner_left), box_h, radius=12, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff"))
    c.setStrokeColor(border)
    c.roundRect(inner_left, box_y, (inner_right - inner_left), box_h, radius=12, stroke=1, fill=1)
    c.setFillColor(slate_900)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(inner_left + 12, box_y + box_h - 16, "Commission / referral (optional)")
    c.setFillColor(slate_700)
    c.setFont("Helvetica", 9)
    c.drawString(inner_left + 12, box_y + 10, f"{commission_percent}% commission on verified partner referrals (completed rides).")
    c.setFillColor(slate_600)
    c.setFont("Helvetica", 8)
    c.drawString(inner_left + 12, box_y + 2, "Settlement schedule confirmed per property.")
    if partner_code:
        c.setFillColor(slate_900)
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(inner_right - 12, box_y + 10, f"Partner code: {partner_code}")

    # Footer
    footer_y = inner_bottom + 2
    c.setStrokeColor(border)
    c.line(inner_left, footer_y + 16, inner_right, footer_y + 16)
    c.setFillColor(slate_600)
    c.setFont("Helvetica", 8)
    c.drawString(
        inner_left,
        footer_y + 6,
        "Jet Ski & More | Gordon's Bay Harbour (False Bay) | +27 (074) 658 8885 | info@jetskiandmore.com",
    )
    c.setFillColor(slate_500)
    c.drawRightString(inner_right, footer_y + 6, safe_site.replace("https://", "").replace("http://", ""))

    c.showPage()
    c.save()
    return buf.getvalue()
