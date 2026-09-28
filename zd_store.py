"""Backups, schema constants, money and calendar helpers for zd (moved verbatim from zd.py)."""

import shutil
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import click


_MAX_BACKUPS = 20


_backed_up_this_run: set[str] = set()


def _backup_file(path):
    """Create a timestamped backup of path if it exists. Once per path per run.

    Used for CSV/config backups (plain file copy). DB backups go through
    _backup_db instead, which uses SQLite's online-backup API so a live
    WAL-mode DB is never copied mid-write (see _backup_db)."""
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


def _backup_db(conn, db_path):
    """Snapshot an OPEN sqlite3 connection to a timestamped .bak file via the
    SQLite online-backup API, so a live WAL-mode DB is never copied mid-write
    (shutil.copy2 can grab a torn snapshot when committed frames sit in the
    -wal sidecar). Once per path per run, pruned to _MAX_BACKUPS like
    _backup_file. No-op if the source DB has no user tables yet (a fresh/
    empty DB has nothing worth snapshotting).
    """
    db_path = Path(db_path)
    key = str(db_path)
    if key in _backed_up_this_run or not db_path.exists():
        return
    table_count = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    if not table_count:
        return
    _backed_up_this_run.add(key)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_suffix(f"{db_path.suffix}.{ts}.bak")
    dest = sqlite3.connect(backup_path)
    try:
        with dest:
            conn.backup(dest)
    finally:
        dest.close()
    # Prune old backups, keep last _MAX_BACKUPS
    pattern = f"{db_path.name}.*.bak"
    backups = sorted(db_path.parent.glob(pattern))
    for old in backups[:-_MAX_BACKUPS]:
        old.unlink(missing_ok=True)


# Target schema version. Bump this and add matching guarded ALTERs in _migrate
# whenever init_db's CREATE TABLE statements gain a column that existing DBs
# won't have. A fresh init_db DB and a migrated older DB must converge.
_SCHEMA_VERSION = 1


# Columns that _migrate must ensure exist on already-populated DBs. Each is
# (table, column, "ALTER TABLE ... ADD COLUMN ..." SQL). These mirror the
# columns added to the CREATE TABLE statements in init_db.
_MIGRATIONS = (
    ("invoices", "paid_date", "ALTER TABLE invoices ADD COLUMN paid_date TEXT"),
    (
        "invoices",
        "billing_mode",
        "ALTER TABLE invoices ADD COLUMN billing_mode TEXT DEFAULT 'hourly'",
    ),
    ("sessions", "billed_rate", "ALTER TABLE sessions ADD COLUMN billed_rate REAL"),
)


def _column_exists(conn, table, column):
    """True if `column` is present on `table` (via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


MONEY = Decimal("0.01")


def to_money(v):
    return Decimal(str(v)).quantize(MONEY, rounding=ROUND_HALF_UP)


def get_client(conn, slug):
    row = conn.execute(
        "SELECT * FROM clients WHERE slug = ?", (slug.lower(),)
    ).fetchone()
    if not row:
        raise click.ClickException(
            f"Client '{slug}' not found. Run `zd clients` to see available clients."
        )
    return row


def week_label(iso_dates):
    """Return a compact range covering the ISO date string(s) given.

    The label renders the dates ACTUALLY worked, not the enclosing Mon-Sun
    week, so a line item can never display a date outside the billed period
    (a lone Saturday Aug 1 session reads "Aug 1", not "Week of Jul 27").
    Formats: "Aug 5" (single day), "Aug 3-9" (within one month),
    "Aug 31-Sep 2" (crossing a month boundary). No year, consistent with the
    rest of the invoice; week_key remains the year-inclusive grouping key.

    Accepts a single ISO date string or an iterable of them.
    """
    if isinstance(iso_dates, str):
        iso_dates = [iso_dates]
    days = sorted(date.fromisoformat(d) for d in iso_dates)
    if not days:
        raise ValueError("week_label requires at least one date")
    first, last = days[0], days[-1]
    if first == last:
        return first.strftime("%b %-d")
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.strftime('%b %-d')}-{last.day}"
    return f"{first.strftime('%b %-d')}-{last.strftime('%b %-d')}"


def week_key(iso_date_str):
    """Return the ISO date (year-inclusive) of the Monday of iso_date_str's week.

    Used as the GROUPING key in group_sessions_by_week so two sessions whose
    weeks share a month/day Monday but fall in different years never collapse
    into the same line item (week_label's displayed string has no year)."""
    d = date.fromisoformat(iso_date_str)
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


BACKFILL_SESSIONS = [
    # Add your historical sessions here:
    # ("client-slug", "YYYY-MM-DD", hours, "notes"),
]


SEED_CLIENTS = [
    # Add your clients here:
    # ("slug", "Client Name", hourly_rate),
]


def _due_date_str(invoice_date_str, terms_days=30):
    d = date.fromisoformat(invoice_date_str)
    due = d + timedelta(days=terms_days)
    return due.strftime("%b %-d")


def _month_bounds(month_value):
    """Return inclusive start and exclusive end ISO dates for YYYY-MM."""
    if not month_value or len(month_value) != 7 or month_value[4] != "-":
        raise click.ClickException("Month must be YYYY-MM format.")
    try:
        year = int(month_value[:4])
        month = int(month_value[5:])
        start = date(year, month, 1)
    except ValueError as exc:
        raise click.ClickException("Month must be YYYY-MM format.") from exc

    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()
