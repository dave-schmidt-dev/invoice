"""zd commands for reconcile, status, paid, backfill and shell completion. zd-owned names are looked up on the zd module at call time."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import click
import zd


@zd.cli.command("reconcile")
@click.option(
    "--fix", is_flag=True,
    help="Repair DB-ahead-of-CSV drift (append missing rows, sync Paid status). Report-only without it.",
)
def cmd_reconcile(fix):
    """Compare the zd DB (authoritative) against the CSV ledger (a
    projection) and report — or with --fix, repair — the benign DB-ahead
    drift.

    \b
    Reports:
      - CSV rows missing entirely (appended with --fix)
      - DB status=Paid not yet reflected in the CSV (synced with --fix)
      - session-sum vs stored-total drift (report only, never auto-changed)
      - CSV-only orphans with no matching DB invoice (report only, NEVER
        imported into the DB)

    \b
    Examples:
      zd reconcile
      zd reconcile --fix
    """
    # reconcile never mutates the DB (it only ever writes the CSV), so
    # always open a readonly connection regardless of --fix.
    with zd.get_conn(readonly=True) as conn:
        result = zd._converge_db_to_csv(conn, apply=fix)

    if not result.ok:
        click.echo(f"  ⚠  {result.warning}")
        return

    verb = "Repaired" if fix else "Would repair"
    any_drift = result.appended or result.status_synced

    if not any_drift and not result.flagged:
        click.echo("  ✓  DB and CSV ledger are in sync. No drift found.")
        return

    if result.appended:
        click.echo(f"  {verb} {len(result.appended)} missing CSV row(s):")
        for number in result.appended:
            click.echo(f"    + {number}")
    if result.status_synced:
        click.echo(f"  {verb} {len(result.status_synced)} status sync(s) to Paid:")
        for number in result.status_synced:
            click.echo(f"    ~ {number}")
    if not fix and any_drift:
        click.echo("  Run with --fix to apply these repairs.")

    if result.total_drift:
        click.echo(f"\n  ⚠  {len(result.total_drift)} invoice(s) with session-sum vs stored-total drift (reported only, stored total NOT changed):")
        for line in result.total_drift:
            click.echo(f"    ! {line}")

    if result.orphans:
        click.echo(f"\n  ⚠  {len(result.orphans)} CSV-only orphan invoice number(s), reported, NOT imported:")
        for number in result.orphans:
            click.echo(f"    ? {number}")


@zd.cli.command("status")
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
    with zd.get_conn(readonly=True) as conn:
        zd._auto_converge(conn)

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
            labor = zd.to_money(total_hours * c["rate"])
            total = labor + zd.to_money(total_exp)
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
                due = zd._due_date_str(inv["invoice_date"])
                click.echo(
                    f"  {inv['invoice_number']:<14} {inv['name']:<22} ${inv['total']:>8,.2f}  due {due}"
                )
        click.echo()


@zd.cli.command("paid")
@click.argument("invoice_number")
@click.option(
    "--date", "paid_date_arg", default=None,
    help="ISO date (YYYY-MM-DD) the invoice was paid. Defaults to today.",
)
def cmd_paid(invoice_number, paid_date_arg):
    """Mark an invoice as paid in zd DB and invoice.py's CSV ledger.

    \b
    Updates status to Paid in both the zd SQLite database and the
    invoice.py CSV ledger so both stay in sync, and records the paid
    date (defaults to today) in the zd database.

    \b
    Example:
      zd paid 2026-0003 --date 2026-05-01
    """
    if paid_date_arg is None:
        paid_date = date.today().isoformat()
    else:
        try:
            paid_date = date.fromisoformat(paid_date_arg).isoformat()
        except ValueError:
            raise click.ClickException(
                f"Invalid --date value {paid_date_arg!r}; expected YYYY-MM-DD."
            )

    with zd.get_conn() as conn:
        zd._auto_converge(conn)
        row = conn.execute(
            "SELECT * FROM invoices WHERE invoice_number = ?", (invoice_number,)
        ).fetchone()
        if not row:
            raise click.ClickException(f"Invoice {invoice_number} not found in zd database.")
        if row["status"] == "Paid":
            click.echo(f"  Invoice {invoice_number} is already marked Paid.")
            return
        conn.execute(
            "UPDATE invoices SET status = 'Paid', paid_date = ? WHERE invoice_number = ?",
            (paid_date, invoice_number),
        )

    # Also update invoice.py's CSV ledger
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("invoice", zd.INVOICE_PY)
        inv_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inv_mod)
        config = inv_mod.load_config()
        csv_file = str(inv_mod._ledger_path_from_config(config))

        csv_path = Path(csv_file)
        if csv_path.exists():
            matched = False
            with inv_mod._file_lock(csv_path):
                rows, file_headers = inv_mod._read_csv_with_headers(csv_path)
                inv_key = inv_mod._csv_field_key(file_headers, "invoice_number") or "invoice_number"
                status_key = inv_mod._csv_field_key(file_headers, "status") or "status"
                for r in rows:
                    if r.get(inv_key) == invoice_number:
                        r[status_key] = "Paid"
                        matched = True
                        break
                if matched:
                    zd._backup_file(csv_path)
                    inv_mod._atomic_write_csv(csv_path, rows, file_headers)
            if matched:
                click.echo(f"  ✓  Invoice {invoice_number} marked Paid in zd DB and CSV ledger.")
            else:
                click.echo(f"  ✓  Invoice {invoice_number} marked Paid in zd DB.")
                click.echo(
                    f"  ⚠  No matching row for {invoice_number} found in the CSV "
                    "ledger; it was not updated."
                )
        else:
            click.echo(f"  ✓  Invoice {invoice_number} marked Paid in zd DB.")
            click.echo(f"  ⚠  CSV ledger not found at {csv_path}; it was not updated.")
    except Exception as e:
        click.echo(f"  ✓  Invoice {invoice_number} marked Paid in zd DB.")
        click.echo(f"  ⚠  Could not update CSV ledger: {e}")
    zd._worklog(f"- [zd paid] {date.today().isoformat()} | {invoice_number} | ${row['total']:,.2f} | status: Paid")


@zd.cli.command("backfill")
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
    with zd.get_conn() as conn:
        # Seed clients
        for slug, name, rate in zd.SEED_CLIENTS:
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
        for slug, work_date, hours, notes in zd.BACKFILL_SESSIONS:
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


@zd.cli.command("completion")
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
