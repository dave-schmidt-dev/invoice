#!/usr/bin/env python3
"""Invoice generator CLI tool.

Generates professional PDF invoices and maintains a CSV log of all invoices.

Usage:
    ./invoice-wrapper --ledger            # Preferred: uses the project virtualenv automatically
    ./invoice-wrapper --invoice 2026-0001 # Preferred: opens the PDF for a specific invoice
    python invoice.py config              # Use only from an activated project virtualenv
    python invoice.py new                 # Use only from an activated project virtualenv
    python invoice.py list                # Use only from an activated project virtualenv
"""

import copy
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import click
from fpdf import FPDF

CONFIG_FILE = Path.home() / ".invoice_config.json"
# Defaults used when no config exists yet; actual paths live inside the config.
_DEFAULT_LEDGER = Path.home() / "invoices" / "invoices.csv"
_DEFAULT_INVOICES_DIR = Path.home() / "invoices"

# Logo constraints (millimetres)
_LOGO_MAX_W = 50
_LOGO_MAX_H = 25
_VALID_LOGO_EXTS = {".png", ".jpg", ".jpeg"}

PAYMENT_TERMS_CHOICES = ["Net 15", "Net 30", "Upon Receipt", "Custom"]
MONEY_PRECISION = Decimal("0.01")
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
INVOICE_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

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
        "ledger_file": str(_DEFAULT_LEDGER),
        "invoices_dir": str(_DEFAULT_INVOICES_DIR),
    },
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _to_decimal(value, field_name):
    """Convert a user-supplied numeric value into Decimal."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise click.ClickException(f"Invalid numeric value for {field_name}: {value!r}") from exc


def _to_money_decimal(value, field_name="amount"):
    """Convert a value to a currency Decimal rounded to cents."""
    return _to_decimal(value, field_name).quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def _prompt_decimal(prompt_text, field_name, minimum):
    """Prompt until a valid Decimal >= minimum is provided."""
    while True:
        raw_value = click.prompt(prompt_text, type=str).strip()
        try:
            value = _to_decimal(raw_value, field_name)
        except click.ClickException:
            click.echo(f"Invalid {field_name.lower()}. Enter a numeric value.")
            continue
        if value < minimum:
            click.echo(f"{field_name} must be at least {minimum}.")
            continue
        return value


def _split_address_lines(address):
    """Split an address into printable lines; supports literal '\\n' input."""
    if not address:
        return []
    normalized = str(address).replace("\\n", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _sanitize_filename_component(value, fallback):
    """Sanitize filename components to prevent traversal and invalid names."""
    cleaned = SAFE_FILENAME_RE.sub("_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = fallback
    return cleaned


def _validate_invoice_number(value):
    """Validate invoice number format for portability and safety."""
    candidate = str(value or "").strip()
    if not candidate:
        raise click.ClickException("Invoice number cannot be blank.")
    if not INVOICE_NUMBER_RE.fullmatch(candidate):
        raise click.ClickException(
            "Invoice number may only include letters, numbers, '.', '_' or '-' and must start with a letter/number."
        )
    return candidate


def _csv_safe(value):
    """Prevent spreadsheet formula injection for CSV exports."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


