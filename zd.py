#!/usr/bin/env python3
"""
zd — Zero Delta time tracker and invoice bridge.

Logs billable sessions and expenses per client, then generates invoices
by calling invoice.py's PDF/CSV machinery directly.

Usage:
    zd log <client> <hours> "<notes>" [--date YYYY-MM-DD]
    zd expense <client> <amount> "<description>" [--date YYYY-MM-DD]
    zd edit <session_id> [--date YYYY-MM-DD] [--hours H] [--notes "..."] [--force]
    zd edit-expense <expense_id> [--date YYYY-MM-DD] [--amount A] [--description "..."] [--force]
    zd status
    zd sessions <client> [--all]
    zd invoice <client> [--date YYYY-MM-DD]
    zd paid <invoice_number>
    zd backfill
    zd clients
"""

import contextlib
import shutil
import sqlite3
import sys
import os
import copy
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import click
from click.shell_completion import CompletionItem


# ---------------------------------------------------------------------------
# Backups — timestamped copies before any destructive write, keep last 5
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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ZD_DB = Path.home() / ".zd.db"

# Locate invoice.py relative to this script (they live in the same project dir)
_SCRIPT_DIR = Path(__file__).resolve().parent
INVOICE_PY = _SCRIPT_DIR / "invoice.py"

CONFIG_FILE = Path.home() / ".invoice_config.json"

LOCAL_SUMMARY_BASE_URL = os.environ.get("ZD_SUMMARY_BASE_URL", "http://127.0.0.1:8086")
LOCAL_SUMMARY_MODEL = os.environ.get("ZD_SUMMARY_MODEL", "summarizer")
LOCAL_SUMMARY_MODEL_PATH = os.environ.get(
    "ZD_SUMMARY_MODEL_PATH",
    str(Path.home() / "models/narrator-bench/gemma-e2b/gemma-4-E2B-it-Q4_K_M.gguf"),
)
LOCAL_SUMMARY_LOG = os.environ.get("ZD_SUMMARY_LOG", "/tmp/zd-summary-server.log")
LOCAL_SUMMARY_TIMEOUT = 30.0
LOCAL_SUMMARY_STARTUP_TIMEOUT = 60.0


class WeekSummaryError(Exception):
    """Raised when weekly summary generation cannot produce usable text."""


class SummaryServerError(Exception):
    """Raised when the local llama-server cannot be brought up for summaries."""

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

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


