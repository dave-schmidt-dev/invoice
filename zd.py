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
import logging
import shutil  # noqa: F401 - tests patch zd.shutil.which
import sqlite3
import sys
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import click
from click.shell_completion import CompletionItem
from cli_logging import configure_file_logger
from zd_store import (  # noqa: E402,F401 - moved to zd_store.py
    _MAX_BACKUPS, _backed_up_this_run, _backup_file, _backup_db, _SCHEMA_VERSION,
    _MIGRATIONS, _column_exists, to_money, get_client, week_label, week_key,
    BACKFILL_SESSIONS, SEED_CLIENTS, _due_date_str, _month_bounds,
)
from zd_summary import (  # noqa: E402,F401 - moved to zd_summary.py
    LOCAL_SUMMARY_STARTUP_TIMEOUT, WeekSummaryError, SummaryServerError,
    _clean_week_summary, _weekly_summary_config, _parse_host_port, _is_loopback_host,
    summarize_week_with_local_gemma, group_sessions_by_week,
)
from zd_reconcile import (  # noqa: E402,F401 - moved to zd_reconcile.py
    _converge_db_to_csv, _load_invoice,
)

LOG_FILE = os.environ.get("ZD_LOG_FILE", "/tmp/zd.log")


def _setup_logging(debug: bool):
    """Configure the named "zd" logger (see cli_logging.configure_file_logger).

    LOG_FILE is read at call time so tests can patch it.
    """
    return configure_file_logger("zd", LOG_FILE, debug)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ZD_DB = Path.home() / ".zd.db"


CONFIG_FILE = Path.home() / ".invoice_config.json"


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


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
        # Back up before touching schema, via the online-backup API (conn is
        # already open) so a live WAL-mode DB is never copied mid-write.
        # _backup_db is a no-op if the file doesn't exist yet (fresh
        # in-memory create), has no tables yet, or was already backed up.
        _backup_db(conn, ZD_DB)
        for _table, alter in missing:
            conn.execute(alter)

    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def get_conn(readonly=False):
    """Open a connection to ZD_DB, set standard PRAGMAs, and (unless
    readonly) snapshot the pre-write state via the online-backup API.

    The connection is opened FIRST because the online-backup API needs an
    open source connection; the backup still captures the DB's state before
    the caller's own writes land, since it runs before this function returns.
    readonly=True skips the backup entirely (H.3) — read-only commands and
    tab-completion have nothing to protect against and were otherwise
    hollowing out the _MAX_BACKUPS retention window on every invocation.
    """
    conn = sqlite3.connect(ZD_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        _backup_db(conn, ZD_DB)
    return conn


def init_db():
    with get_conn(readonly=True) as conn:
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


def _complete_client(ctx, param, incomplete):
    try:
        conn = get_conn(readonly=True)
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

    FAIL-CLOSED (INV-6): CONFIG_FILE holds the payee, payment/banking, and
    every client profile — not just the one this call cares about. If the
    file EXISTS but doesn't parse as JSON, we must NOT treat that as "start
    from empty", because writing back would silently wipe everything else in
    the file down to just this one client. So a corrupt-but-present file
    raises a clean ClickException and is left byte-for-byte untouched; only a
    genuinely ABSENT file starts from {}.

    Also locked and atomic, sharing invoice.py's `_file_lock`/
    `_atomic_write_json` so this writer and invoice.py's `save_config` never
    race each other on the same file (`_file_lock` derives its lock path from
    a hash of the target path, so both callers land on the same lock).
    """
    import json
    inv_mod = _load_invoice()

    with inv_mod._file_lock(CONFIG_FILE):
        if CONFIG_FILE.exists():
            try:
                config = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
            except Exception as e:
                logging.getLogger("zd").warning(
                    "config file present but unparseable; refusing to overwrite"
                )
                raise click.ClickException(
                    f"Config file '{CONFIG_FILE}' is not valid JSON ({e}); left "
                    "untouched. Fix or restore it before adding a client."
                )
        else:
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
        inv_mod._atomic_write_json(CONFIG_FILE, config)


def _server_alive(base_url, timeout=2.0):
    """Return True if base_url responds 200 on /health."""
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


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
    # Owner-only (0600): this log captures llama-server's stdout/stderr, which
    # can include client session notes sent for summarization (INV-1). Create
    # via os.open so the mode applies at creation time (bypassing umask), then
    # best-effort chmod in case the file already existed with looser perms
    # from before this hardening (a chmod failure here must not crash spawn).
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    log_file = os.fdopen(fd, "ab", buffering=0)
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option("--debug", is_flag=True, help="Write DEBUG-level logs to <LOG_FILE> (default: WARNING+).")
@click.pass_context
def cli(ctx, debug):
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
      zd edit <id> --notes "corrected note"           # works even if already billed

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
    _setup_logging(debug)
    logging.getLogger("zd").debug("cli invoked: %s", ctx.invoked_subcommand)
    if ctx.resilient_parsing or any(arg in ("--help", "-h") for arg in sys.argv[1:]):
        return
    init_db()


def _auto_converge(conn):
    """Opportunistic, silent-when-clean convergence call for ordinary
    commands (cmd_invoice/cmd_status/cmd_paid). Never raises — any
    unexpected failure inside _converge_db_to_csv is already degraded to a
    no-op result, but this wrapper is defense-in-depth so a host command can
    never be crashed by reconcile logic."""
    try:
        result = _converge_db_to_csv(conn, apply=True, report=False)
    except Exception:
        return
    if result.changed:
        n = len(set(result.appended) | set(result.status_synced))
        click.echo(f"  ↻ Synced {n} invoice(s) from the DB to the CSV ledger.")
    # Surface a PARTIAL apply failure (a row we started to repair but couldn't):
    # the operator should know convergence didn't fully heal. A benign
    # couldn't-even-load degrade (ok=False) stays quiet here — `zd reconcile`
    # surfaces that if run explicitly.
    if result.ok and result.warning:
        click.echo(f"  ⚠  {result.warning}")


# Command modules below `import zd` and look zd-owned names up at call
# time; a script run registers this module as `zd` so they share it.
sys.modules.setdefault("zd", sys.modules[__name__])
from zd_cmd_records import (  # noqa: E402,F401 - registers commands on cli
    cmd_clients, cmd_log, cmd_expense, cmd_sessions, cmd_edit, cmd_edit_expense,
    cmd_add_client,
)


from zd_cmd_ledger import (  # noqa: E402,F401 - registers commands on cli
    cmd_reconcile, cmd_status, cmd_paid, cmd_backfill, cmd_completion,
)


from zd_cmd_invoice import (  # noqa: E402,F401 - registers commands on cli
    cmd_invoice,
)


if __name__ == "__main__":
    cli(prog_name="zd")
