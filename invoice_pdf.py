"""PDF layout and rendering for invoice.py (moved verbatim)."""

import copy
import unicodedata
from decimal import Decimal
from pathlib import Path
import click
from fpdf import FPDF
from invoice_input import _to_decimal, _to_money_decimal


# Logo constraints (millimetres)
_LOGO_MAX_W = 50


_LOGO_MAX_H = 25


def _split_address_lines(address):
    """Split an address into printable lines; supports literal '\\n' input."""
    if not address:
        return []
    normalized = str(address).replace("\\n", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


# Column widths (mm). Page content width = 210 - 2*20 = 170 mm.
_DESC_W = 90


_HRS_W = 25


_RATE_W = 30


_AMT_W = 25


_LABEL_W = _DESC_W + _HRS_W + _RATE_W  # 145 mm


_FULL_W = _LABEL_W + _AMT_W            # 170 mm — full table width


# Typographic characters that appear routinely in text pasted from Notes/Mail
# but have no latin-1 codepoint. Map them to sensible ASCII before the harsher
# NFKD + latin-1 "replace" pass so common punctuation survives readably instead
# of collapsing to "?".
_TYPOGRAPHIC_MAP = {
    "—": "-",   # — em dash
    "–": "-",   # – en dash
    "“": '"',   # " left double quote
    "”": '"',   # " right double quote
    "‘": "'",   # ' left single quote
    "’": "'",   # ' right single quote
    "…": "...",  # … ellipsis
    "•": "*",   # • bullet
}


_TYPOGRAPHIC_TABLE = {ord(k): v for k, v in _TYPOGRAPHIC_MAP.items()}


def _latin1_safe(s):
    """Return `s` reduced to characters the latin-1 core PDF font can render.

    fpdf2's built-in Helvetica is latin-1 only; any other codepoint raises
    FPDFUnicodeEncodingException at draw time. This maps common typographic
    punctuation to ASCII, then NFKD-normalizes and encodes to latin-1 with
    "replace" so anything still unrepresentable becomes "?" rather than crashing.
    Non-str input is returned unchanged (callers may pass through numbers/None).
    """
    if not isinstance(s, str):
        return s
    cleaned = s.translate(_TYPOGRAPHIC_TABLE)
    cleaned = unicodedata.normalize("NFKD", cleaned)
    return cleaned.encode("latin-1", "replace").decode("latin-1")


class _InvoicePDF(FPDF):
    """FPDF subclass that renders a centered 'Page X of Y' footer on every page."""

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="C")


def _multi_cell_height(pdf, text, cell_width, line_h=5):
    """Estimate the rendered height (mm) of `text` if drawn via multi_cell at
    width `cell_width` and line height `line_h`. Mirrors fpdf2's word-boundary
    wrapping closely enough to predict whether a row will fit on the current
    page; used to force a page break BEFORE rendering so the row never gets
    split between pages."""
    text = (text or "").replace("\n", " ")
    if not text:
        return line_h
    safe_w = cell_width - 1  # small safety margin for cell padding
    words = text.split()
    if not words:
        return line_h
    lines = 1
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if pdf.get_string_width(candidate) <= safe_w:
            current = candidate
        else:
            lines += 1
            current = word
    return lines * line_h


def _payee_lines(payee):
    """Build the From block: name + address only. Email and phone live
    in the footer Contact block so the From and Bill To columns balance."""
    lines = [payee.get("name", "")]
    if payee.get("address"):
        lines.extend(_split_address_lines(payee["address"]))
    city = payee.get("city", "")
    state = payee.get("state", "")
    zip_ = payee.get("zip", "")
    if city or state or zip_:
        lines.append(f"{city}, {state} {zip_}".strip(", ").strip())
    return [l for l in lines if l]


def _payee_contact_lines(payee):
    """Build the footer Contact block: email + phone only."""
    lines = []
    if payee.get("email"):
        lines.append(payee["email"])
    if payee.get("phone"):
        lines.append(payee["phone"])
    return lines


def _client_lines(client):
    lines = [client.get("name", "")]
    if client.get("contact"):
        lines.append(client["contact"])
    if client.get("address"):
        lines.extend(_split_address_lines(client["address"]))
    city = client.get("city", "")
    state = client.get("state", "")
    zip_ = client.get("zip", "")
    if city or state or zip_:
        lines.append(f"{city}, {state} {zip_}".strip(", ").strip())
    return [l for l in lines if l]


