"""CSV ledger, file locking, atomic writes and invoice-number helpers for invoice.py (moved verbatim)."""

import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import click


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


def _read_csv_with_headers(path):
    """Read a CSV file, returning (rows, fieldnames) using the file's actual headers."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
    except UnicodeDecodeError as exc:
        raise click.ClickException(
            f"Ledger file '{path}' is not valid UTF-8 text: {exc}"
        ) from exc
    return rows, fieldnames


def _csv_field_key(fieldnames, canonical_name):
    """Find the key in fieldnames that matches a canonical CSV_HEADERS name.

    Handles legacy CSV files whose headers differ from CSV_HEADERS
    (e.g. 'Invoice Number' vs 'invoice_number').
    """
    if canonical_name in fieldnames:
        return canonical_name
    # Normalise: lowercase, strip, collapse spaces/underscores
    def _norm(s):
        return s.lower().strip().replace("_", " ").replace("-", " ")
    target = _norm(canonical_name)
    for fn in fieldnames:
        if _norm(fn) == target:
            return fn
    return None


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


def get_next_invoice_number(csv_file):
    """Return the next invoice number in format YYYY-#### (last known + 1, starting at 1)."""
    current_year = date.today().year
    csv_path = Path(csv_file)
    
    if not csv_path.exists():
        return f"{current_year}-0001"

    last_num = 0
    with _file_lock(csv_path):
        rows, file_headers = _read_csv_with_headers(csv_path)
        inv_key = _csv_field_key(file_headers, "invoice_number") or "invoice_number"
        for row in rows:
            try:
                # Extract number from YYYY-#### format
                invoice_str = row.get(inv_key) or ""
                if "-" in invoice_str:
                    year_part, num_part = invoice_str.split("-", 1)
                    if year_part == str(current_year):
                        num = int(num_part)
                        if num > last_num:
                            last_num = num
            except (ValueError, KeyError, TypeError):
                pass

    return f"{current_year}-{last_num + 1:04d}"


def _invoice_number_exists(csv_file, invoice_number):
    """Return True if invoice_number is already recorded in the CSV ledger.

    The CSV ledger is the authoritative store, so this is checked BEFORE any
    PDF is rendered — a duplicate number must never overwrite an existing
    invoice's PDF (INV-5). `save_to_csv` keeps its own dup guard as
    defense-in-depth.
    """
    normalized_number = _validate_invoice_number(invoice_number)
    csv_path = Path(csv_file)
    if not csv_path.exists():
        return False

    with _file_lock(csv_path):
        rows, file_headers = _read_csv_with_headers(csv_path)
        inv_key = _csv_field_key(file_headers, "invoice_number") or "invoice_number"
        for row in rows:
            if str(row.get(inv_key) or "").strip() == normalized_number:
                return True
    return False


def zd_db_tracks_invoice(db_path, invoice_number):
    """Return True if the zd DB at db_path already holds invoice_number.

    The zd SQLite DB is authoritative for zd-generated invoices (INV-2), so
    invoice.py must not change their status behind its back. The DB is opened
    read-only through a URI: no backup, migration, WAL change or file creation
    can happen here. A missing DB file, or a DB with no `invoices` table yet,
    means "not tracked". Any other SQLite failure propagates so the caller can
    fail closed rather than diverge the two records.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM invoices WHERE invoice_number = ? LIMIT 1",
            (invoice_number,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise
    finally:
        conn.close()
    return row is not None
