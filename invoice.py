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
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
from fpdf import FPDF
from invoice_ledger import (  # noqa: E402,F401 - moved to invoice_ledger.py
    CSV_FORMULA_PREFIXES, SAFE_FILENAME_RE, INVOICE_NUMBER_RE, fcntl, CSV_HEADERS,
    _sanitize_filename_component, _validate_invoice_number, _csv_safe, _file_lock,
    _get_file_mode, _atomic_write_json, _read_csv_with_headers, _csv_field_key,
    _atomic_write_csv, get_next_invoice_number, _invoice_number_exists,
    zd_db_tracks_invoice,
)
from invoice_input import (  # noqa: E402,F401 - moved to invoice_input.py
    _VALID_LOGO_EXTS, PAYMENT_TERMS_CHOICES, MONEY_PRECISION, _DEFAULT_CLIENT,
    _to_decimal, _to_money_decimal, _prompt_decimal, _open_path, _open_email_client,
    _prompt_client_info, get_line_items,
)
from invoice_pdf import (  # noqa: E402,F401 - moved to invoice_pdf.py
    _LOGO_MAX_W, _LOGO_MAX_H, _split_address_lines, _DESC_W, _HRS_W, _RATE_W, _AMT_W,
    _LABEL_W, _FULL_W, _TYPOGRAPHIC_MAP, _TYPOGRAPHIC_TABLE, _latin1_safe, _InvoicePDF,
    _multi_cell_height, _payee_lines, _payee_contact_lines, _client_lines, generate_pdf,
)

LOG_FILE = os.environ.get("INVOICE_LOG_FILE", "/tmp/invoice.log")


def _setup_logging(debug: bool):
    """Configure the named "invoice" logger (never the root logger, so we
    don't capture click/fpdf2/third-party log noise or spam stderr).

    Idempotent: repeated calls (e.g. across CliRunner invocations in tests)
    never stack duplicate handlers — only the level is refreshed on repeat
    calls. Level is DEBUG when --debug is passed, WARNING otherwise, set on
    both the logger and the handler.

    INV-1 defense-in-depth: LOG_FILE lives in /tmp, a shared directory, so
    the file is best-effort chmod'd to owner-only (0600) after the handler
    creates it. Log CONTENT must stay PII-free regardless — see callers.
    """
    logger = logging.getLogger("invoice")
    logger.propagate = False
    level = logging.DEBUG if debug else logging.WARNING
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        handler.setLevel(level)
        logger.addHandler(handler)
        try:
            os.chmod(LOG_FILE, 0o600)
        except OSError:
            pass
    else:
        for handler in logger.handlers:
            handler.setLevel(level)
    logger.setLevel(level)
    return logger

# ---------------------------------------------------------------------------
# Backups — timestamped copies before any destructive write, keep last 20
# ---------------------------------------------------------------------------

_MAX_BACKUPS = 20
_backed_up_this_run: set[str] = set()


def _backup_file(path):
    """Create a timestamped backup of path if it exists. Once per path per run."""
    path = Path(path)
    key = str(path)
    if key in _backed_up_this_run or not path.exists():
        return
    _backed_up_this_run.add(key)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(f"{path.suffix}.{ts}.bak")
    shutil.copy2(path, backup)
    # Prune old backups, keep last _MAX_BACKUPS
    pattern = f"{path.name}.*.bak"
    backups = sorted(path.parent.glob(pattern))
    for old in backups[:-_MAX_BACKUPS]:
        old.unlink(missing_ok=True)


CONFIG_FILE = Path.home() / ".invoice_config.json"
# Defaults used when no config exists yet; actual paths live inside the config.
_DEFAULT_LEDGER = Path.home() / "invoices" / "invoices.csv"
_DEFAULT_INVOICES_DIR = Path.home() / "invoices"


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


