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
from datetime import date
from pathlib import Path

import click
from fpdf import FPDF

CONFIG_FILE = Path.home() / ".invoice_config.json"
# Defaults used when no config exists yet; actual paths live inside the config.
_DEFAULT_CSV = Path.home() / "invoices" / "invoices.csv"
_DEFAULT_INVOICES_DIR = Path.home() / "invoices"

CSV_HEADERS = [
    "invoice_number",
    "date",
    "payee_name",
    "payer_name",
    "line_items",
    "total",
    "pdf_file",
]

DEFAULT_CONFIG = {
    "payee": {
        "name": "",
        "address": "",
        "city": "",
        "state": "",
        "zip": "",
        "email": "",
        "phone": "",
    },
    "payer": {
        "name": "",
        "address": "",
        "city": "",
        "state": "",
        "zip": "",
        "contact": "",
    },
    "payment": {
        "bank_name": "",
        "routing": "",
        "account": "",
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


def _run_config_setup(existing=None):
    """Interactive config wizard. Merges into *existing* if provided."""
    config = copy.deepcopy(existing or DEFAULT_CONFIG)

    click.echo("\n=== Payee Information (You / Your Company) ===")
    config["payee"]["name"] = click.prompt(
        "Your name or company", default=config["payee"]["name"] or ""
    )
    config["payee"]["address"] = click.prompt(
        "Street address", default=config["payee"]["address"] or ""
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

    click.echo("\n=== Payer Information (Your Client) ===")
    config["payer"]["name"] = click.prompt(
        "Client name or company", default=config["payer"]["name"] or ""
    )
    config["payer"]["contact"] = click.prompt(
        "Contact name", default=config["payer"]["contact"] or ""
    )
    config["payer"]["address"] = click.prompt(
        "Client street address", default=config["payer"]["address"] or ""
    )
    config["payer"]["city"] = click.prompt(
        "Client city", default=config["payer"]["city"] or ""
    )
    config["payer"]["state"] = click.prompt(
        "Client state", default=config["payer"]["state"] or ""
    )
    config["payer"]["zip"] = click.prompt(
        "Client ZIP code", default=config["payer"]["zip"] or ""
    )

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
    """Return the next invoice number (last known + 1, starting at 1)."""
    if not Path(csv_file).exists():
        return 1

    last_num = 0
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                num = int(row["invoice_number"])
                if num > last_num:
                    last_num = num
            except (ValueError, KeyError):
                pass

    return last_num + 1


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
        lines.append(payee["address"])
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


def _payer_lines(payer):
    lines = [payer.get("name", "")]
    if payer.get("contact"):
        lines.append(payer["contact"])
    if payer.get("address"):
        lines.append(payer["address"])
    city = payer.get("city", "")
    state = payer.get("state", "")
    zip_ = payer.get("zip", "")
    if city or state or zip_:
        lines.append(f"{city}, {state} {zip_}".strip(", ").strip())
    return [l for l in lines if l]


def generate_pdf(invoice_number, invoice_date, config, line_items, output_path):
    """Render the PDF invoice and return the subtotal."""
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    payee = config.get("payee", {})
    payer = config.get("payer", {})
    payment = config.get("payment", {})

    # ---- Header ----
    pdf.set_font("Helvetica", "B", 28)
    pdf.cell(0, 14, "INVOICE", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, f"Invoice #: {invoice_number:04d}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Date: {invoice_date}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ---- FROM / BILL TO ----
    col_w = 85

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_w, 6, "FROM:", new_x="RIGHT", new_y="LAST")
    pdf.set_x(pdf.get_x() + 10)
    pdf.cell(col_w, 6, "BILL TO:", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    payee_ls = _payee_lines(payee)
    payer_ls = _payer_lines(payer)

    for i in range(max(len(payee_ls), len(payer_ls))):
        left = payee_ls[i] if i < len(payee_ls) else ""
        right = payer_ls[i] if i < len(payer_ls) else ""
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


def save_to_csv(invoice_number, invoice_date, config, line_items, total, pdf_file):
    """Append the invoice summary to the CSV log.

    Returns the path of the CSV file that was written.
    """
    csv_file = config.get("storage", {}).get("csv_file") or str(_DEFAULT_CSV)
    # Ensure parent directory exists.
    Path(csv_file).parent.mkdir(parents=True, exist_ok=True)
    file_exists = Path(csv_file).exists()

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

    click.echo(f"\nCreating Invoice #{invoice_number:04d}  ({invoice_date})")

    line_items = get_line_items()

    Path(invoices_dir).mkdir(parents=True, exist_ok=True)
    pdf_filename = f"invoice_{invoice_number:04d}_{invoice_date}.pdf"
    pdf_path = str(Path(invoices_dir) / pdf_filename)

    total = generate_pdf(invoice_number, invoice_date, config_data, line_items, pdf_path)
    csv_used = save_to_csv(invoice_number, invoice_date, config_data, line_items, total, pdf_path)

    click.echo(f"\n✓  Invoice #{invoice_number:04d} saved to: {pdf_path}")
    click.echo(f"✓  Total due: ${total:,.2f}")
    click.echo(f"✓  CSV log updated: {csv_used}")


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


if __name__ == "__main__":
    cli()