def generate_pdf(invoice_number, invoice_date, config, line_items, output_path,
                 client=None, payment_terms="", payment_description=None):
    """Render the PDF invoice and return the subtotal."""
    pdf = _InvoicePDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    payee = config.get("payee", {})
    if client is None:
        clients = config.get("clients", [])
        client = clients[0] if clients else {}
    payment = config.get("payment", {})
    header_cfg = config.get("invoice_header", {})

    def _safe_identity(value, field_name):
        """Sanitize an IDENTITY / ADDRESS / PAYMENT string and WARN (stderr) if
        it was actually altered. Unlike free-text, silently corrupting a legal
        name or payment reference is unacceptable — so name each changed field.
        No-op (and no warning) for already-latin-1 values."""
        cleaned = _latin1_safe(value)
        if cleaned != value:
            click.echo(
                f"Warning: non-latin-1 character(s) in {field_name} were "
                f"transliterated for the PDF: {value!r} -> {cleaned!r}",
                err=True,
            )
        return cleaned

    def _safe_identity_lines(lines, field_name):
        """Apply `_safe_identity` to each line of an identity/address block."""
        return [_safe_identity(line, f"{field_name} line {i + 1}")
                for i, line in enumerate(lines)]

    # ---- Header ----
    logo_path = header_cfg.get("logo_path", "")
    title_text = _safe_identity(header_cfg.get("title") or "INVOICE", "invoice title")

    # Logo on left, title on right
    if logo_path and Path(logo_path).exists():
        # Render logo constrained to _LOGO_MAX_W × _LOGO_MAX_H mm, preserving aspect ratio.
        logo_y = pdf.get_y()
        # Position logo at absolute left (x=10mm) for true left alignment
        pdf.image(logo_path, x=10, w=_LOGO_MAX_W, h=_LOGO_MAX_H, keep_aspect_ratio=True)
        # Position title on the right side of the page, aligned with logo top
        pdf.set_font("Helvetica", "B", 28)
        pdf.set_xy(pdf.w - pdf.r_margin - 80, logo_y)
        pdf.cell(80, 14, title_text, align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "B", 28)
        pdf.cell(0, 14, title_text, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, f"Invoice #: {invoice_number}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Date: {invoice_date}", align="R", new_x="LMARGIN", new_y="NEXT")
    if payment_terms:
        # FREE-TEXT terms string (e.g. "Net 30 - due on receipt"); transliterate silently.
        pdf.cell(0, 5, f"Payment Terms: {_latin1_safe(payment_terms)}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(27)  # Tripled again from 18 to 27 for maximum white space after header

    # ---- From / Bill To ----
    col_w = 85

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_w, 5, "From:", new_x="RIGHT", new_y="LAST")
    pdf.set_x(pdf.get_x() + 10)
    pdf.cell(col_w, 5, "Bill To:", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 10)
    # IDENTITY / ADDRESS: payee (From) and client (Bill To) blocks. Transliterate
    # but WARN per changed line — a corrupted legal name must never be silent.
    payee_ls = _safe_identity_lines(_payee_lines(payee), "payee (From)")
    client_ls = _safe_identity_lines(_client_lines(client), "client (Bill To)")

    for i in range(max(len(payee_ls), len(client_ls))):
        left = payee_ls[i] if i < len(payee_ls) else ""
        right = client_ls[i] if i < len(client_ls) else ""
        pdf.cell(col_w, 4, left, new_x="RIGHT", new_y="LAST")
        pdf.set_x(pdf.get_x() + 10)
        pdf.cell(col_w, 4, right, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)

    # ---- Table header ----
    pdf.set_fill_color(40, 40, 40)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)

    pdf.cell(_DESC_W, 8, "Description", fill=True, new_x="RIGHT", new_y="LAST")
    pdf.cell(_HRS_W, 8, "Hours", fill=True, align="C", new_x="RIGHT", new_y="LAST")
    pdf.cell(_RATE_W, 8, "Rate ($/hr)", fill=True, align="C", new_x="RIGHT", new_y="LAST")
    pdf.cell(_AMT_W, 8, "Amount", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    # ---- Line items ----
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)

    subtotal = Decimal("0.00")
    shade = False
    for i, item in enumerate(line_items):
        # FREE-TEXT: line-item descriptions are pasted from Notes/Mail and are
        # the actual crash trigger. Transliterate SILENTLY — mangling an em dash
        # in a description is harmless, and warning on every paste would be noise.
        description = _latin1_safe(str(item["description"]).replace("\n", " "))
        hours = _to_decimal(item["hours"], "hours")
        rate = _to_money_decimal(item["rate"], "rate")
        amount = _to_money_decimal(item["amount"], "amount")

        # Predict the description's cell width and the row's total height so we
        # can force a page break BEFORE drawing anything — this prevents
        # multi_cell from auto-paginating in the middle of a row, which leaves
        # the numeric columns stranded at invalid coordinates on the next page.
        if hours == 0 and rate == 0 and amount == 0:
            desc_w = _FULL_W
        elif hours == 0 and rate == 0:
            desc_w = _LABEL_W
        else:
            desc_w = _DESC_W
        estimated_h = _multi_cell_height(pdf, description, desc_w)
        sep_h = 2 if i > 0 else 0
        needs_break = (pdf.get_y() + sep_h + estimated_h) > (pdf.h - pdf.b_margin)

        if needs_break:
            # Row wouldn't fit; start a fresh page. Skip the separator —
            # it would orphan at the bottom of the previous page or appear
            # awkwardly at the top of the new one.
            pdf.add_page()
        elif i > 0:
            # Standard inter-row separator.
            pdf.ln(1)
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.2)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(1)

        pdf.set_fill_color(245, 245, 245)

        if hours == 0 and rate == 0 and amount == 0:
            # Purely descriptive bullet: span the full table width so long lines don't wrap.
            pdf.multi_cell(_FULL_W, 5, description, fill=shade, new_x="LMARGIN", new_y="NEXT")
        elif hours == 0 and rate == 0:
            # Flat-fee / expense row: span description across DESC+HRS+RATE, amount on the right.
            row_y = pdf.get_y()
            pdf.multi_cell(_LABEL_W, 5, description, fill=shade, new_x="LMARGIN", new_y="NEXT")
            row_h = pdf.get_y() - row_y
            pdf.set_xy(pdf.l_margin + _LABEL_W, row_y)
            pdf.cell(_AMT_W, row_h, f"${amount:,.2f}", fill=shade, align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_y(row_y + row_h)
        else:
            # Standard 4-column row (hours × rate).
            row_y = pdf.get_y()
            pdf.multi_cell(_DESC_W, 5, description, fill=shade, new_x="LMARGIN", new_y="NEXT")
            row_h = pdf.get_y() - row_y
            pdf.set_xy(pdf.l_margin + _DESC_W, row_y)
            pdf.cell(_HRS_W, row_h, f"{hours:.2f}", fill=shade, align="C", new_x="RIGHT", new_y="LAST")
            pdf.cell(_RATE_W, row_h, f"${rate:,.2f}", fill=shade, align="C", new_x="RIGHT", new_y="LAST")
            pdf.cell(_AMT_W, row_h, f"${amount:,.2f}", fill=shade, align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_y(row_y + row_h)

        subtotal += amount
        shade = not shade

    subtotal = _to_money_decimal(subtotal, "subtotal")

    # ---- Divider ----
    pdf.ln(2)
    pdf.set_draw_color(40, 40, 40)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)

    # ---- Total ----
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(_LABEL_W, 8, "TOTAL DUE:", align="R", new_x="RIGHT", new_y="LAST")
    pdf.cell(_AMT_W, 8, f"${subtotal:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    # ---- Payment info ----
    # Use custom payment description if provided, otherwise fall back to config
    payment_info = copy.deepcopy(payment)
    if payment_description is not None:
        payment_info["description"] = payment_description
    
    # PAYMENT: transliterate but WARN per field — a corrupted routing/account
    # reference silently is exactly the failure mode this guards against.
    payment_lines = []
    if payment_info.get("description"):
        payment_lines.append(_safe_identity(payment_info["description"], "payment description"))
    if payment_info.get("bank_name"):
        payment_lines.append(f"Bank: {_safe_identity(payment_info['bank_name'], 'payment bank_name')}")
    if payment_info.get("routing"):
        payment_lines.append(f"Routing #: {_safe_identity(payment_info['routing'], 'payment routing')}")
    if payment_info.get("account"):
        payment_lines.append(f"Account #: {_safe_identity(payment_info['account'], 'payment account')}")

    # CONTACT (identity): payee email/phone shown in the footer.
    contact_lines = _safe_identity_lines(_payee_contact_lines(payee), "payee contact")

    if payment_lines or contact_lines:
        pdf.ln(12)
        col_w = 85

        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(col_w, 5, "Payment Information:" if payment_lines else "",
                 new_x="RIGHT", new_y="LAST")
        pdf.set_x(pdf.get_x() + 10)
        pdf.cell(col_w, 5, "Contact:" if contact_lines else "",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        pdf.set_font("Helvetica", "", 10)
        for i in range(max(len(payment_lines), len(contact_lines))):
            left = payment_lines[i] if i < len(payment_lines) else ""
            right = contact_lines[i] if i < len(contact_lines) else ""
            pdf.cell(col_w, 4, left, new_x="RIGHT", new_y="LAST")
            pdf.set_x(pdf.get_x() + 10)
            pdf.cell(col_w, 4, right, new_x="LMARGIN", new_y="NEXT")

    pdf.output(output_path)
    return subtotal