def _resolve_invoice_pdf_path(config, invoice_number):
    """Look up the exact PDF path for an invoice number from the configured ledger."""
    normalized_number = _validate_invoice_number(invoice_number)
    ledger_path = _ledger_path_from_config(config)
    if not ledger_path.exists():
        raise click.ClickException(f"Invoice ledger not found: {ledger_path}")

    with _file_lock(ledger_path):
        rows, file_headers = _read_csv_with_headers(ledger_path)
        inv_key = _csv_field_key(file_headers, "invoice_number") or "invoice_number"
        pdf_key = _csv_field_key(file_headers, "pdf_file") or "pdf_file"
        for row in rows:
            if str(row.get(inv_key) or "").strip() != normalized_number:
                continue
            pdf_value = str(row.get(pdf_key) or "").strip()
            if not pdf_value:
                raise click.ClickException(
                    f"Invoice #{normalized_number} does not have a PDF path recorded in {ledger_path}."
                )
            return Path(pdf_value).expanduser()

    raise click.ClickException(f"Invoice #{normalized_number} not found in ledger: {ledger_path}")


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
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"Config file '{CONFIG_FILE}' is not valid JSON: {exc}"
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Could not read config file '{CONFIG_FILE}': {exc}") from exc

    if not isinstance(cfg, dict):
        raise click.ClickException(f"Config file '{CONFIG_FILE}' is not a JSON object")

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
    """Save config to ~/.invoice_config.json.

    Locked via `_file_lock` so this writer and zd.py's `_sync_client_to_config`
    (which locks the same CONFIG_FILE path via `inv_mod._file_lock`) never
    interleave a read-modify-write against each other.
    """
    with _file_lock(CONFIG_FILE):
        _backup_file(CONFIG_FILE)
        _atomic_write_json(CONFIG_FILE, config, mode=0o600)


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
    config.setdefault("payee", {})
    for key in DEFAULT_CONFIG["payee"]:
        config["payee"].setdefault(key, "")
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


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def save_to_csv(invoice_number, invoice_date, config, line_items, total, pdf_file, client=None, status="Draft"):
    """Append the invoice summary to the CSV log.

    `status` defaults to "Draft" so interactive `invoice.py new` keeps its
    existing behavior. Callers that generate-and-finalize the invoice in a
    single step (e.g. `zd invoice`) should pass status="Sent" so the CSV
    ledger and downstream stores agree on the invoice's actual state.

    Returns the path of the CSV file that was written.
    """
    csv_file = str(_ledger_path_from_config(config))
    csv_path = Path(csv_file)
    # Ensure parent directory exists.
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if client is None:
        clients = config.get("clients", [])
        client = clients[0] if clients else {}

    def _format_line_item(item):
        # A flat-rate line item (hours == 0 and rate == 0) carries its full
        # detail in the description, so omit the "(0 hrs @ $0.00/hr)" suffix.
        if item["hours"] == 0 and item["rate"] == 0:
            return f"{item['description']}"
        return f"{item['description']} ({item['hours']} hrs @ ${item['rate']:.2f}/hr)"

    items_str = "; ".join(_format_line_item(item) for item in line_items)

    row_data = {
        "invoice_number": _validate_invoice_number(invoice_number),
        "date": invoice_date,
        "payee_name": _csv_safe(config.get("payee", {}).get("name", "")),
        "payer_name": _csv_safe(client.get("name", "")),
        "line_items": _csv_safe(items_str),
        "total": f"{_to_money_decimal(total, 'total'):.2f}",
        "pdf_file": _csv_safe(pdf_file),
        "status": status,
    }

    _backup_file(csv_path)
    with _file_lock(csv_path):
        existing_rows = []
        file_headers = None
        if csv_path.exists():
            existing_rows, file_headers = _read_csv_with_headers(csv_path)
            inv_key = _csv_field_key(file_headers, "invoice_number") or "invoice_number"
            existing_numbers = {str(row.get(inv_key, "")) for row in existing_rows}
        else:
            existing_numbers = set()

        # Duplicate-invoice-number guard kept as defense-in-depth.
        if row_data["invoice_number"] in existing_numbers:
            raise click.ClickException(
                f"Invoice number '{row_data['invoice_number']}' already exists in {csv_path}. "
                "Choose a different invoice number."
            )

        # Use CSV_HEADERS for new files; keep the file's existing headers for
        # legacy ledgers whose columns differ from CSV_HEADERS.
        write_headers = file_headers if file_headers and set(file_headers) != set(CSV_HEADERS) else CSV_HEADERS
        if file_headers and set(file_headers) != set(CSV_HEADERS):
            # Map row_data keys to the file's actual header names.
            mapped_row = {}
            for key, value in row_data.items():
                actual_key = _csv_field_key(file_headers, key)
                if actual_key:
                    mapped_row[actual_key] = value
            row_data = mapped_row

        # Read existing rows, append the new row in memory, then rewrite the
        # whole file via an atomic os.replace so a crash mid-write can never
        # leave a torn/partial row in the ledger (INV-6).
        all_rows = existing_rows + [row_data]
        _atomic_write_csv(csv_path, all_rows, write_headers)
    return csv_file


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--ledger", "open_ledger", is_flag=True, help="Open the configured invoice ledger file.")
@click.option("--invoice", "invoice_number", metavar="INVOICE_NUMBER", help="Open the PDF for a specific invoice number.")
@click.option("--debug", is_flag=True, help="Write DEBUG-level logs to <LOG_FILE> (default: WARNING+).")
@click.pass_context
def cli(ctx, open_ledger, invoice_number, debug):
    """Invoice generator — create PDF invoices and track them in a CSV log."""
    _setup_logging(debug)
    logging.getLogger("invoice").debug("cli invoked: %s", ctx.invoked_subcommand)
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

    # The CSV ledger is authoritative. Validate uniqueness BEFORE rendering the
    # PDF so a duplicate number can never overwrite an existing invoice's PDF
    # (INV-5). Then render to a temp path and os.replace it into place, so a
    # crash after the final PDF is written but before the ledger append leaves
    # only an orphan PDF (no ledger row) — never a ledger row pointing at a
    # missing PDF (CR-4). save_to_csv keeps its own dup-check as defense-in-depth.
    if _invoice_number_exists(csv_file, invoice_number):
        raise click.ClickException(
            f"Invoice number '{invoice_number}' already exists in {csv_file}. "
            "Choose a different invoice number."
        )

    temp_pdf_path = f"{pdf_path}.tmp-{os.getpid()}"
    try:
        total = generate_pdf(
            invoice_number, invoice_date, config_data, line_items, temp_pdf_path,
            client=client, payment_terms=payment_terms, payment_description=payment_description,
        )
        os.replace(temp_pdf_path, pdf_path)
    except BaseException:
        # Clean up the temp PDF on any failure before it is placed atomically.
        logging.getLogger("invoice").warning(
            "PDF write failed before atomic replace; temp file cleanup attempted"
        )
        try:
            os.unlink(temp_pdf_path)
        except OSError:
            pass
        raise

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