@contextmanager
def _file_lock(lock_target):
    """Best-effort cross-process lock using a sidecar lock file."""
    lock_target = Path(lock_target)
    lock_hash = hashlib.sha256(str(lock_target).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"invoice-{lock_hash}.lock"
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _get_file_mode(path, default_mode):
    """Use existing file permissions when possible."""
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return default_mode


def _atomic_write_json(path, data, mode=0o600):
    """Atomically write JSON content to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=path.parent, encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        if hasattr(os, "chmod"):
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _atomic_write_csv(path, rows, fieldnames, default_mode=0o600):
    """Atomically rewrite a CSV file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    mode = _get_file_mode(path, default_mode)
    try:
        with tempfile.NamedTemporaryFile(
            "w", newline="", delete=False, dir=path.parent, encoding="utf-8"
        ) as tmp:
            writer = csv.DictWriter(tmp, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        if hasattr(os, "chmod"):
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _normalize_storage_config(storage):
    """Back-fill and normalize storage paths, preserving the legacy csv_file alias."""
    if storage is None:
        storage = {}
    ledger_value = storage.get("ledger_file") or storage.get("csv_file") or str(_DEFAULT_LEDGER)
    invoices_dir = storage.get("invoices_dir") or str(_DEFAULT_INVOICES_DIR)
    ledger_path = str(Path(ledger_value).expanduser())
    storage["ledger_file"] = ledger_path
    storage["csv_file"] = ledger_path
    storage["invoices_dir"] = str(Path(invoices_dir).expanduser())
    return storage


def _ledger_path_from_config(config):
    """Return the configured invoice ledger path."""
    storage = _normalize_storage_config(config.setdefault("storage", {}))
    return Path(storage["ledger_file"])


def _invoices_dir_from_config(config):
    """Return the configured invoice output directory."""
    storage = _normalize_storage_config(config.setdefault("storage", {}))
    return Path(storage["invoices_dir"])


def _open_path(path):
    """Open a file in the platform-default application."""
    target = Path(path).expanduser()
    if not target.exists():
        raise click.ClickException(f"Path not found: {target}")
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=True)
        elif sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(f"Could not open '{target}': {exc}") from exc
    return target


def _resolve_invoice_pdf_path(config, invoice_number):
    """Look up the exact PDF path for an invoice number from the configured ledger."""
    normalized_number = _validate_invoice_number(invoice_number)
    ledger_path = _ledger_path_from_config(config)
    if not ledger_path.exists():
        raise click.ClickException(f"Invoice ledger not found: {ledger_path}")

    with _file_lock(ledger_path):
        with open(ledger_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("invoice_number") or "").strip() != normalized_number:
                    continue
                pdf_value = str(row.get("pdf_file") or "").strip()
                if not pdf_value:
                    raise click.ClickException(
                        f"Invoice #{normalized_number} does not have a PDF path recorded in {ledger_path}."
                    )
                return Path(pdf_value).expanduser()

    raise click.ClickException(f"Invoice #{normalized_number} not found in ledger: {ledger_path}")


def _open_email_client(client_email, subject, body, pdf_path):
    """Open a compose window in the default mail client."""
    recipient = urllib.parse.quote(client_email, safe="@._+-")
    encoded_subject = urllib.parse.quote(subject)
    encoded_body = urllib.parse.quote(body)
    mailto_url = f"mailto:{recipient}?subject={encoded_subject}&body={encoded_body}"

    if sys.platform == "darwin":
        script = """
on run argv
    set recipientAddress to item 1 of argv
    set messageSubject to item 2 of argv
    set messageBody to item 3 of argv
    set attachmentPath to item 4 of argv

    tell application "Mail"
        set newMessage to make new outgoing message with properties {subject:messageSubject, content:messageBody}
        tell newMessage
            make new to recipient at end of to recipients with properties {address:recipientAddress}
            tell content
                make new attachment with properties {file name:(POSIX file attachmentPath as alias)}
            end tell
            activate
        end tell
    end tell
end run
""".strip()
        subprocess.run(
            ["osascript", "-e", script, client_email, subject, body, str(pdf_path)],
            check=True,
        )
        return "apple_mail"

    if sys.platform == "win32":
        os.startfile(mailto_url)  # type: ignore[attr-defined]
        return "mailto_windows"

    subprocess.run(["xdg-open", mailto_url], check=True)
    return "mailto"


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

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"Config file '{CONFIG_FILE}' is not valid JSON: {exc}"
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Could not read config file '{CONFIG_FILE}': {exc}") from exc

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
    cfg["storage"] = _normalize_storage_config(cfg.get("storage", {}))
    return cfg


def save_config(config):
    """Save config to ~/.invoice_config.json."""
    _atomic_write_json(CONFIG_FILE, config, mode=0o600)


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
    ledger_file = click.prompt(
        "Invoice ledger path",
        default=config["storage"].get("ledger_file") or config["storage"].get("csv_file") or str(_DEFAULT_LEDGER),
    )
    config["storage"]["ledger_file"] = ledger_file
    config["storage"]["invoices_dir"] = click.prompt(
        "PDF output directory",
        default=config["storage"].get("invoices_dir") or str(_DEFAULT_INVOICES_DIR),
    )
    config["storage"] = _normalize_storage_config(config["storage"])

    save_config(config)
    click.echo(f"\nConfig saved to '{CONFIG_FILE}'.")
    return config


# ---------------------------------------------------------------------------
# Invoice number
# ---------------------------------------------------------------------------


def get_next_invoice_number(csv_file):
    """Return the next invoice number in format YYYY-#### (last known + 1, starting at 1)."""
    current_year = date.today().year
    csv_path = Path(csv_file)
    
    if not csv_path.exists():
        return f"{current_year}-0001"

    last_num = 0
    with _file_lock(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
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

        hours = _prompt_decimal("  Hours", "Hours", Decimal("0"))
        rate = _prompt_decimal("  Rate ($/hr)", "Rate", Decimal("0"))
        amount = _to_money_decimal(hours * rate)

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
        lines.extend(_split_address_lines(payee["address"]))
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

    subtotal = Decimal("0.00")
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
        description = str(item["description"]).replace("\n", " ")
        hours = _to_decimal(item["hours"], "hours")
        rate = _to_money_decimal(item["rate"], "rate")
        amount = _to_money_decimal(item["amount"], "amount")
        pdf.multi_cell(_DESC_W, 5, description, fill=shade, new_x="LMARGIN", new_y="NEXT")  # Reduced from 6 to 5 for even tighter spacing
        row_h = pdf.get_y() - row_y
        # Render the numeric columns at the same starting Y, spanning the full row height.
        pdf.set_xy(pdf.l_margin + _DESC_W, row_y)
        pdf.cell(
            _HRS_W, row_h, f"{hours:.2f}", fill=shade, align="C", new_x="RIGHT", new_y="LAST"
        )
        pdf.cell(
            _RATE_W,
            row_h,
            f"${rate:,.2f}",
            fill=shade,
            align="C",
            new_x="RIGHT",
            new_y="LAST",
        )
        pdf.cell(
            _AMT_W,
            row_h,
            f"${amount:,.2f}",
            fill=shade,
            align="R",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        # Advance past the description if it was taller than the numeric cells.
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
    csv_file = str(_ledger_path_from_config(config))
    csv_path = Path(csv_file)
    # Ensure parent directory exists.
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if client is None:
        clients = config.get("clients", [])
        client = clients[0] if clients else {}

    items_str = "; ".join(
        f"{item['description']} ({item['hours']} hrs @ ${item['rate']:.2f}/hr)"
        for item in line_items
    )

    row_data = {
        "invoice_number": _validate_invoice_number(invoice_number),
        "date": invoice_date,
        "payee_name": _csv_safe(config.get("payee", {}).get("name", "")),
        "payer_name": _csv_safe(client.get("name", "")),
        "line_items": _csv_safe(items_str),
        "total": f"{_to_money_decimal(total, 'total'):.2f}",
        "pdf_file": _csv_safe(pdf_file),
        "status": "Draft",  # Default status for new invoices
    }

    with _file_lock(csv_path):
        existing_numbers = set()
        if csv_path.exists():
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_numbers = {str(row.get("invoice_number", "")) for row in reader}

        if row_data["invoice_number"] in existing_numbers:
            raise click.ClickException(
                f"Invoice number '{row_data['invoice_number']}' already exists in {csv_path}. "
                "Choose a different invoice number."
            )

        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            if f.tell() == 0:
                writer.writeheader()
            writer.writerow(row_data)
            f.flush()
            os.fsync(f.fileno())
    return csv_file


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--ledger", "open_ledger", is_flag=True, help="Open the configured invoice ledger file.")
@click.option("--invoice", "invoice_number", metavar="INVOICE_NUMBER", help="Open the PDF for a specific invoice number.")
@click.pass_context
def cli(ctx, open_ledger, invoice_number):
    """Invoice generator — create PDF invoices and track them in a CSV log."""
    selected_shortcuts = int(open_ledger) + int(bool(invoice_number))
    if selected_shortcuts > 1:
        raise click.UsageError("Use either --ledger or --invoice, not both.")

    if ctx.invoked_subcommand:
        if selected_shortcuts:
            raise click.UsageError("Shortcut flags cannot be combined with subcommands.")
        return

    if open_ledger:
        config_data = load_config()
        ledger_path = _ledger_path_from_config(config_data)
        opened_path = _open_path(ledger_path)
        click.echo(f"Opened invoice ledger: {opened_path}")
        return

    if invoice_number:
        config_data = load_config()
        pdf_path = _resolve_invoice_pdf_path(config_data, invoice_number)
        opened_path = _open_path(pdf_path)
        click.echo(f"Opened invoice PDF: {opened_path}")
        return

    click.echo(ctx.get_help())


@cli.command("config")
def cmd_config():
    """Set up or update payee, payer, and payment configuration."""
    existing = None
    if CONFIG_FILE.exists():
        existing = load_config()
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

    csv_file = str(_ledger_path_from_config(config_data))
    invoices_dir = str(_invoices_dir_from_config(config_data))

    default_invoice_number = get_next_invoice_number(csv_file)
    invoice_number = default_invoice_number
    if invoice_date is None:
        invoice_date = date.today().isoformat()

    # Allow user to customize invoice number and date
    click.echo(f"\n--- Invoice Setup ---")
    click.echo(f"Default: Invoice #{invoice_number} dated {invoice_date}")
    
    # Option to change invoice number
    custom_number = click.prompt(
        "Invoice number (press Enter to use default)",
        default=default_invoice_number,
        show_default=False,
    )
    invoice_number = _validate_invoice_number(custom_number or default_invoice_number)
    if invoice_number != default_invoice_number:
        click.echo(f"✓ Using custom invoice number: {invoice_number}")
    
    # Option to change invoice date
    custom_date = click.prompt(
        "Invoice date (YYYY-MM-DD, press Enter for today)",
        default=invoice_date,
        show_default=False,
    )
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
    client_name = _sanitize_filename_component(client.get("name", "Client"), "Client")
    safe_invoice_number = _sanitize_filename_component(invoice_number, "invoice")
    pdf_filename = f"{client_name}_Invoice_{safe_invoice_number}.pdf"
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
    click.echo(f"✓  Ledger updated: {csv_used}")
    
    # Offer to open email client with invoice attached
    if client.get("email"):
        if click.confirm("Open email client to send this invoice?"):
            try:
                # Create email subject and body
                subject = f"Invoice #{invoice_number} from {config_data['payee']['name']}"
                body = f"Dear {client.get('contact', 'Valued Client')},\n\nPlease find attached invoice #{invoice_number} for ${total:,.2f}.\n\nPayment is due {payment_terms}.\n\n{payment_description or 'Thank you for your business!'}"

                mode = _open_email_client(client["email"], subject, body, pdf_path)
                if mode == "apple_mail":
                    click.echo("✓ Apple Mail opened with invoice attached!")
                    click.echo("  - Email is ready to send")
                    click.echo("  - Review and click Send!")
                else:
                    click.echo("✓ Email client opened with invoice ready to send")
                    click.echo(f"  - Manually attach: {pdf_path}")
                
            except Exception as e:
                click.echo(f"⚠ Could not open email client: {e}")
                click.echo("  You can manually email the invoice from:")
                click.echo(f"  {pdf_path}")
    else:
        click.echo("💡 Tip: Add client email in config to enable quick email sending")


@cli.command("status")
@click.argument("invoice_number")
@click.argument("status", type=click.Choice(["Draft", "Sent", "Paid", "Overdue"], case_sensitive=False))
def cmd_status(invoice_number, status):
    """Update the status of an invoice."""
    config_data = load_config()
    csv_file = str(_ledger_path_from_config(config_data))
    
    if not Path(csv_file).exists():
        click.echo(f"No invoices found. Ledger file not found: {csv_file}")
        return
    
    invoice_number = _validate_invoice_number(invoice_number)

    with _file_lock(csv_file):
        # Read all rows
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # Find the invoice
        found = False
        for row in rows:
            if row["invoice_number"] == invoice_number:
                row["status"] = status.capitalize()
                found = True
                break

        if not found:
            click.echo(f"Invoice #{invoice_number} not found.")
            return

        # Write back atomically.
        _atomic_write_csv(Path(csv_file), rows, CSV_HEADERS)
    
    click.echo(f"✓ Invoice #{invoice_number} status updated to: {status.capitalize()}")


@cli.command("list")
@click.option("--status", default="all",
             type=click.Choice(["all", "Draft", "Sent", "Paid", "Overdue"], case_sensitive=False),
             help="Filter by invoice status")
def cmd_list(status):
    """List all previously generated invoices."""
    config_data = load_config()
    csv_file = str(_ledger_path_from_config(config_data))

    if not Path(csv_file).exists():
        click.echo("No invoices found. Run 'invoice.py new' to create one.")
        return

    with open(csv_file, newline="", encoding="utf-8") as f:
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

    display_rows = []
    for row in rows:
        try:
            total_value = _to_money_decimal(row.get("total") or 0, "total")
        except click.ClickException:
            total_value = Decimal("0.00")
        display_rows.append(
            {
                "invoice_number": str(row.get("invoice_number") or ""),
                "date": str(row.get("date") or ""),
                "payer_name": str(row.get("payer_name") or ""),
                "total": f"${total_value:,.2f}",
                "status": str(row.get("status") or "Draft"),
                "pdf": Path(row.get("pdf_file") or "").name,
            }
        )

    headers = {
        "invoice_number": "#",
        "date": "Date",
        "payer_name": "Payer",
        "total": "Total",
        "status": "Status",
        "pdf": "PDF",
    }

    widths = {
        key: max(len(headers[key]), *(len(item[key]) for item in display_rows))
        for key in headers
    }

    click.echo()
    click.echo(
        f"{headers['invoice_number']:<{widths['invoice_number']}}  "
        f"{headers['date']:<{widths['date']}}  "
        f"{headers['payer_name']:<{widths['payer_name']}}  "
        f"{headers['total']:>{widths['total']}}  "
        f"{headers['status']:<{widths['status']}}  "
        f"{headers['pdf']:<{widths['pdf']}}"
    )
    click.echo("-" * (sum(widths.values()) + 10))
    for row in display_rows:
        click.echo(
            f"{row['invoice_number']:<{widths['invoice_number']}}  "
            f"{row['date']:<{widths['date']}}  "
            f"{row['payer_name']:<{widths['payer_name']}}  "
            f"{row['total']:>{widths['total']}}  "
            f"{row['status']:<{widths['status']}}  "
            f"{row['pdf']:<{widths['pdf']}}"
        )
    click.echo()


if __name__ == "__main__":
    cli()
