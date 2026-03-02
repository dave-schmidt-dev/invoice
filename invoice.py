#!/usr/bin/env python3
"""Invoice generator CLI tool.

Generates professional PDF invoices and maintains a CSV log of all invoices.

Usage:
    invoice config   # Set up or update payee/payer info and data directory
    invoice new      # Create a new invoice interactively
    invoice new --date 2024-05-01   # Backdate an invoice
    invoice list     # Print all past invoices
"""

import csv
import json
from datetime import date
from pathlib import Path

import click
from fpdf import FPDF

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# The config file lives in the user's home directory so it is found
# regardless of which directory the command is run from.
CONFIG_FILE = Path.home() / ".invoice_config.json"

# Default directory for the CSV log and generated PDFs.  Users can override
# this during `invoice config` and the choice is persisted in CONFIG_FILE.
DEFAULT_DATA_DIR = Path.home() / ".invoice"

CSV_HEADERS = [
    "invoice_number",
    "date",
    "payee_name",
    "payer_name",
    "line_items",
    "total",
    "pdf_file",
]


# ---------------------------------------------------------------------------
# Path helpers (resolved at runtime so they honour the configured data_dir)
# ---------------------------------------------------------------------------


def get_data_dir() -> Path:
    """Return the configured data directory (expands ~ if present)."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        raw = cfg.get("data_dir", str(DEFAULT_DATA_DIR))
        return Path(raw).expanduser()
    return DEFAULT_DATA_DIR


def get_csv_file() -> Path:
    """Return the path to the CSV invoice log."""
    return get_data_dir() / "invoices.csv"


def get_invoices_dir() -> Path:
    """Return the directory where generated PDFs are stored."""
    return get_data_dir() / "invoices"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load and return the invoice configuration."""
    if not CONFIG_FILE.exists():
        raise click.ClickException(
            f"Config file not found at '{CONFIG_FILE}'. "
            "Run 'invoice config' first."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _run_config_setup(existing: dict | None = None) -> None:
    """Interactive wizard that writes (or updates) invoice_config.json."""

    def prompt(label, key_path, default=""):
        """Walk key_path into existing config to pre-fill the prompt."""
        pre = existing or {}
        for k in key_path:
            pre = pre.get(k, {}) if isinstance(pre, dict) else {}
        current = pre if isinstance(pre, str) else default
        return click.prompt(label, default=current)

    click.echo("\n=== Payee (you / your company) ===")
    payee_name = prompt("Name", ["payee", "name"])
    payee_address = prompt("Address", ["payee", "address"])
    payee_city = prompt("City", ["payee", "city"])
    payee_state = prompt("State", ["payee", "state"])
    payee_zip = prompt("ZIP", ["payee", "zip"])
    payee_email = prompt("Email", ["payee", "email"])
    payee_phone = prompt("Phone", ["payee", "phone"])

    click.echo("\n=== Payer (your client) ===")
    payer_name = prompt("Name", ["payer", "name"])
    payer_address = prompt("Address", ["payer", "address"])
    payer_city = prompt("City", ["payer", "city"])
    payer_state = prompt("State", ["payer", "state"])
    payer_zip = prompt("ZIP", ["payer", "zip"])
    payer_contact = prompt("Contact person", ["payer", "contact"])

    click.echo("\n=== Payment / Banking info (optional — press Enter to skip) ===")
    bank_name = prompt("Bank name", ["payment", "bank_name"])
    routing = prompt("Routing number", ["payment", "routing"])
    account = prompt("Account number", ["payment", "account"])

    click.echo("\n=== Data directory ===")
    click.echo(
        "Directory where the CSV log and generated PDFs will be stored.\n"
        "Use an absolute path or one starting with ~ for your home directory."
    )
    existing_data_dir = (existing or {}).get("data_dir", str(DEFAULT_DATA_DIR))
    raw_data_dir = click.prompt("Data directory", default=existing_data_dir)
    data_dir = str(Path(raw_data_dir).expanduser())

    config = {
        "data_dir": data_dir,
        "payee": {
            "name": payee_name,
            "address": payee_address,
            "city": payee_city,
            "state": payee_state,
            "zip": payee_zip,
            "email": payee_email,
            "phone": payee_phone,
        },
        "payer": {
            "name": payer_name,
            "address": payer_address,
            "city": payer_city,
            "state": payer_state,
            "zip": payer_zip,
            "contact": payer_contact,
        },
        "payment": {
            "bank_name": bank_name,
            "routing": routing,
            "account": account,
        },
    }

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    click.echo(f"\n✓  Config saved to: {CONFIG_FILE}")
    click.echo(f"✓  Data directory : {data_dir}")


# ---------------------------------------------------------------------------
# Invoice number helpers
# ---------------------------------------------------------------------------


def get_next_invoice_number() -> int:
    """Return the next invoice number (max existing + 1, or 1 if none)."""
    csv_file = get_csv_file()
    if not csv_file.exists():
        return 1
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        numbers = [int(row["invoice_number"]) for row in reader if row.get("invoice_number")]
    return (max(numbers) + 1) if numbers else 1


# ---------------------------------------------------------------------------
# Line-item collection
# ---------------------------------------------------------------------------


def get_line_items() -> list[dict]:
    """Interactively collect one or more line items from the user."""
    click.echo("\n=== Invoice Line Items ===")
    click.echo("Enter each project / task below. Leave description blank to finish.\n")
    items = []
    while True:
        description = click.prompt("Description (blank to finish)", default="")
        if not description.strip():
            if not items:
                click.echo("Please enter at least one line item.")
                continue
            break
        hours = click.prompt("  Hours", type=float)
        rate = click.prompt("  Rate ($/hr)", type=float)
        amount = round(hours * rate, 2)
        click.echo(f"  → Amount: ${amount:,.2f}")
        items.append({"description": description, "hours": hours, "rate": rate, "amount": amount})
    return items


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

_PAGE_W = 210  # A4 width in mm
_MARGIN = 15
_CONTENT_W = _PAGE_W - 2 * _MARGIN
_DESC_W = _CONTENT_W * 0.50
_HRS_W = _CONTENT_W * 0.15
_RATE_W = _CONTENT_W * 0.175
_AMT_W = _CONTENT_W * 0.175
_LABEL_W = _CONTENT_W - _AMT_W


def generate_pdf(
    invoice_number: int,
    invoice_date: str,
    config: dict,
    line_items: list[dict],
    output_path: str,
) -> float:
    """Render the invoice to a PDF file and return the total amount."""
    payee = config.get("payee", {})
    payer = config.get("payer", {})
    payment = config.get("payment", {})

    pdf = FPDF()
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
    pdf.add_page()

    # ---- Header ----
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, f"Invoice #: {invoice_number:04d}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Date: {invoice_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ---- Payee / Payer blocks ----
    pdf.set_font("Helvetica", "B", 10)
    col_w = _CONTENT_W / 2
    pdf.cell(col_w, 5, "FROM:", new_x="RIGHT", new_y="LAST")
    pdf.cell(col_w, 5, "BILL TO:", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)

    def _two_col(left, right=""):
        pdf.cell(col_w, 5, left, new_x="RIGHT", new_y="LAST")
        pdf.cell(col_w, 5, right, new_x="LMARGIN", new_y="NEXT")

    _two_col(payee.get("name", ""), payer.get("name", ""))
    _two_col(payee.get("address", ""), payer.get("address", ""))
    _two_col(
        f"{payee.get('city', '')}, {payee.get('state', '')} {payee.get('zip', '')}".strip(", "),
        f"{payer.get('city', '')}, {payer.get('state', '')} {payer.get('zip', '')}".strip(", "),
    )
    _two_col(payee.get("email", ""), payer.get("contact", ""))
    _two_col(payee.get("phone", ""))
    pdf.ln(8)

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
    for item in line_items:
        pdf.set_fill_color(245, 245, 245)
        pdf.cell(_DESC_W, 7, item["description"], fill=shade, new_x="RIGHT", new_y="LAST")
        pdf.cell(
            _HRS_W, 7, f"{item['hours']:.2f}", fill=shade, align="C", new_x="RIGHT", new_y="LAST"
        )
        pdf.cell(
            _RATE_W,
            7,
            f"${item['rate']:,.2f}",
            fill=shade,
            align="C",
            new_x="RIGHT",
            new_y="LAST",
        )
        pdf.cell(
            _AMT_W,
            7,
            f"${item['amount']:,.2f}",
            fill=shade,
            align="R",
            new_x="LMARGIN",
            new_y="NEXT",
        )
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
    if any(payment.get(k) for k in ("bank_name", "routing", "account")):
        pdf.ln(12)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Payment Information:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        if payment.get("bank_name"):
            pdf.cell(0, 5, f"Bank: {payment['bank_name']}", new_x="LMARGIN", new_y="NEXT")
        if payment.get("routing"):
            pdf.cell(0, 5, f"Routing #: {payment['routing']}", new_x="LMARGIN", new_y="NEXT")
        if payment.get("account"):
            pdf.cell(0, 5, f"Account #: {payment['account']}", new_x="LMARGIN", new_y="NEXT")

    pdf.output(output_path)
    return subtotal


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def save_to_csv(
    invoice_number: int,
    invoice_date: str,
    config: dict,
    line_items: list[dict],
    total: float,
    pdf_file: str,
) -> None:
    """Append the invoice summary to the CSV log."""
    csv_file = get_csv_file()
    file_exists = csv_file.exists()

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
                "invoice_number": f"{invoice_number:04d}",
                "date": invoice_date,
                "payee_name": config.get("payee", {}).get("name", ""),
                "payer_name": config.get("payer", {}).get("name", ""),
                "line_items": items_str,
                "total": f"{total:.2f}",
                "pdf_file": pdf_file,
            }
        )


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Invoice generator — create PDF invoices and track them in a CSV log."""


@cli.command("config")
def cmd_config():
    """Set up or update payee, payer, payment, and data directory configuration."""
    existing = None
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            existing = json.load(f)
        click.echo(f"Existing config found at '{CONFIG_FILE}'.")
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

    invoice_number = get_next_invoice_number()
    if invoice_date is None:
        invoice_date = date.today().isoformat()

    click.echo(f"\nCreating Invoice #{invoice_number:04d}  ({invoice_date})")

    line_items = get_line_items()

    invoices_dir = get_invoices_dir()
    invoices_dir.mkdir(parents=True, exist_ok=True)

    pdf_filename = f"invoice_{invoice_number:04d}_{invoice_date}.pdf"
    pdf_path = str(invoices_dir / pdf_filename)

    total = generate_pdf(invoice_number, invoice_date, config_data, line_items, pdf_path)
    save_to_csv(invoice_number, invoice_date, config_data, line_items, total, pdf_path)

    csv_file = get_csv_file()
    click.echo(f"\n✓  Invoice #{invoice_number:04d} saved to: {pdf_path}")
    click.echo(f"✓  Total due: ${total:,.2f}")
    click.echo(f"✓  CSV log updated: {csv_file}")


@cli.command("list")
def cmd_list():
    """List all previously generated invoices."""
    csv_file = get_csv_file()
    if not csv_file.exists():
        click.echo("No invoices found. Run 'invoice new' to create one.")
        return

    with open(csv_file, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        click.echo("No invoices found.")
        return

    click.echo(f"\n{'#':<8}{'Date':<14}{'Payer':<28}{'Total':>10}   PDF")
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


if __name__ == "__main__":
    cli()
