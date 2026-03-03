#!/usr/bin/env python3
"""Invoice generator CLI tool.

Generates professional PDF invoices and maintains a CSV log of all invoices.

Usage:
    python invoice.py config   # Set up payee/payer information
    python invoice.py new      # Create a new invoice
    python invoice.py list     # List all past invoices
"""

import copy
import csv
import json
import sys
from datetime import date
from pathlib import Path

import click
from fpdf import FPDF

CONFIG_FILE = Path.home() / ".invoice_config.json"
# Defaults used when no config exists yet; actual paths live inside the config.
_DEFAULT_CSV = Path.home() / "invoices" / "invoices.csv"
_DEFAULT_INVOICES_DIR = Path.home() / "invoices"

# Logo constraints (millimetres)
_LOGO_MAX_W = 50
_LOGO_MAX_H = 25
_VALID_LOGO_EXTS = {".png", ".jpg", ".jpeg"}

PAYMENT_TERMS_CHOICES = ["Net 15", "Net 30", "Upon Receipt", "Custom"]

CSV_HEADERS = [
    "invoice_number",
    "date",
    "payee_name",
    "payer_name",
    "line_items",
    "total",
    "pdf_file",
    "status",
]

_DEFAULT_CLIENT = {
    "name": "",
    "address": "",
    "city": "",
    "state": "",
    "zip": "",
    "contact": "",
}