def _migrate(conn):
    """Bring an existing DB up to _SCHEMA_VERSION.

    Fast path: if PRAGMA user_version is already at the target, do nothing
    (idempotent, no backup). Otherwise back up the DB file once (INV-6:
    every schema mutation is preceded by a backup) and add each missing
    column. Each ALTER is guarded by PRAGMA table_info so a DB that already
    has a column (e.g. an out-of-band `paid_date`) is left untouched.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= _SCHEMA_VERSION:
        return

    missing = [
        (table, alter)
        for table, column, alter in _MIGRATIONS
        if not _column_exists(conn, table, column)
    ]
    if missing:
        # Back up before touching schema. _backup_file is a no-op if the file
        # doesn't exist yet (fresh in-memory create) or was already backed up.
        _backup_file(ZD_DB)
        for _table, alter in missing:
            conn.execute(alter)

    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def get_conn():
    _backup_file(ZD_DB)
    conn = sqlite3.connect(ZD_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT UNIQUE NOT NULL,   -- short name used in CLI args
                name        TEXT NOT NULL,           -- full name for invoices
                rate        REAL NOT NULL,
                created_at  TEXT DEFAULT (date('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   INTEGER NOT NULL REFERENCES clients(id),
                work_date   TEXT NOT NULL,           -- ISO date
                hours       REAL NOT NULL,
                notes       TEXT,
                invoice_id  INTEGER REFERENCES invoices(id),  -- null = unbilled
                created_at  TEXT DEFAULT (datetime('now')),
                -- Columns below are appended by _migrate on existing DBs via
                -- ALTER TABLE ADD COLUMN, which always appends. Keep them last
                -- here so a fresh init_db and a migrated DB have identical
                -- column ordering (see _migrate / _SCHEMA_VERSION).
                billed_rate REAL                     -- rate locked in at invoice time
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   INTEGER NOT NULL REFERENCES clients(id),
                expense_date TEXT NOT NULL,
                amount      REAL NOT NULL,
                description TEXT,
                invoice_id  INTEGER REFERENCES invoices(id),
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number  TEXT UNIQUE NOT NULL,
                client_id       INTEGER NOT NULL REFERENCES clients(id),
                invoice_date    TEXT NOT NULL,
                total           REAL NOT NULL,
                status          TEXT DEFAULT 'Sent',  -- Sent | Paid
                pdf_path        TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                -- Columns below are appended by _migrate on existing DBs via
                -- ALTER TABLE ADD COLUMN, which always appends. Keep them last
                -- here so a fresh init_db and a migrated DB have identical
                -- column ordering (see _migrate / _SCHEMA_VERSION).
                paid_date       TEXT,                  -- ISO date the invoice was paid
                billing_mode    TEXT DEFAULT 'hourly'  -- hourly | flat
            );
        """)
        _migrate(conn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MONEY = Decimal("0.01")

def to_money(v):
    return Decimal(str(v)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _complete_client(ctx, param, incomplete):
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT slug FROM clients WHERE slug LIKE ?", (incomplete + "%",)
        ).fetchall()
        conn.close()
        return [CompletionItem(r["slug"]) for r in rows]
    except Exception:
        return []


def _worklog_path():
    """Return the worklog Path from config, or None if not configured."""
    try:
        import json
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        p = config.get("storage", {}).get("worklog_file", "")
        return Path(p) if p else None
    except Exception:
        return None


def _worklog(entry: str):
    """Append a structured entry to the configured worklog. Never raises."""
    try:
        path = _worklog_path()
        if not path:
            return
        today = date.today()
        header = f"{today.month}/{today.day}/{today.year}"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        last_header_line = next(
            (l for l in reversed(text.splitlines()) if l.strip() and not l.startswith("-")),
            None,
        )
        needs_header = last_header_line != header
        with open(path, "a", encoding="utf-8") as f:
            if needs_header:
                sep = "\n\n" if text and not text.endswith("\n\n") else ("\n" if text and not text.endswith("\n") else "")
                f.write(f"{sep}{header}\n")
            f.write(f"{entry}\n")
    except Exception:
        pass  # Never block the main operation


def _sync_client_to_config(name):
    """Ensure a client entry exists in ~/.invoice_config.json by name.

    Creates a minimal entry (name only) if missing so invoice PDF
    generation can find the client. Does nothing if already present.
    """
    import json
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    except Exception:
        config = {}

    clients = config.setdefault("clients", [])
    for c in clients:
        if c.get("name", "").lower() == name.lower():
            return  # already present
    clients.append({
        "name": name,
        "contact": "",
        "address": "",
        "city": "",
        "state": "",
        "zip": "",
    })
    _backup_file(CONFIG_FILE)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def get_client(conn, slug):
    row = conn.execute(
        "SELECT * FROM clients WHERE slug = ?", (slug.lower(),)
    ).fetchone()
    if not row:
        raise click.ClickException(
            f"Client '{slug}' not found. Run `zd clients` to see available clients."
        )
    return row


def week_label(iso_date_str):
    """Return 'Week of Mon DD' for a given ISO date string."""
    d = date.fromisoformat(iso_date_str)
    # Find Monday of that week
    monday = d - timedelta(days=d.weekday())
    return f"Week of {monday.strftime('%b %-d')}"


def _clean_week_summary(text):
    """Normalize local LLM output into a short invoice-safe phrase."""
    summary = " ".join(str(text or "").strip().split())
    if not summary:
        raise WeekSummaryError("empty summary")
    summary = summary.strip("\"'` ")
    if summary.endswith("."):
        summary = summary[:-1]
    if len(summary) > 140:
        summary = summary[:137].rstrip() + "..."
    return summary


def _notes_for_summary(sessions):
    """Build compact dated notes text for a weekly summary prompt."""
    lines = []
    for s in sessions:
        notes = (s["notes"] or "").strip()
        if notes:
            lines.append(f"- {s['work_date']}: {notes}")
    return "\n".join(lines)


def _summary_timeout(value, default=LOCAL_SUMMARY_TIMEOUT):
    """Return a positive timeout value, falling back for invalid config/env input."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    if timeout <= 0:
        return default
    return timeout


def _weekly_summary_config(config):
    """Return normalized weekly summary settings from invoice config.

    Adds `model_path` (path to the GGUF weights) and `log_path` (where
    llama-server's stdout/stderr go when we spawn it) so the auto-start
    helper can find them without extra config plumbing."""
    summary_config = (
        config.get("zd", {})
        .get("weekly_summaries", {})
    )
    default_timeout = _summary_timeout(os.environ.get("ZD_SUMMARY_TIMEOUT"))
    return {
        "enabled": bool(summary_config.get("enabled", False)),
        "base_url": summary_config.get("base_url") or LOCAL_SUMMARY_BASE_URL,
        "model": summary_config.get("model") or LOCAL_SUMMARY_MODEL,
        "model_path": summary_config.get("model_path") or LOCAL_SUMMARY_MODEL_PATH,
        "log_path": summary_config.get("log_path") or LOCAL_SUMMARY_LOG,
        "timeout_seconds": _summary_timeout(summary_config.get("timeout_seconds"), default_timeout),
    }


def _server_alive(base_url, timeout=2.0):
    """Return True if base_url responds 200 on /health."""
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _parse_host_port(base_url, default_port=8086):
    parsed = urllib.parse.urlparse(base_url)
    return parsed.hostname or "127.0.0.1", str(parsed.port or default_port)


def _spawn_summary_server(model_path, base_url, alias, log_path):
    """Spawn llama-server in the background with megalodon's locked argv
    pattern. Returns the Popen handle. Raises SummaryServerError if the
    binary or model file are missing."""
    import shutil
    import subprocess
    if not shutil.which("llama-server"):
        raise SummaryServerError(
            "llama-server not found on PATH (brew install llama.cpp)."
        )
    if not Path(model_path).exists():
        raise SummaryServerError(
            f"summary model GGUF not found at {model_path}. "
            f"Set zd.weekly_summaries.model_path in ~/.invoice_config.json "
            f"or ZD_SUMMARY_MODEL_PATH env var."
        )
    host, port = _parse_host_port(base_url)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab", buffering=0)
    return subprocess.Popen(
        [
            "llama-server",
            "-m", str(model_path),
            "--alias", alias,
            "--jinja",
            "--chat-template-kwargs", '{"enable_thinking":false}',
            "-ngl", "99",
            "-c", "8192",
            "--host", host,
            "--port", port,
        ],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,  # decouple from zd's signal group
    )


def _wait_for_summary_server(base_url, timeout=LOCAL_SUMMARY_STARTUP_TIMEOUT, poll_interval=0.25):
    """Block until base_url's /health returns 200 or timeout. Raises on timeout."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if _server_alive(base_url, timeout=1.0):
            return
        _time.sleep(poll_interval)
    raise SummaryServerError(
        f"llama-server at {base_url} did not become ready within {timeout:.0f}s"
    )


def _shutdown_summary_server(proc, timeout=10.0):
    """Terminate a spawned server. SIGTERM first, then SIGKILL if it lingers."""
    import subprocess
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except Exception:
            pass
    except Exception:
        pass


@contextlib.contextmanager
def _summary_server_context(summary_settings):
    """Ensure llama-server is up for the duration of the block.

    - If a server is already responding at base_url, use it as-is. Do NOT
      shut it down on exit — it belongs to someone else.
    - Otherwise spawn llama-server with the configured GGUF model, wait
      for /health to pass, and terminate it when the block exits."""
    base_url = summary_settings["base_url"]
    we_started = False
    proc = None
    if not _server_alive(base_url):
        click.echo("  Starting local llama-server for summaries (cold start)...")
        proc = _spawn_summary_server(
            model_path=summary_settings["model_path"],
            base_url=base_url,
            alias=summary_settings["model"],
            log_path=summary_settings["log_path"],
        )
        we_started = True
        try:
            _wait_for_summary_server(base_url)
        except SummaryServerError:
            _shutdown_summary_server(proc)
            raise
    try:
        yield
    finally:
        if we_started and proc is not None:
            click.echo("  Stopping local llama-server.")
            _shutdown_summary_server(proc)


def summarize_week_with_local_gemma(
    label,
    sessions,
    *,
    base_url=LOCAL_SUMMARY_BASE_URL,
    model=LOCAL_SUMMARY_MODEL,
    timeout=LOCAL_SUMMARY_TIMEOUT,
):
    """Generate a one-line weekly invoice summary using the local Gemma server."""
    notes = _notes_for_summary(sessions)
    if not notes:
        raise WeekSummaryError("no notes to summarize")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write concise professional invoice line item summaries. "
                    "Return exactly one plain-text sentence fragment, no markdown, no quotes, no bullets."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Summarize this consulting work for {label} in 12 words or fewer. "
                    "Do not mention hours, dates, rates, invoices, or the client name.\n\n"
                    f"{notes}"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 48,
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise WeekSummaryError(str(exc)) from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise WeekSummaryError("local summary response missing content") from exc
    return _clean_week_summary(content)


def group_sessions_by_week(sessions, summary_provider=None):
    """
    Group a list of session rows by calendar week.
    Returns list of dicts: {label, hours, rate, amount, sessions}
    """
    weeks = {}
    for s in sessions:
        label = week_label(s["work_date"])
        if label not in weeks:
            weeks[label] = {"label": label, "sessions": [], "hours": 0.0, "rate": s["rate"]}
        weeks[label]["sessions"].append(s)
        weeks[label]["hours"] += s["hours"]

    result = []
    for label, data in weeks.items():
        rate = data["rate"]
        # Use the raw accumulated hours — to_money already quantizes the
        # amount to cents (Decimal/ROUND_HALF_UP). A pre-round of hours here
        # is an extra rounding step that only adds skew (INV-4).
        hours = data["hours"]
        amount = float(to_money(hours * rate))
        description = data["label"]
        if summary_provider is not None:
            try:
                summary = summary_provider(data["label"], data["sessions"])
                if summary:
                    description = f"{data['label']} - {_clean_week_summary(summary)}"
            except WeekSummaryError as exc:
                click.echo(f"  ⚠  Weekly summary unavailable for {data['label']}: {exc}")
        result.append({
            "description": description,
            "hours": hours,
            "rate": rate,
            "amount": amount,
        })
    return result


# ---------------------------------------------------------------------------
# Seed data (backfill)
# ---------------------------------------------------------------------------

BACKFILL_SESSIONS = [
    # Add your historical sessions here:
    # ("client-slug", "YYYY-MM-DD", hours, "notes"),
]

SEED_CLIENTS = [
    # Add your clients here:
    # ("slug", "Client Name", hourly_rate),
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.pass_context
def cli(ctx):
    """zd — Zero Delta time tracker and invoice bridge.

    \b
    CLIENT MANAGEMENT
      zd clients
      zd add-client <slug> "<Full Name>" <rate>
      zd add-client acme "Acme Corp" 95.00
      zd add-client acme "Acme Corp" 110.00          # updates rate if slug exists
      zd backfill                                     # seed from SEED_CLIENTS / BACKFILL_SESSIONS

    \b
    LOGGING WORK
      zd log <client> <hours> "<notes>"
      zd log acme 1.5 "reviewed contracts"
      zd log acme 2.0 "development work" --date 2026-03-18
      zd expense <client> <amount> "<description>"
      zd expense acme 42.00 "domain renewal"
      zd expense acme 199.00 "software license" --date 2026-03-15

    \b
    EDITING ENTRIES
      zd edit <id> --date 2026-03-20                # fix date on a session
      zd edit <id> --hours 2.0 --notes "updated"    # change hours and notes
      zd edit-expense <id> --amount 50.00            # fix expense amount
      zd edit <id> --hours 1.0 --force               # edit even if already billed

    \b
    REVIEWING WORK
      zd status
      zd sessions                                     # all clients, unbilled sessions
      zd sessions <client>                            # one client, unbilled sessions
      zd sessions <client> --all                      # one client, all sessions (inc. billed)
      zd sessions --all                               # all clients, all sessions

    \b
    INVOICING
      zd invoice <client>
      zd invoice acme
      zd invoice acme --date 2026-03-31
      zd invoice acme --regenerate 2026-0002   # re-create PDF for existing invoice
      zd paid <invoice_number>
      zd paid 2026-0003
    """
    if ctx.resilient_parsing or any(arg in ("--help", "-h") for arg in sys.argv[1:]):
        return
    init_db()


@cli.command("clients")
def cmd_clients():
    """List all clients and their rates."""
    with get_conn() as conn:
        rows = conn.execute("SELECT slug, name, rate FROM clients ORDER BY name").fetchall()
    if not rows:
        click.echo("No clients found. Run `zd backfill` to seed initial clients.")
        return
    click.echo()
    click.echo(f"  {'SLUG':<16} {'NAME':<30} {'RATE':>8}")
    click.echo("  " + "-" * 58)
    for r in rows:
        click.echo(f"  {r['slug']:<16} {r['name']:<30} ${r['rate']:>6.2f}/hr")
    click.echo()


@cli.command("log")
@click.argument("client", shell_complete=_complete_client)
@click.argument("hours", type=float)
@click.argument("notes")
@click.option("--date", "work_date", default=None, help="Date YYYY-MM-DD (default: today)")
def cmd_log(client, hours, notes, work_date):
    """Log a billable session.

    \b
    CLIENT is the short slug you assigned when adding the client.
    HOURS accepts decimals (e.g. 1.5 = 1h 30m).

    \b
    Examples:
      zd log acme 1.5 "reviewed contracts"
      zd log acme 2.0 "development work" --date 2026-03-18
      zd log acme 0.5 "quick call"
    """
    if work_date is None:
        work_date = date.today().isoformat()
    else:
        try:
            date.fromisoformat(work_date)
        except ValueError:
            raise click.ClickException("Date must be YYYY-MM-DD format.")

    if hours <= 0:
        raise click.ClickException("Hours must be greater than 0.")

    with get_conn() as conn:
        c = get_client(conn, client)
        conn.execute(
            "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
            (c["id"], work_date, hours, notes),
        )

    amount = to_money(hours * c["rate"])
    click.echo(f"  ✓  {work_date}  {c['name']}  {hours}h @ ${c['rate']:.2f}/hr = ${amount:,.2f}")
    _worklog(f"- [zd log] {work_date} | {c['slug']} | {hours}h @ ${c['rate']:.2f}/hr = ${amount:,.2f} | \"{notes}\"")


@cli.command("expense")
@click.argument("client", shell_complete=_complete_client)
@click.argument("amount", type=float)
@click.argument("description")
@click.option("--date", "expense_date", default=None, help="Date YYYY-MM-DD (default: today)")
def cmd_expense(client, amount, description, expense_date):
    """Log a reimbursable expense.

    \b
    Expenses appear as separate line items on the next invoice.

    \b
    Examples:
      zd expense acme 42.00 "domain renewal"
      zd expense acme 199.00 "software license" --date 2026-03-15
    """
    if expense_date is None:
        expense_date = date.today().isoformat()
    else:
        try:
            date.fromisoformat(expense_date)
        except ValueError:
            raise click.ClickException("Date must be YYYY-MM-DD format.")

    if amount <= 0:
        raise click.ClickException("Amount must be greater than 0.")

    with get_conn() as conn:
        c = get_client(conn, client)
        conn.execute(
            "INSERT INTO expenses (client_id, expense_date, amount, description) VALUES (?,?,?,?)",
            (c["id"], expense_date, amount, description),
        )

    click.echo(f"  ✓  {expense_date}  {c['name']}  ${amount:,.2f}  {description}")
    _worklog(f"- [zd expense] {expense_date} | {c['slug']} | ${amount:,.2f} | \"{description}\"")


@cli.command("status")
def cmd_status():
    """Show unbilled hours and outstanding invoices across all clients.

    \b
    Displays two sections:
      UNBILLED          — hours and expenses not yet invoiced, per client
      OUTSTANDING       — sent invoices not yet marked paid

    \b
    Example:
      zd status
    """
    with get_conn() as conn:
        clients = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()

        click.echo()
        click.echo("  UNBILLED")
        click.echo("  " + "-" * 52)
        grand_unbilled = Decimal("0")
        any_unbilled = False
        for c in clients:
            sessions = conn.execute(
                """SELECT s.*, cl.rate FROM sessions s
                   JOIN clients cl ON cl.id = s.client_id
                   WHERE s.client_id = ? AND s.invoice_id IS NULL
                   ORDER BY s.work_date""",
                (c["id"],),
            ).fetchall()
            expenses = conn.execute(
                "SELECT * FROM expenses WHERE client_id = ? AND invoice_id IS NULL",
                (c["id"],),
            ).fetchall()
            if not sessions and not expenses:
                continue
            any_unbilled = True
            total_hours = sum(s["hours"] for s in sessions)
            total_exp = sum(e["amount"] for e in expenses)
            labor = to_money(total_hours * c["rate"])
            total = labor + to_money(total_exp)
            grand_unbilled += total
            exp_note = f" + ${total_exp:,.2f} expenses" if total_exp else ""
            click.echo(
                f"  {c['name']:<30} {total_hours:>5.1f}h  ${labor:>8,.2f}{exp_note}  →  ${total:>8,.2f}"
            )
        if not any_unbilled:
            click.echo("  All hours billed.")
        else:
            click.echo("  " + "-" * 52)
            click.echo(f"  {'TOTAL UNBILLED':<30}         ${grand_unbilled:>8,.2f}")

        # Outstanding invoices
        outstanding = conn.execute(
            """SELECT i.invoice_number, c.name, i.invoice_date, i.total, i.status
               FROM invoices i JOIN clients c ON c.id = i.client_id
               WHERE i.status != 'Paid'
               ORDER BY i.invoice_date""",
        ).fetchall()

        click.echo()
        click.echo("  OUTSTANDING INVOICES")
        click.echo("  " + "-" * 52)
        if not outstanding:
            click.echo("  None.")
        else:
            for inv in outstanding:
                due = _due_date_str(inv["invoice_date"])
                click.echo(
                    f"  {inv['invoice_number']:<14} {inv['name']:<22} ${inv['total']:>8,.2f}  due {due}"
                )
        click.echo()


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


@cli.command("sessions")
@click.argument("client", required=False, default=None, shell_complete=_complete_client)
@click.option("--all", "show_all", is_flag=True, help="Include already-billed sessions")
def cmd_sessions(client, show_all):
    """List sessions for a client, or all clients if CLIENT is omitted.

    \b
    Shows unbilled sessions by default. Use --all to include
    sessions already attached to a prior invoice.

    \b
    Examples:
      zd sessions                    # all clients, unbilled
      zd sessions --all              # all clients, all sessions
      zd sessions acme               # one client, unbilled
      zd sessions acme --all         # one client, all sessions
    """
    with get_conn() as conn:
        if client:
            clients = [get_client(conn, client)]
        else:
            clients = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()

        if not clients:
            click.echo("  No clients found.")
            return

        click.echo()
        label_suffix = "all" if show_all else "unbilled"
        grand_h = 0.0
        grand_amt = Decimal("0")

        for c in clients:
            query = """
                SELECT s.*, cl.rate,
                       CASE WHEN s.invoice_id IS NULL THEN 'unbilled' ELSE i.invoice_number END as inv_label
                FROM sessions s
                JOIN clients cl ON cl.id = s.client_id
                LEFT JOIN invoices i ON i.id = s.invoice_id
                WHERE s.client_id = ?
            """
            if not show_all:
                query += " AND s.invoice_id IS NULL"
            query += " ORDER BY s.work_date"
            rows = conn.execute(query, (c["id"],)).fetchall()

            if not rows:
                if client:
                    click.echo(f"  No {label_suffix} sessions for {c['name']}.")
                continue

            click.echo(f"  {c['name']} — {label_suffix} sessions")
            click.echo(f"  {'ID':>5}  {'DATE':<12} {'HRS':>5}  {'AMOUNT':>8}  {'STATUS':<12}  NOTES")
            click.echo("  " + "-" * 79)
            total_h = 0.0
            total_amt = Decimal("0")
            for r in rows:
                amt = to_money(r["hours"] * r["rate"])
                total_h += r["hours"]
                total_amt += amt
                notes_trunc = (r["notes"] or "")[:40]
                click.echo(
                    f"  {r['id']:>5}  {r['work_date']:<12} {r['hours']:>5.1f}  ${amt:>7,.2f}  {r['inv_label']:<12}  {notes_trunc}"
                )
            click.echo("  " + "-" * 79)
            click.echo(f"  {'':>5}  {'TOTAL':<12} {total_h:>5.1f}  ${total_amt:>7,.2f}")
            click.echo()
            grand_h += total_h
            grand_amt += total_amt

        if not client and len(clients) > 1:
            click.echo(f"  {'':>5}  {'GRAND TOTAL':<12} {grand_h:>5.1f}  ${grand_amt:>7,.2f}")
            click.echo()


@cli.command("edit")
@click.argument("session_id", type=int)
@click.option("--date", "work_date", default=None, help="New date YYYY-MM-DD")
@click.option("--hours", type=float, default=None, help="New hours value")
@click.option("--notes", default=None, help="New notes text")
@click.option("--force", is_flag=True, help="Allow editing billed sessions")
def cmd_edit(session_id, work_date, hours, notes, force):
    """Edit an existing session.

    \b
    Use `zd sessions` to find the session ID, then update
    any combination of date, hours, and notes.

    \b
    Examples:
      zd edit 14 --date 2026-03-20
      zd edit 14 --hours 2.0 --notes "updated description"
      zd edit 14 --hours 1.0 --force   # edit even if already billed
    """
    if work_date is None and hours is None and notes is None:
        raise click.ClickException(
            "Nothing to update. Provide at least one of --date, --hours, or --notes."
        )

    if work_date is not None:
        try:
            date.fromisoformat(work_date)
        except ValueError:
            raise click.ClickException("Date must be YYYY-MM-DD format.")

    if hours is not None and hours <= 0:
        raise click.ClickException("Hours must be greater than 0.")

    with get_conn() as conn:
        row = conn.execute(
            """SELECT s.*, c.slug, c.name, c.rate
               FROM sessions s JOIN clients c ON c.id = s.client_id
               WHERE s.id = ?""",
            (session_id,),
        ).fetchone()
        if not row:
            raise click.ClickException(f"Session {session_id} not found.")

        if row["invoice_id"] is not None and not force:
            raise click.ClickException(
                f"Session {session_id} is already billed. Use --force to edit anyway."
            )

        updates = []
        values = []
        changes = []
        if work_date is not None:
            updates.append("work_date = ?")
            values.append(work_date)
            changes.append(f"date: {row['work_date']} → {work_date}")
        if hours is not None:
            updates.append("hours = ?")
            values.append(hours)
            changes.append(f"hours: {row['hours']} → {hours}")
        if notes is not None:
            updates.append("notes = ?")
            values.append(notes)
            changes.append(f"notes updated")

        values.append(session_id)
        conn.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
            values,
        )

    click.echo(f"  ✓  Session {session_id} updated: {'; '.join(changes)}")
    _worklog(f"- [zd edit] session {session_id} | {row['slug']} | {'; '.join(changes)}")


@cli.command("edit-expense")
@click.argument("expense_id", type=int)
@click.option("--date", "expense_date", default=None, help="New date YYYY-MM-DD")
@click.option("--amount", type=float, default=None, help="New amount")
@click.option("--description", default=None, help="New description")
@click.option("--force", is_flag=True, help="Allow editing billed expenses")
def cmd_edit_expense(expense_id, expense_date, amount, description, force):
    """Edit an existing expense.

    \b
    Examples:
      zd edit-expense 3 --amount 50.00
      zd edit-expense 3 --date 2026-03-20 --description "updated"
    """
    if expense_date is None and amount is None and description is None:
        raise click.ClickException(
            "Nothing to update. Provide at least one of --date, --amount, or --description."
        )

    if expense_date is not None:
        try:
            date.fromisoformat(expense_date)
        except ValueError:
            raise click.ClickException("Date must be YYYY-MM-DD format.")

    if amount is not None and amount <= 0:
        raise click.ClickException("Amount must be greater than 0.")

    with get_conn() as conn:
        row = conn.execute(
            """SELECT e.*, c.slug, c.name
               FROM expenses e JOIN clients c ON c.id = e.client_id
               WHERE e.id = ?""",
            (expense_id,),
        ).fetchone()
        if not row:
            raise click.ClickException(f"Expense {expense_id} not found.")

        if row["invoice_id"] is not None and not force:
            raise click.ClickException(
                f"Expense {expense_id} is already billed. Use --force to edit anyway."
            )

        updates = []
        values = []
        changes = []
        if expense_date is not None:
            updates.append("expense_date = ?")
            values.append(expense_date)
            changes.append(f"date: {row['expense_date']} → {expense_date}")
        if amount is not None:
            updates.append("amount = ?")
            values.append(amount)
            changes.append(f"amount: ${row['amount']:,.2f} → ${amount:,.2f}")
        if description is not None:
            updates.append("description = ?")
            values.append(description)
            changes.append(f"description updated")

        values.append(expense_id)
        conn.execute(
            f"UPDATE expenses SET {', '.join(updates)} WHERE id = ?",
            values,
        )

    click.echo(f"  ✓  Expense {expense_id} updated: {'; '.join(changes)}")
    _worklog(f"- [zd edit-expense] expense {expense_id} | {row['slug']} | {'; '.join(changes)}")


@cli.command("invoice")
@click.argument("client", shell_complete=_complete_client)
@click.option("--date", "invoice_date", default=None, help="Invoice date YYYY-MM-DD (default: today)")
@click.option(
    "--month",
    "invoice_month",
    metavar="YYYY-MM",
    default=None,
    help="Only invoice unbilled items in this calendar month (YYYY-MM).",
)
@click.option(
    "--summarize-weeks",
    is_flag=True,
    help="Use local Gemma to add one-line summaries to weekly line items.",
)
@click.option("--regenerate", default=None, help="Regenerate PDF for an existing invoice number")
def cmd_invoice(client, invoice_date, invoice_month, summarize_weeks, regenerate):
    """Generate an invoice PDF for all unbilled sessions of a client.

    \b
    Pulls all unbilled sessions and expenses, groups them into weekly
    line items, generates a PDF via invoice.py, appends to the CSV
    ledger, and marks everything billed in the zd database.

    \b
    Use --regenerate to re-create the PDF for an already-billed invoice
    (e.g. after fixing config, correcting a session, or updating rates).

    \b
    Examples:
      zd invoice acme
      zd invoice acme --date 2026-03-31
      zd invoice acme --month 2026-04 --date 2026-04-30
      zd invoice acme --month 2026-04 --summarize-weeks
      zd invoice acme --regenerate 2026-0002
    """
    month_start = None
    month_end = None
    if invoice_month is not None:
        month_start, month_end = _month_bounds(invoice_month)

    # --- Load invoice.py config and machinery ---
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("invoice", INVOICE_PY)
        inv_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inv_mod)
    except Exception as e:
        raise click.ClickException(f"Could not load invoice.py: {e}")

    config = inv_mod.load_config()
    summary_settings = _weekly_summary_config(config)
    effective_summarize_weeks = bool(summarize_weeks or summary_settings["enabled"])

    def _summary_func(label, week_sessions):
        return summarize_week_with_local_gemma(
            label,
            week_sessions,
            base_url=summary_settings["base_url"],
            model=summary_settings["model"],
            timeout=summary_settings["timeout_seconds"],
        )

    with get_conn() as conn:
        c = get_client(conn, client)

        # --- Match client profile in invoice.py config ---
        inv_clients = config.get("clients", [])
        matched_client = None
        for ic in inv_clients:
            if c["name"].lower() in ic.get("name", "").lower() or \
               ic.get("name", "").lower() in c["name"].lower():
                matched_client = ic
                break
        if not matched_client and inv_clients:
            click.echo(f"  Could not auto-match '{c['name']}' to an invoice.py client profile.")
            click.echo("  Available profiles:")
            for i, ic in enumerate(inv_clients):
                click.echo(f"    {i+1}. {ic.get('name')}")
            idx = click.prompt("  Select profile number", type=click.IntRange(1, len(inv_clients)))
            matched_client = inv_clients[idx - 1]

        if regenerate:
            # ---- Regenerate existing invoice ----
            inv_row = conn.execute(
                "SELECT * FROM invoices WHERE invoice_number = ? AND client_id = ?",
                (regenerate, c["id"]),
            ).fetchone()
            if not inv_row:
                raise click.ClickException(
                    f"Invoice {regenerate} not found for client {c['name']}."
                )

            invoice_number = inv_row["invoice_number"]
            invoice_date = inv_row["invoice_date"]
            inv_id = inv_row["id"]

            sessions = conn.execute(
                """SELECT s.*, cl.rate FROM sessions s
                   JOIN clients cl ON cl.id = s.client_id
                   WHERE s.invoice_id = ?
                   ORDER BY s.work_date""",
                (inv_id,),
            ).fetchall()

            expenses = conn.execute(
                "SELECT * FROM expenses WHERE invoice_id = ?",
                (inv_id,),
            ).fetchall()

            summary_func = _summary_func if effective_summarize_weeks else None
            if effective_summarize_weeks:
                with _summary_server_context(summary_settings):
                    line_items = group_sessions_by_week(sessions, summary_provider=summary_func)
            else:
                line_items = group_sessions_by_week(sessions, summary_provider=summary_func)
            for e in expenses:
                line_items.append({
                    "description": f"Expense: {e['description']}",
                    "hours": 0,
                    "rate": 0,
                    "amount": float(to_money(e["amount"])),
                })

            total_hours = sum(s["hours"] for s in sessions)
            total_labor = to_money(total_hours * c["rate"])
            total_exp = to_money(sum(e["amount"] for e in expenses))
            total = total_labor + total_exp

            click.echo(f"\n  Regenerating invoice {invoice_number} for {c['name']}")
            click.echo(f"  {len(sessions)} sessions → {len(line_items)} weekly line items")
            click.echo(f"  {total_hours:.1f} hours @ ${c['rate']:.2f}/hr = ${total_labor:,.2f}")
            if total_exp:
                click.echo(f"  Expenses: ${total_exp:,.2f}")
            click.echo(f"  Total: ${total:,.2f}")

            if not click.confirm("\n  Proceed?"):
                click.echo("  Cancelled.")
                return

            # Generate PDF
            invoices_dir = str(inv_mod._invoices_dir_from_config(config))
            Path(invoices_dir).mkdir(parents=True, exist_ok=True)
            client_slug = inv_mod._sanitize_filename_component(c["name"], "Client")
            safe_num = inv_mod._sanitize_filename_component(invoice_number, "invoice")
            pdf_filename = f"{client_slug}_Invoice_{safe_num}.pdf"
            pdf_path = str(Path(invoices_dir) / pdf_filename)

            actual_total = inv_mod.generate_pdf(
                invoice_number, invoice_date, config, line_items, pdf_path,
                client=matched_client, payment_terms="Net 30",
            )

            # Update DB record
            conn.execute(
                "UPDATE invoices SET total = ?, pdf_path = ? WHERE id = ?",
                (float(actual_total), pdf_path, inv_id),
            )

            # Update CSV ledger row
            csv_file = str(inv_mod._ledger_path_from_config(config))
            csv_path = Path(csv_file)
            if csv_path.exists():
                try:
                    with inv_mod._file_lock(csv_path):
                        rows, file_headers = inv_mod._read_csv_with_headers(csv_path)
                        inv_key = inv_mod._csv_field_key(file_headers, "invoice_number") or "invoice_number"
                        total_key = inv_mod._csv_field_key(file_headers, "total") or "total"
                        pdf_key = inv_mod._csv_field_key(file_headers, "pdf_file") or "pdf_file"
                        for r in rows:
                            if r.get(inv_key) == invoice_number:
                                r[total_key] = f"{float(actual_total):.2f}"
                                r[pdf_key] = pdf_path
                                break
                        inv_mod._atomic_write_csv(csv_path, rows, file_headers)
                except Exception as e:
                    click.echo(f"  ⚠  Could not update CSV ledger: {e}")

            # Remove old PDF if path changed
            old_path = inv_row["pdf_path"]
            if old_path and old_path != pdf_path and Path(old_path).exists():
                Path(old_path).unlink()

            click.echo(f"\n  ✓  Invoice {invoice_number} regenerated: {pdf_path}")
            click.echo(f"  ✓  Total: ${actual_total:,.2f}")
            _worklog(f"- [zd invoice] {invoice_date} | {invoice_number} | {c['name']} | ${actual_total:,.2f} | regenerated")
            return

        # ---- New invoice ----
        if invoice_date is None:
            invoice_date = date.today().isoformat()

        session_query = """SELECT s.*, cl.rate FROM sessions s
               JOIN clients cl ON cl.id = s.client_id
               WHERE s.client_id = ? AND s.invoice_id IS NULL"""
        session_params = [c["id"]]
        expense_query = "SELECT * FROM expenses WHERE client_id = ? AND invoice_id IS NULL"
        expense_params = [c["id"]]
        if month_start is not None and month_end is not None:
            session_query += " AND s.work_date >= ? AND s.work_date < ?"
            session_params.extend([month_start, month_end])
            expense_query += " AND expense_date >= ? AND expense_date < ?"
            expense_params.extend([month_start, month_end])
        session_query += " ORDER BY s.work_date"
        expense_query += " ORDER BY expense_date"

        sessions = conn.execute(session_query, session_params).fetchall()
        expenses = conn.execute(expense_query, expense_params).fetchall()

        if not sessions and not expenses:
            scope = f" in {invoice_month}" if invoice_month else ""
            click.echo(f"  No unbilled sessions or expenses for {c['name']}{scope}.")
            return

        # Build line items grouped by week. When summarization is enabled,
        # ensure the local llama-server is up for the entire grouping pass
        # — _summary_server_context spawns it cold if needed and tears it
        # down when we exit, so no orphan server lingers.
        summary_func = _summary_func if effective_summarize_weeks else None
        if effective_summarize_weeks:
            click.echo("  Summarizing weekly line items with local Gemma model...")
            with _summary_server_context(summary_settings):
                line_items = group_sessions_by_week(sessions, summary_provider=summary_func)
        else:
            line_items = group_sessions_by_week(sessions, summary_provider=summary_func)

        # Add expense line items if any
        for e in expenses:
            line_items.append({
                "description": f"Expense: {e['description']}",
                "hours": 0,
                "rate": 0,
                "amount": float(to_money(e["amount"])),
            })

        # Get next invoice number — take the max of CSV ledger and zd DB
        # so they never collide even if one source is ahead of the other.
        csv_file = str(inv_mod._ledger_path_from_config(config))
        csv_next = inv_mod.get_next_invoice_number(csv_file)

        current_year = date.today().year
        db_last = conn.execute(
            "SELECT invoice_number FROM invoices WHERE invoice_number LIKE ? ORDER BY invoice_number DESC LIMIT 1",
            (f"{current_year}-%",),
        ).fetchone()
        db_next_num = 1
        if db_last:
            try:
                db_next_num = int(db_last["invoice_number"].split("-", 1)[1]) + 1
            except (ValueError, IndexError):
                pass
        db_next = f"{current_year}-{db_next_num:04d}"

        # Pick whichever is higher
        invoice_number = max(csv_next, db_next)
        click.echo(f"\n  Generating invoice {invoice_number} for {c['name']}")
        if invoice_month:
            click.echo(f"  Month: {invoice_month}")
        click.echo(f"  {len(sessions)} sessions → {len(line_items)} weekly line items")

        # Confirm before generating.
        #
        # The confirmation total MUST equal the persisted total by
        # construction (INV-4). generate_pdf derives the invoice total by
        # summing to_money(item["amount"]) over every line item and then
        # quantizing the running sum once (invoice.py: subtotal += amount;
        # subtotal = _to_money_decimal(subtotal)). Reproduce that here from
        # the SAME line_items that are handed to generate_pdf, so the number
        # the user approves is exactly the number written to the PDF/CSV/DB.
        #
        # Summing the per-week to_money(hours*rate) amounts is NOT the same as
        # to_money(total_hours * rate) (sum-of-rounded != rounded-of-sum), so
        # the old aggregate labor figure could diverge from what was billed.
        total_hours = sum(s["hours"] for s in sessions)
        # Labor = per-week line items (hours or rate set); expenses = the rest.
        total_labor = sum(
            (to_money(str(li["amount"])) for li in line_items
             if li.get("hours") or li.get("rate")),
            Decimal("0.00"),
        )
        total_exp = sum(
            (to_money(str(li["amount"])) for li in line_items
             if not li.get("hours") and not li.get("rate")),
            Decimal("0.00"),
        )
        total = to_money(sum(
            (to_money(str(li["amount"])) for li in line_items),
            Decimal("0.00"),
        ))
        click.echo(f"  {total_hours:.1f} hours @ ${c['rate']:.2f}/hr = ${total_labor:,.2f}")
        if total_exp:
            click.echo(f"  Expenses: ${total_exp:,.2f}")
        click.echo(f"  Total: ${total:,.2f}")

        if not click.confirm("\n  Proceed?"):
            click.echo("  Cancelled.")
            return

        # ------------------------------------------------------------------
        # DB-authoritative write ordering (INV-2 / INV-5).
        #
        # The SQLite COMMIT is the single point of no return. Before it the
        # ONLY durable artifact we create is a TEMP PDF at a non-final path,
        # which can never overwrite an existing invoice and is deleted on
        # rollback. The final PDF (via os.replace) and the CSV ledger row are
        # written ONLY after the commit, so a crash before the commit leaves
        # nothing durable behind and the sessions stay unbilled — no
        # double-billing on rerun. See plan §A1.
        # ------------------------------------------------------------------

        # Step 1 — Under the ledger file lock, prove the chosen invoice number
        # is absent from BOTH the CSV ledger AND the zd DB before any durable
        # write (closes INV-5). The DB check is cheap and non-durable; holding
        # the ledger lock only for these reads keeps PDF generation and the DB
        # txn out from under the lock (save_to_csv re-locks + re-checks later
        # as the backstop).
        csv_path = Path(csv_file)
        with inv_mod._file_lock(csv_path):
            if csv_path.exists():
                ledger_rows, ledger_headers = inv_mod._read_csv_with_headers(csv_path)
                ledger_inv_key = (
                    inv_mod._csv_field_key(ledger_headers, "invoice_number")
                    or "invoice_number"
                )
                ledger_numbers = {str(r.get(ledger_inv_key, "")) for r in ledger_rows}
            else:
                ledger_numbers = set()
            if invoice_number in ledger_numbers:
                raise click.ClickException(
                    f"Invoice number '{invoice_number}' already exists in the CSV "
                    f"ledger ({csv_path}). Refusing to write a duplicate."
                )
            db_dup = conn.execute(
                "SELECT 1 FROM invoices WHERE invoice_number = ?", (invoice_number,)
            ).fetchone()
            if db_dup:
                raise click.ClickException(
                    f"Invoice number '{invoice_number}' already exists in the zd "
                    "database. Refusing to write a duplicate."
                )

        # Step 2 — Render the PDF to a TEMP path in the invoices dir, NEVER the
        # final path. A temp file at a non-final path can never clobber an
        # existing invoice and is removed on rollback.
        invoices_dir = str(inv_mod._invoices_dir_from_config(config))
        Path(invoices_dir).mkdir(parents=True, exist_ok=True)
        client_slug = inv_mod._sanitize_filename_component(c["name"], "Client")
        safe_num = inv_mod._sanitize_filename_component(invoice_number, "invoice")
        pdf_filename = f"{client_slug}_Invoice_{safe_num}.pdf"
        pdf_path = str(Path(invoices_dir) / pdf_filename)
        temp_pdf = f"{pdf_path}.tmp-{os.getpid()}"

        committed = False
        try:
            actual_total = inv_mod.generate_pdf(
                invoice_number, invoice_date, config, line_items, temp_pdf,
                client=matched_client, payment_terms="Net 30",
            )

            # Step 3 — Explicit transaction. After this commit the DB is
            # authoritative: the invoice exists and its sessions/expenses are
            # billed. Snapshot the client's current rate into
            # sessions.billed_rate so the billed amount is immutable against
            # later rate changes (INV-3).
            conn.execute("BEGIN")
            conn.execute(
                """INSERT INTO invoices
                       (invoice_number, client_id, invoice_date, total, status, pdf_path, billing_mode)
                   VALUES (?,?,?,?,?,?,?)""",
                (invoice_number, c["id"], invoice_date, float(actual_total),
                 "Sent", pdf_path, "hourly"),
            )
            inv_row = conn.execute(
                "SELECT id FROM invoices WHERE invoice_number = ?", (invoice_number,)
            ).fetchone()
            inv_id = inv_row["id"]

            update_session_query = (
                "UPDATE sessions SET invoice_id = ?, billed_rate = ? "
                "WHERE client_id = ? AND invoice_id IS NULL"
            )
            update_session_params = [inv_id, float(c["rate"]), c["id"]]
            update_expense_query = "UPDATE expenses SET invoice_id = ? WHERE client_id = ? AND invoice_id IS NULL"
            update_expense_params = [inv_id, c["id"]]
            if month_start is not None and month_end is not None:
                update_session_query += " AND work_date >= ? AND work_date < ?"
                update_session_params.extend([month_start, month_end])
                update_expense_query += " AND expense_date >= ? AND expense_date < ?"
                update_expense_params.extend([month_start, month_end])
            conn.execute(update_session_query, update_session_params)
            conn.execute(update_expense_query, update_expense_params)

            conn.commit()
            committed = True
        except BaseException:
            # Failure BEFORE the commit: roll back the (uncommitted) DB work and
            # delete the temp PDF. Nothing durable leaked — clean abort, no
            # double-billing. Re-raise so the error surfaces.
            if not committed:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    os.remove(temp_pdf)
                except OSError:
                    pass
            raise

        # ------------------------------------------------------------------
        # Past the commit: the DB is the safe-ahead authoritative store. Any
        # failure projecting to the final PDF or CSV ledger must NOT crash and
        # must NOT re-bill on rerun (sessions are already invoice_id != NULL).
        # ------------------------------------------------------------------

        # Step 4 — Promote the temp PDF to its final path (atomic rename; the
        # final PDF appears only now).
        pdf_finalized = True
        try:
            os.replace(temp_pdf, pdf_path)
        except OSError as e:
            pdf_finalized = False
            click.echo(
                "\n  ⚠  Invoice committed to the zd DB (authoritative), but the "
                f"PDF could not be finalized: {e}"
            )
            click.echo(
                "     The DB record is safe and the CSV row was NOT written, so the "
                "ledger never references a missing PDF. Reproject from the DB to repair."
            )
            try:
                os.remove(temp_pdf)
            except OSError:
                pass

        # Step 5 — Append the CSV ledger row (status="Sent" to match the DB
        # row). Written ONLY when the final PDF is in place, so a failed
        # os.replace can never leave a ledger row pointing at a missing PDF.
        # save_to_csv is atomic (read-all -> append -> os.replace) and
        # re-checks for duplicates under its own lock as the backstop.
        if pdf_finalized:
            try:
                inv_mod.save_to_csv(
                    invoice_number, invoice_date, config, line_items,
                    actual_total, pdf_path, client=matched_client, status="Sent",
                )
            except Exception as e:
                click.echo(
                    "\n  ⚠  Invoice committed to the zd DB (authoritative), but the "
                    f"CSV ledger row could not be written: {e}"
                )
                click.echo(
                    "     The DB record is safe; the CSV projection may need repair."
                )

    if pdf_finalized:
        click.echo(f"\n  ✓  Invoice {invoice_number} saved to: {pdf_path}")
        click.echo(f"  ✓  Total: ${actual_total:,.2f}")
        click.echo(f"  ✓  Ledger updated.")
        click.echo(f"\n  Run `zd paid {invoice_number}` when payment is received.\n")
    else:
        click.echo(
            f"\n  ⚠  Invoice {invoice_number} is recorded in the zd DB "
            f"(total ${actual_total:,.2f}) but its PDF/CSV projection is incomplete."
        )
        click.echo(
            "     The DB is authoritative; the PDF and CSV can be rebuilt from it.\n"
        )
    _worklog(f"- [zd invoice] {invoice_date} | {invoice_number} | {c['name']} | ${actual_total:,.2f} | {len(sessions)} sessions, {sum(s['hours'] for s in sessions):.1f}h | status: Sent")


@cli.command("paid")
@click.argument("invoice_number")
def cmd_paid(invoice_number):
    """Mark an invoice as paid in zd DB and invoice.py's CSV ledger.

    \b
    Updates status to Paid in both the zd SQLite database and the
    invoice.py CSV ledger so both stay in sync.

    \b
    Example:
      zd paid 2026-0003
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE invoice_number = ?", (invoice_number,)
        ).fetchone()
        if not row:
            raise click.ClickException(f"Invoice {invoice_number} not found in zd database.")
        if row["status"] == "Paid":
            click.echo(f"  Invoice {invoice_number} is already marked Paid.")
            return
        conn.execute(
            "UPDATE invoices SET status = 'Paid' WHERE invoice_number = ?", (invoice_number,)
        )

    # Also update invoice.py's CSV ledger
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("invoice", INVOICE_PY)
        inv_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inv_mod)
        config = inv_mod.load_config()
        csv_file = str(inv_mod._ledger_path_from_config(config))

        csv_path = Path(csv_file)
        if csv_path.exists():
            with inv_mod._file_lock(csv_path):
                rows, file_headers = inv_mod._read_csv_with_headers(csv_path)
                inv_key = inv_mod._csv_field_key(file_headers, "invoice_number") or "invoice_number"
                status_key = inv_mod._csv_field_key(file_headers, "status") or "status"
                for r in rows:
                    if r.get(inv_key) == invoice_number:
                        r[status_key] = "Paid"
                        break
                inv_mod._atomic_write_csv(csv_path, rows, file_headers)
        click.echo(f"  ✓  Invoice {invoice_number} marked Paid in zd DB and CSV ledger.")
    except Exception as e:
        click.echo(f"  ✓  Invoice {invoice_number} marked Paid in zd DB.")
        click.echo(f"  ⚠  Could not update CSV ledger: {e}")
    _worklog(f"- [zd paid] {date.today().isoformat()} | {invoice_number} | ${row['total']:,.2f} | status: Paid")


@cli.command("backfill")
def cmd_backfill():
    """Seed clients and historical sessions from SEED_CLIENTS / BACKFILL_SESSIONS.

    \b
    Populate SEED_CLIENTS and BACKFILL_SESSIONS at the top of zd.py
    with your own data, then run this once to load them into the DB.
    Safe to re-run — duplicate sessions are skipped automatically.

    \b
    Example:
      zd backfill
    """
    with get_conn() as conn:
        # Seed clients
        for slug, name, rate in SEED_CLIENTS:
            existing = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", (slug,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE clients SET name = ?, rate = ? WHERE slug = ?",
                    (name, rate, slug),
                )
                click.echo(f"  ↺  Updated client: {name} @ ${rate:.2f}/hr")
            else:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    (slug, name, rate),
                )
                click.echo(f"  ✓  Added client: {name} @ ${rate:.2f}/hr")

        # Seed sessions — skip any that already exist on same date+client+hours
        inserted = 0
        skipped = 0
        for slug, work_date, hours, notes in BACKFILL_SESSIONS:
            c = conn.execute("SELECT id FROM clients WHERE slug = ?", (slug,)).fetchone()
            if not c:
                continue
            exists = conn.execute(
                """SELECT id FROM sessions
                   WHERE client_id = ? AND work_date = ? AND hours = ? AND notes = ?""",
                (c["id"], work_date, hours, notes),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                (c["id"], work_date, hours, notes),
            )
            inserted += 1

    click.echo(f"\n  ✓  Backfill complete: {inserted} sessions inserted, {skipped} already present.")
    click.echo("  Run `zd status` to see unbilled totals.\n")


@cli.command("add-client")
@click.argument("slug")
@click.argument("name")
@click.argument("rate", type=float)
def cmd_add_client(slug, name, rate):
    """Add or update a client.

    \b
    SLUG is a short lowercase identifier used in all other commands.
    NAME is the full display name used on invoices (quote if it has spaces).
    RATE is the hourly billing rate in dollars.

    \b
    Examples:
      zd add-client acme "Acme Corp" 95.00
      zd add-client acme "Acme Corp" 110.00   # updates rate if slug exists
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM clients WHERE slug = ?", (slug.lower(),)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE clients SET name = ?, rate = ? WHERE slug = ?",
                (name, rate, slug.lower()),
            )
            click.echo(f"  ↺  Updated: {name} @ ${rate:.2f}/hr  (slug: {slug.lower()})")
            _worklog(f"- [zd client] {date.today().isoformat()} | {slug.lower()} | \"{name}\" | ${rate:.2f}/hr | updated")
        else:
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                (slug.lower(), name, rate),
            )
            click.echo(f"  ✓  Added: {name} @ ${rate:.2f}/hr  (slug: {slug.lower()})")
            _worklog(f"- [zd client] {date.today().isoformat()} | {slug.lower()} | \"{name}\" | ${rate:.2f}/hr | added")

    _sync_client_to_config(name)


@cli.command("completion")
@click.argument("shell", type=click.Choice(["zsh", "bash", "fish"]), default="zsh", required=False)
def cmd_completion(shell):
    """Print shell completion setup instructions.

    \b
    Examples:
      zd completion          # zsh instructions (default)
      zd completion bash
      zd completion fish
    """
    var = {"zsh": "_ZD_COMPLETE=zsh_source", "bash": "_ZD_COMPLETE=bash_source", "fish": "_ZD_COMPLETE=fish_source"}[shell]
    rc = {"zsh": "~/.zshrc", "bash": "~/.bash_profile", "fish": "~/.config/fish/config.fish"}[shell]
    eval_line = f'eval "$({var} zd)"'
    fish_line = f"{var} zd | source"
    line = fish_line if shell == "fish" else eval_line
    click.echo(f"\n  Add this line to {rc}:\n")
    click.echo(f"    {line}\n")
    click.echo(f"  Then restart your shell or run:  source {rc}\n")


if __name__ == "__main__":
    cli(prog_name="zd")