def _zd_db_path():
    """Location of the zd SQLite DB (same default as zd.ZD_DB), resolved per call.

    Deliberately not imported from zd.py: zd loads invoice.py fresh, so
    importing zd back would create a cycle.
    """
    return Path.home() / ".zd.db"


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

    # The zd DB is authoritative for zd-tracked invoices (INV-2). Changing
    # only the CSV would leave `zd status` listing a "Paid" invoice as
    # outstanding, so refuse before anything is written. CSV-only (legacy)
    # invoices, and a machine with no zd DB, keep the plain CSV behavior.
    try:
        zd_tracked = zd_db_tracks_invoice(_zd_db_path(), invoice_number)
    except sqlite3.Error as exc:
        raise click.ClickException(
            f"Could not check the zd database ({type(exc).__name__}); "
            "refusing to change the status so the CSV cannot diverge from it."
        )
    if zd_tracked:
        if status.lower() == "paid":
            hint = f"Use `zd paid {invoice_number}` instead."
        else:
            hint = (
                "The zd database is authoritative for this invoice and zd only "
                f"supports `zd paid {invoice_number}`; no other status change "
                "is available through invoice.py."
            )
        raise click.ClickException(
            f"Invoice #{invoice_number} is tracked in the zd database, so "
            f"invoice.py will not change its status. {hint} Nothing was written."
        )

    with _file_lock(csv_file):
        rows, file_headers = _read_csv_with_headers(csv_file)
        inv_key = _csv_field_key(file_headers, "invoice_number") or "invoice_number"
        status_key = _csv_field_key(file_headers, "status")
        if status_key is None:
            # Legacy ledger predates the status column — add it for every row
            # so _atomic_write_csv (kept strict) doesn't choke on an unknown key.
            status_key = "status"
            file_headers = list(file_headers) + [status_key]

        # Find the invoice
        found = False
        for row in rows:
            if row.get(inv_key) == invoice_number:
                row[status_key] = status.capitalize()
                found = True
                break

        if not found:
            click.echo(f"Invoice #{invoice_number} not found.")
            return

        # Write back atomically using the file's actual headers.
        _atomic_write_csv(Path(csv_file), rows, file_headers)
    
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

    rows, file_headers = _read_csv_with_headers(csv_file)

    if not rows:
        click.echo("No invoices found.")
        return

    # Resolve actual header keys
    _fk = lambda name: _csv_field_key(file_headers, name) or name
    inv_key = _fk("invoice_number")
    date_key = _fk("date")
    payer_key = _fk("payer_name")
    total_key = _fk("total")
    status_key = _fk("status")
    pdf_key = _fk("pdf_file")

    # Filter by status if specified
    if status != "all":
        rows = [row for row in rows if row.get(status_key) == status.capitalize()]
        if not rows:
            click.echo(f"No invoices found with status: {status.capitalize()}")
            return

    display_rows = []
    for row in rows:
        try:
            total_value = _to_money_decimal(row.get(total_key) or 0, "total")
        except click.ClickException:
            total_value = Decimal("0.00")
        display_rows.append(
            {
                "invoice_number": str(row.get(inv_key) or ""),
                "date": str(row.get(date_key) or ""),
                "payer_name": str(row.get(payer_key) or ""),
                "total": f"${total_value:,.2f}",
                "status": str(row.get(status_key) or "Draft"),
                "pdf": Path(row.get(pdf_key) or "").name,
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