DEFAULT_CONFIG = {
    "invoice_header": {
        "title": "INVOICE",
        "logo_path": "",
    },
    "payee": {
        "name": "",
        "address": "",
        "city": "",
        "state": "",
        "zip": "",
        "email": "",
        "phone": "",
    },
    "clients": [copy.deepcopy(_DEFAULT_CLIENT)],
    "payment": {
        "bank_name": "",
        "routing": "",
        "account": "",
        "description": "",
    },
    "storage": {
        "csv_file": str(_DEFAULT_CSV),
        "invoices_dir": str(_DEFAULT_INVOICES_DIR),
    },
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config():
    """Load config from ~/.invoice_config.json, prompting for setup if needed."""
    if not CONFIG_FILE.exists():
        click.echo(f"Config file '{CONFIG_FILE}' not found.")
        if click.confirm("Would you like to set up your config now?"):
            return _run_config_setup()
        click.echo(
            "Tip: run 'invoice.py config' at any time to configure payee/payer info and storage paths."
        )
        return copy.deepcopy(DEFAULT_CONFIG)

    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

    # Migrate old single 'payer' key to the new 'clients' list.
    if "payer" in cfg and "clients" not in cfg:
        cfg["clients"] = [cfg.pop("payer")]
    cfg.setdefault("clients", [copy.deepcopy(_DEFAULT_CLIENT)])

    # Back-fill invoice_header section.
    cfg.setdefault("invoice_header", copy.deepcopy(DEFAULT_CONFIG["invoice_header"]))
    cfg["invoice_header"].setdefault("title", "INVOICE")
    cfg["invoice_header"].setdefault("logo_path", "")

    # Back-fill payment fields.
    cfg.setdefault("payment", {})
    cfg["payment"].setdefault("description", "")

    # Back-fill the storage section for configs created before this field existed.
    cfg.setdefault("storage", {})
    cfg["storage"].setdefault("csv_file", str(_DEFAULT_CSV))
    cfg["storage"].setdefault("invoices_dir", str(_DEFAULT_INVOICES_DIR))
    # Expand ~ in paths in case the user edited the config file manually.
    cfg["storage"]["csv_file"] = str(Path(cfg["storage"]["csv_file"]).expanduser())
    cfg["storage"]["invoices_dir"] = str(Path(cfg["storage"]["invoices_dir"]).expanduser())
    return cfg


def save_config(config):
    """Save config to ~/.invoice_config.json."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def _prompt_client_info(existing=None):
    """Interactively prompt for a single client's info. Returns a client dict."""
    c = copy.deepcopy(existing or _DEFAULT_CLIENT)
    c["name"] = click.prompt("Client name or company", default=c.get("name") or "")
    c["contact"] = click.prompt("Contact name", default=c.get("contact") or "")
    c["address"] = click.prompt(
        "Client street address (use \\n for separate lines, e.g., '123 Main St\\nPO Box 456')", 
        default=c.get("address") or ""
    )
    c["city"] = click.prompt("Client city", default=c.get("city") or "")
    c["state"] = click.prompt("Client state", default=c.get("state") or "")
    c["zip"] = click.prompt("Client ZIP code", default=c.get("zip") or "")
    return c


def _run_config_setup(existing=None):
    """Interactive config wizard. Merges into *existing* if provided."""
    config = copy.deepcopy(existing or DEFAULT_CONFIG)
    # Ensure all sections exist (handles migrated / partial configs).
    config.setdefault("invoice_header", copy.deepcopy(DEFAULT_CONFIG["invoice_header"]))
    config["invoice_header"].setdefault("title", "INVOICE")
    config["invoice_header"].setdefault("logo_path", "")
    if "payer" in config and "clients" not in config:
        config["clients"] = [config.pop("payer")]
    config.setdefault("clients", [copy.deepcopy(_DEFAULT_CLIENT)])
    config.setdefault("payment", {})
    config["payment"].setdefault("description", "")

    # ---- Invoice Header ----
    click.echo("\n=== Invoice Header ===")
    config["invoice_header"]["title"] = click.prompt(
        "Invoice title", default=config["invoice_header"].get("title") or "INVOICE"
    )
    while True:
        logo = click.prompt(
            "Logo image path (PNG/JPG, leave blank to skip)",
            default=config["invoice_header"].get("logo_path") or "",
        )
        if not logo:
            config["invoice_header"]["logo_path"] = ""
            break
        logo_path = Path(logo).expanduser()
        if not logo_path.exists():
            click.echo(f"  File not found: {logo_path}")
        elif logo_path.suffix.lower() not in _VALID_LOGO_EXTS:
            click.echo(f"  Unsupported format '{logo_path.suffix}'. Use PNG or JPG.")
        else:
            config["invoice_header"]["logo_path"] = str(logo_path)
            break

    click.echo("\n=== Payee Information (You / Your Company) ===")
    config["payee"]["name"] = click.prompt(
        "Your name or company", default=config["payee"]["name"] or ""
    )
    config["payee"]["address"] = click.prompt(
        "Street address (use \\n for separate lines, e.g., '123 Main St\\nPO Box 456')", 
        default=config["payee"]["address"] or ""
    )
    config["payee"]["city"] = click.prompt(
        "City", default=config["payee"]["city"] or ""
    )
    config["payee"]["state"] = click.prompt(
        "State", default=config["payee"]["state"] or ""
    )
    config["payee"]["zip"] = click.prompt(
        "ZIP code", default=config["payee"]["zip"] or ""
    )
    config["payee"]["email"] = click.prompt(
        "Email", default=config["payee"]["email"] or ""
    )
    config["payee"]["phone"] = click.prompt(
        "Phone", default=config["payee"]["phone"] or ""
    )

    # ---- Client Profiles ----
    click.echo("\n=== Client Profiles ===")
    clients = list(config.get("clients", [copy.deepcopy(_DEFAULT_CLIENT)]))
    while True:
        click.echo("\nCurrent clients:")
        if clients:
            for i, c in enumerate(clients):
                click.echo(f"  {i + 1}. {c.get('name') or '(unnamed)'}")
        else:
            click.echo("  (none)")
        click.echo("Options: [a] Add client  [e#] Edit (e.g. e1)  [d#] Delete (e.g. d1)  [done]")
        action = click.prompt("Action", default="done")
        action = action.strip().lower()
        if action == "done":
            if not clients:
                click.echo("At least one client profile is required.")
            else:
                break
        elif action == "a":
            click.echo("\n--- New Client ---")
            clients.append(_prompt_client_info())
        elif action.startswith("e") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(clients):
                click.echo(f"\n--- Edit Client {idx + 1} ---")
                clients[idx] = _prompt_client_info(clients[idx])
            else:
                click.echo("Invalid selection.")
        elif action.startswith("d") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(clients):
                removed = clients.pop(idx)
                click.echo(f"Removed client: {removed.get('name')}")
            else:
                click.echo("Invalid selection.")
        else:
            click.echo("Unknown action.")
    config["clients"] = clients

    click.echo("\n=== Payment / Banking Information ===")
    config["payment"]["bank_name"] = click.prompt(
        "Bank name", default=config["payment"]["bank_name"] or ""
    )
    config["payment"]["routing"] = click.prompt(
        "Routing number", default=config["payment"]["routing"] or ""
    )
    config["payment"]["account"] = click.prompt(
        "Account number", default=config["payment"]["account"] or ""
    )
    config["payment"]["description"] = click.prompt(
        "Payment description (e.g. 'Please pay via ACH or check')",
        default=config["payment"].get("description") or "",
    )

    click.echo("\n=== Storage Paths ===")
    config["storage"]["csv_file"] = click.prompt(
        "Invoice CSV log path",
        default=config["storage"].get("csv_file") or str(_DEFAULT_CSV),
    )
    config["storage"]["invoices_dir"] = click.prompt(
        "PDF output directory",
        default=config["storage"].get("invoices_dir") or str(_DEFAULT_INVOICES_DIR),
    )

    save_config(config)
    click.echo(f"\nConfig saved to '{CONFIG_FILE}'.")
    return config


# ---------------------------------------------------------------------------
# Invoice number
# ---------------------------------------------------------------------------


def get_next_invoice_number(csv_file):
    """Return the next invoice number in format YYYY-#### (last known + 1, starting at 1)."""
    current_year = date.today().year
    
    if not Path(csv_file).exists():
        return f"{current_year}-0001"

    last_num = 0
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Extract number from YYYY-#### format
                invoice_str = row["invoice_number"]
                if "-" in invoice_str:
                    year_part, num_part = invoice_str.split("-", 1)
                    if year_part == str(current_year):
                        num = int(num_part)
                        if num > last_num:
                            last_num = num
            except (ValueError, KeyError):
                pass

    return f"{current_year}-{last_num + 1:04d}"


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------


def get_line_items():
    """Interactively collect line items from the user."""
    line_items = []
    click.echo(
        "\n=== Invoice Line Items ===\n"
        "Enter each project / task below. Leave description blank to finish.\n"
    )

    while True:
        description = click.prompt(
            "Description (blank to finish)", default="", show_default=False
        )
        if not description:
            if not line_items:
                click.echo("At least one line item is required.")
                continue
            break

        hours = click.prompt("  Hours", type=float)
        rate = click.prompt("  Rate ($/hr)", type=float)
        amount = round(hours * rate, 2)

        line_items.append(
            {
                "description": description,
                "hours": hours,
                "rate": rate,
                "amount": amount,
            }
        )
        click.echo(f"  → Amount: ${amount:,.2f}\n")

    return line_items


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

# Column widths (mm). Page content width = 210 - 2*20 = 170 mm.
_DESC_W = 90
_HRS_W = 25
_RATE_W = 30
_AMT_W = 25
_LABEL_W = _DESC_W + _HRS_W + _RATE_W  # 145 mm


def _payee_lines(payee):
    lines = [payee.get("name", "")]
    if payee.get("address"):
        # Split address into two lines if it contains "\n"
        address_lines = payee["address"].split("\n")
        lines.extend(address_lines)
    city = payee.get("city", "")
    state = payee.get("state", "")
    zip_ = payee.get("zip", "")
    if city or state or zip_:
        lines.append(f"{city}, {state} {zip_}".strip(", ").strip())
    if payee.get("email"):
        lines.append(payee["email"])
    if payee.get("phone"):
        lines.append(payee["phone"])
    return [l for l in lines if l]


def _client_lines(client):
    lines = [client.get("name", "")]
    if client.get("contact"):
        lines.append(client["contact"])
    if client.get("address"):
        # Split address into two lines if it contains "\n"
        address_lines = client["address"].split("\n")
        lines.extend(address_lines)
    city = client.get("city", "")
    state = client.get("state", "")
    zip_ = client.get("zip", "")
    if city or state or zip_:
        lines.append(f"{city}, {state} {zip_}".strip(", ").strip())
    return [l for l in lines if l]


def generate_pdf(invoice_number, invoice_date, config, line_items, output_path,
                 client=None, payment_terms="", payment_description=None):
    """Render the PDF invoice and return the subtotal."""
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    payee = config.get("payee", {})
    if client is None:
        clients = config.get("clients", [])
        client = clients[0] if clients else {}
    payment = config.get("payment", {})
    header_cfg = config.get("invoice_header", {})

    # ---- Header ----
    logo_path = header_cfg.get("logo_path", "")
    title_text = header_cfg.get("title") or "INVOICE"

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
        pdf.cell(0, 5, f"Payment Terms: {payment_terms}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(27)  # Tripled again from 18 to 27 for maximum white space after header

    # ---- FROM / BILL TO ----
    col_w = 85

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_w, 6, "FROM:", new_x="RIGHT", new_y="LAST")
    pdf.set_x(pdf.get_x() + 10)
    pdf.cell(col_w, 6, "BILL TO:", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    payee_ls = _payee_lines(payee)
    client_ls = _client_lines(client)

    for i in range(max(len(payee_ls), len(client_ls))):
        left = payee_ls[i] if i < len(payee_ls) else ""
        right = client_ls[i] if i < len(client_ls) else ""
        pdf.cell(col_w, 6, left, new_x="RIGHT", new_y="LAST")
        pdf.set_x(pdf.get_x() + 10)
        pdf.cell(col_w, 6, right, new_x="LMARGIN", new_y="NEXT")

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

    subtotal = 0.0
    shade = False
    for i, item in enumerate(line_items):
        # Add separator line between projects (except before first item)
        if i > 0:
            pdf.ln(1)  # Reduced from 2 to 1 for tighter spacing between projects
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.2)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(1)  # Reduced from 2 to 1 for tighter spacing
        
        pdf.set_fill_color(245, 245, 245)
        row_y = pdf.get_y()
        # Use multi_cell so long descriptions wrap instead of overflowing.
        # Replace newlines in description with spaces for single spacing within projects
        description = item["description"].replace("\n", " ")
        pdf.multi_cell(_DESC_W, 5, description, fill=shade, new_x="LMARGIN", new_y="NEXT")  # Reduced from 6 to 5 for even tighter spacing
        row_h = pdf.get_y() - row_y
        # Render the numeric columns at the same starting Y, spanning the full row height.
        pdf.set_xy(pdf.l_margin + _DESC_W, row_y)
        pdf.cell(
            _HRS_W, row_h, f"{item['hours']:.2f}", fill=shade, align="C", new_x="RIGHT", new_y="LAST"
        )
        pdf.cell(
            _RATE_W,
            row_h,
            f"${item['rate']:,.2f}",
            fill=shade,
            align="C",
            new_x="RIGHT",
            new_y="LAST",
        )
        pdf.cell(
            _AMT_W,
            row_h,
            f"${item['amount']:,.2f}",
            fill=shade,
            align="R",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        # Advance past the description if it was taller than the numeric cells.
        pdf.set_y(row_y + row_h)
        subtotal += item["amount"]
        shade = not shade

    subtotal = round(subtotal, 2)

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
    
    if any(payment_info.get(k) for k in ("bank_name", "routing", "account", "description")):
        pdf.ln(12)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Payment Information:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        if payment_info.get("description"):
            pdf.cell(0, 5, payment_info["description"], new_x="LMARGIN", new_y="NEXT")
        if payment_info.get("bank_name"):
            pdf.cell(0, 5, f"Bank: {payment_info['bank_name']}", new_x="LMARGIN", new_y="NEXT")
        if payment_info.get("routing"):
            pdf.cell(0, 5, f"Routing #: {payment_info['routing']}", new_x="LMARGIN", new_y="NEXT")
        if payment_info.get("account"):
            pdf.cell(0, 5, f"Account #: {payment_info['account']}", new_x="LMARGIN", new_y="NEXT")

    pdf.output(output_path)
    return subtotal


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def save_to_csv(invoice_number, invoice_date, config, line_items, total, pdf_file, client=None):
    """Append the invoice summary to the CSV log.

    Returns the path of the CSV file that was written.
    """
    csv_file = config.get("storage", {}).get("csv_file") or str(_DEFAULT_CSV)
    # Ensure parent directory exists.
    Path(csv_file).parent.mkdir(parents=True, exist_ok=True)
    file_exists = Path(csv_file).exists()

    if client is None:
        clients = config.get("clients", [])
        client = clients[0] if clients else {}

    items_str = "; ".join(
        f"{item['description']} ({item['hours']} hrs @ ${item['rate']:.2f}/hr)"
        for item in line_items
    )

    with open(csv_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(
            {
                "invoice_number": str(invoice_number),
                "date": invoice_date,
                "payee_name": config.get("payee", {}).get("name", ""),
                "payer_name": client.get("name", ""),
                "line_items": items_str,
                "total": f"{total:.2f}",
                "pdf_file": pdf_file,
                "status": "Draft",  # Default status for new invoices
            }
        )
    return csv_file


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Invoice generator — create PDF invoices and track them in a CSV log."""


@cli.command("config")
def cmd_config():
    """Set up or update payee, payer, and payment configuration."""
    existing = None
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            existing = json.load(f)
        # Migrate old 'payer' key to 'clients' list.
        if "payer" in existing and "clients" not in existing:
            existing["clients"] = [existing.pop("payer")]
        existing.setdefault("clients", [copy.deepcopy(_DEFAULT_CLIENT)])
        existing.setdefault("invoice_header", copy.deepcopy(DEFAULT_CONFIG["invoice_header"]))
        existing["invoice_header"].setdefault("title", "INVOICE")
        existing["invoice_header"].setdefault("logo_path", "")
        existing.setdefault("payment", {})
        existing["payment"].setdefault("description", "")
        # Back-fill storage section for older configs.
        existing.setdefault("storage", {})
        existing["storage"].setdefault("csv_file", str(_DEFAULT_CSV))
        existing["storage"].setdefault("invoices_dir", str(_DEFAULT_INVOICES_DIR))
        # Expand ~ in case the user edited the file manually.
        existing["storage"]["csv_file"] = str(Path(existing["storage"]["csv_file"]).expanduser())
        existing["storage"]["invoices_dir"] = str(Path(existing["storage"]["invoices_dir"]).expanduser())
        click.echo(f"Existing config found in '{CONFIG_FILE}'.")
        if not click.confirm("Update it?"):
            return
    _run_config_setup(existing)


@cli.command("new")
@click.option(
    "--date",
    "invoice_date",
    default=None,
    help="Invoice date in YYYY-MM-DD format (defaults to today).",
)
def cmd_new(invoice_date):
    """Create a new invoice interactively."""
    config_data = load_config()

    csv_file = config_data.get("storage", {}).get("csv_file") or str(_DEFAULT_CSV)
    invoices_dir = config_data.get("storage", {}).get("invoices_dir") or str(_DEFAULT_INVOICES_DIR)

    invoice_number = get_next_invoice_number(csv_file)
    if invoice_date is None:
        invoice_date = date.today().isoformat()

    # Allow user to customize invoice number and date
    click.echo(f"\n--- Invoice Setup ---")
    click.echo(f"Default: Invoice #{invoice_number} dated {invoice_date}")
    
    # Option to change invoice number
    custom_number = click.prompt("Invoice number (press Enter to use default)", default=invoice_number, type=int, show_default=False)
    if custom_number != invoice_number:
        invoice_number = custom_number
        click.echo(f"✓ Using custom invoice number: {invoice_number}")
    
    # Option to change invoice date
    custom_date = click.prompt("Invoice date (YYYY-MM-DD, press Enter for today)", default=invoice_date, show_default=False)
    try:
        # Validate the date format
        date.fromisoformat(custom_date)
        invoice_date = custom_date
        if invoice_date != date.today().isoformat():
            click.echo(f"✓ Using custom date: {invoice_date}")
    except ValueError:
        click.echo("⚠ Invalid date format. Using today's date.")
        invoice_date = date.today().isoformat()

    # Option to change payment description (per-invoice basis)
    default_payment_desc = config_data.get("payment", {}).get("description", "")
    payment_description = click.prompt(
        "Payment description (press Enter to use default)", 
        default=default_payment_desc, 
        show_default=False
    )
    if payment_description and payment_description != default_payment_desc:
        click.echo(f"✓ Using custom payment description")
    elif not payment_description:
        payment_description = default_payment_desc

    click.echo(f"\n--- Creating Invoice #{invoice_number} dated {invoice_date} ---")

    # ---- Client selection ----
    clients = config_data.get("clients", [])
    if not clients:
        click.echo("No client profiles found. Please run 'invoice.py config' to add clients.")
        return
    if len(clients) == 1:
        client = clients[0]
        click.echo(f"Client: {client.get('name', '')}")
    else:
        click.echo("\nSelect a client:")
        for i, c in enumerate(clients):
            click.echo(f"  {i + 1}. {c.get('name', '(unnamed)')}")
        choice = click.prompt("Client number", type=click.IntRange(1, len(clients)))
        client = clients[choice - 1]

    # ---- Payment terms ----
    click.echo("\nPayment Terms:")
    for i, t in enumerate(PAYMENT_TERMS_CHOICES):
        click.echo(f"  {i + 1}. {t}")
    terms_idx = click.prompt(
        "Select payment terms",
        type=click.IntRange(1, len(PAYMENT_TERMS_CHOICES)),
        default=2,
    )
    selected = PAYMENT_TERMS_CHOICES[terms_idx - 1]
    if selected == "Custom":
        payment_terms = click.prompt("Enter custom payment terms")
    else:
        payment_terms = selected

    line_items = get_line_items()

    Path(invoices_dir).mkdir(parents=True, exist_ok=True)
    # Use ClientName_Invoice_InvoiceNumber.pdf format
    client_name = client.get("name", "Client").replace(" ", "_")
    pdf_filename = f"{client_name}_Invoice_{invoice_number}.pdf"
    pdf_path = str(Path(invoices_dir) / pdf_filename)

    total = generate_pdf(
        invoice_number, invoice_date, config_data, line_items, pdf_path,
        client=client, payment_terms=payment_terms, payment_description=payment_description,
    )
    csv_used = save_to_csv(
        invoice_number, invoice_date, config_data, line_items, total, pdf_path,
        client=client,
    )

    click.echo(f"\n✓  Invoice #{invoice_number} saved to: {pdf_path}")
    click.echo(f"✓  Total due: ${total:,.2f}")
    click.echo(f"✓  CSV log updated: {csv_used}")
    
    # Offer to open email client with invoice attached
    if client.get("email"):
        if click.confirm("Open email client to send this invoice?"):
            try:
                import subprocess
                import urllib.parse
                
                # Create email subject and body
                subject = f"Invoice #{invoice_number} from {config_data['payee']['name']}"
                body = f"Dear {client.get('contact', 'Valued Client')},\n\nPlease find attached invoice #{invoice_number} for ${total:,.2f}.\n\nPayment is due {payment_terms}.\n\n{payment_description or 'Thank you for your business!'}"
                
                # URL encode for mailto: link
                encoded_subject = urllib.parse.quote(subject)
                encoded_body = urllib.parse.quote(body)
                
                # Create mailto: link
                mailto_url = f"mailto:{client['email']}?subject={encoded_subject}&body={encoded_body}"
                
                # Open default mail client
                if sys.platform == 'darwin':  # macOS
                    # Use AppleScript to create email with attachment
                    script = f'''
                    tell application "Mail"
                        set newMessage to make new outgoing message with properties {{subject:"{subject}", content:"{body}"}}
                        tell newMessage
                            make new to recipient at end of to recipients with properties {{address:"{client['email']}"}}
                            tell content
                                make new attachment with properties {{file name:"{pdf_path}"}}
                            end tell
                            activate
                        end tell
                    end tell
                    '''
                    subprocess.run(['osascript', '-e', script])
                    click.echo("✓ Apple Mail opened with invoice attached!")
                    click.echo("  - Email is ready to send")
                    click.echo("  - Review and click Send!")
                    
                elif sys.platform == 'win32':  # Windows
                    subprocess.run(['start', mailto_url], shell=True)
                    click.echo("✓ Email client opened with invoice ready to send")
                    click.echo(f"  - Manually attach: {pdf_path}")
                    
                else:  # Linux
                    subprocess.run(['xdg-open', mailto_url])
                    click.echo("✓ Email client opened with invoice ready to send")
                    click.echo(f"  - Manually attach: {pdf_path}")
                
            except Exception as e:
                click.echo(f"⚠ Could not open email client: {e}")
                click.echo("  You can manually email the invoice from:")
                click.echo(f"  {pdf_path}")
    else:
        click.echo("💡 Tip: Add client email in config to enable quick email sending")


@cli.command("list")
def cmd_list():
    """List all previously generated invoices."""
    config_data = load_config()
    csv_file = config_data.get("storage", {}).get("csv_file") or str(_DEFAULT_CSV)

    if not Path(csv_file).exists():
        click.echo("No invoices found. Run 'invoice.py new' to create one.")
        return

    with open(csv_file, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        click.echo("No invoices found.")
        return

    # Header
    click.echo(
        f"\n{'#':<8}{'Date':<14}{'Payer':<28}{'Total':>10}   PDF"
    )
    click.echo("-" * 80)
    for row in rows:
        click.echo(
            f"{row['invoice_number']:<8}"
            f"{row['date']:<14}"
            f"{row['payer_name']:<28}"
            f"${row['total']:>9}   "
            f"{row['pdf_file']}"
        )
    click.echo()


@cli.command("status")
@click.argument("invoice_number")
@click.argument("status", type=click.Choice(["Draft", "Sent", "Paid", "Overdue"], case_sensitive=False))
def cmd_status(invoice_number, status):
    """Update the status of an invoice."""
    config_data = load_config()
    csv_file = config_data.get("storage", {}).get("csv_file") or str(_DEFAULT_CSV)
    
    if not Path(csv_file).exists():
        click.echo(f"No invoices found. CSV file not found: {csv_file}")
        return
    
    # Read all rows
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    # Find the invoice
    found = False
    for row in rows:
        if row['invoice_number'] == invoice_number:
            row['status'] = status.capitalize()
            found = True
            break
    
    if not found:
        click.echo(f"Invoice #{invoice_number} not found.")
        return
    
    # Write back to CSV
    with open(csv_file, 'w', newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    
    click.echo(f"✓ Invoice #{invoice_number} status updated to: {status.capitalize()}")


@cli.command("list")
@click.option("--status", default="all",
             type=click.Choice(["all", "Draft", "Sent", "Paid", "Overdue"], case_sensitive=False),
             help="Filter by invoice status")
def cmd_list(status):
    """List all previously generated invoices."""
    config_data = load_config()
    csv_file = config_data.get("storage", {}).get("csv_file") or str(_DEFAULT_CSV)

    if not Path(csv_file).exists():
        click.echo("No invoices found. Run 'invoice.py new' to create one.")
        return

    with open(csv_file, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        click.echo("No invoices found.")
        return

    # Filter by status if specified
    if status != "all":
        rows = [row for row in rows if row.get("status") == status.capitalize()]
        if not rows:
            click.echo(f"No invoices found with status: {status.capitalize()}")
            return

    # Header
    # Calculate width based on longest PDF path for proper alignment
    max_pdf_length = max(len(row['pdf_file']) for row in rows) if rows else 30
    header_width = 8 + 14 + 28 + 10 + 12 + max_pdf_length + 10
    
    click.echo(
        f"\n{'#':<8}{'Date':<14}{'Payer':<28}{'Total':>10}{'Status':<12}   PDF"
    )
    click.echo("-" * header_width)
    for row in rows:
        click.echo(
            f"{row['invoice_number']:<8}"
            f"{row['date']:<14}"
            f"{row['payer_name']:<28}"
            f"${row['total']:>9}   "
            f"{row.get('status', 'Draft'):<12}"
            f"{row['pdf_file']}"
        )
    click.echo()


if __name__ == "__main__":
    cli()
