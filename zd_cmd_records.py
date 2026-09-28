"""zd commands for clients, sessions and expenses. zd-owned names are looked up on the zd module at call time so tests that patch zd attributes keep working."""

import math
from datetime import date
from decimal import Decimal
import click
import zd


@zd.cli.command("clients")
def cmd_clients():
    """List all clients and their rates."""
    with zd.get_conn(readonly=True) as conn:
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


@zd.cli.command("log")
@click.argument("client", shell_complete=zd._complete_client)
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

    if not math.isfinite(hours) or hours <= 0:
        raise click.ClickException("Hours must be greater than 0.")

    with zd.get_conn() as conn:
        c = zd.get_client(conn, client)
        conn.execute(
            "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
            (c["id"], work_date, hours, notes),
        )

    amount = zd.to_money(hours * c["rate"])
    click.echo(f"  ✓  {work_date}  {c['name']}  {hours}h @ ${c['rate']:.2f}/hr = ${amount:,.2f}")
    zd._worklog(f"- [zd log] {work_date} | {c['slug']} | {hours}h @ ${c['rate']:.2f}/hr = ${amount:,.2f} | \"{notes}\"")


@zd.cli.command("expense")
@click.argument("client", shell_complete=zd._complete_client)
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

    if not math.isfinite(amount) or amount <= 0:
        raise click.ClickException("Amount must be greater than 0.")

    with zd.get_conn() as conn:
        c = zd.get_client(conn, client)
        conn.execute(
            "INSERT INTO expenses (client_id, expense_date, amount, description) VALUES (?,?,?,?)",
            (c["id"], expense_date, amount, description),
        )

    click.echo(f"  ✓  {expense_date}  {c['name']}  ${amount:,.2f}  {description}")
    zd._worklog(f"- [zd expense] {expense_date} | {c['slug']} | ${amount:,.2f} | \"{description}\"")


@zd.cli.command("sessions")
@click.argument("client", required=False, default=None, shell_complete=zd._complete_client)
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
    with zd.get_conn(readonly=True) as conn:
        if client:
            clients = [zd.get_client(conn, client)]
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
                amt = zd.to_money(r["hours"] * r["rate"])
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


@zd.cli.command("edit")
@click.argument("session_id", type=int)
@click.option("--date", "work_date", default=None, help="New date YYYY-MM-DD")
@click.option("--hours", type=float, default=None, help="New hours value")
@click.option("--notes", default=None, help="New notes text")
@click.option(
    "--force", is_flag=True,
    help="Retained for backward compatibility; has no effect on billed sessions "
         "(their hours/date are always locked per INV-3).",
)
def cmd_edit(session_id, work_date, hours, notes, force):
    """Edit an existing session.

    \b
    Use `zd sessions` to find the session ID, then update
    any combination of date, hours, and notes.

    \b
    Billed sessions (attached to an invoice) have their hours/date locked
    to keep that invoice's total correct (INV-3); only --notes may be
    edited on a billed session, with or without --force.

    \b
    Examples:
      zd edit 14 --date 2026-03-20
      zd edit 14 --hours 2.0 --notes "updated description"
      zd edit 14 --notes "corrected note"      # works even if billed
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

    if hours is not None and (not math.isfinite(hours) or hours <= 0):
        raise click.ClickException("Hours must be greater than 0.")

    with zd.get_conn() as conn:
        row = conn.execute(
            """SELECT s.*, c.slug, c.name, c.rate, i.invoice_number AS invoice_number
               FROM sessions s JOIN clients c ON c.id = s.client_id
               LEFT JOIN invoices i ON i.id = s.invoice_id
               WHERE s.id = ?""",
            (session_id,),
        ).fetchone()
        if not row:
            raise click.ClickException(f"Session {session_id} not found.")

        if row["invoice_id"] is not None:
            if hours is not None or work_date is not None:
                raise click.ClickException(
                    f"Session {session_id} is billed on invoice {row['invoice_number']}. "
                    "Its hours/date are locked to keep that invoice's total correct "
                    "(INV-3). Only --notes can be edited on a billed session."
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
    zd._worklog(f"- [zd edit] session {session_id} | {row['slug']} | {'; '.join(changes)}")


@zd.cli.command("edit-expense")
@click.argument("expense_id", type=int)
@click.option("--date", "expense_date", default=None, help="New date YYYY-MM-DD")
@click.option("--amount", type=float, default=None, help="New amount")
@click.option("--description", default=None, help="New description")
@click.option(
    "--force", is_flag=True,
    help="Retained for backward compatibility; has no effect on billed expenses "
         "(their amount/date are always locked per INV-3).",
)
def cmd_edit_expense(expense_id, expense_date, amount, description, force):
    """Edit an existing expense.

    \b
    Billed expenses (attached to an invoice) have their amount/date locked
    to keep that invoice's total correct (INV-3); only --description may be
    edited on a billed expense, with or without --force.

    \b
    Examples:
      zd edit-expense 3 --amount 50.00
      zd edit-expense 3 --date 2026-03-20 --description "updated"
      zd edit-expense 3 --description "fixed"    # works even if billed
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

    if amount is not None and (not math.isfinite(amount) or amount <= 0):
        raise click.ClickException("Amount must be greater than 0.")

    with zd.get_conn() as conn:
        row = conn.execute(
            """SELECT e.*, c.slug, c.name, i.invoice_number AS invoice_number
               FROM expenses e JOIN clients c ON c.id = e.client_id
               LEFT JOIN invoices i ON i.id = e.invoice_id
               WHERE e.id = ?""",
            (expense_id,),
        ).fetchone()
        if not row:
            raise click.ClickException(f"Expense {expense_id} not found.")

        if row["invoice_id"] is not None:
            if amount is not None or expense_date is not None:
                raise click.ClickException(
                    f"Expense {expense_id} is billed on invoice {row['invoice_number']}. "
                    "Its amount/date are locked to keep that invoice's total correct "
                    "(INV-3). Only --description can be edited on a billed expense."
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
    zd._worklog(f"- [zd edit-expense] expense {expense_id} | {row['slug']} | {'; '.join(changes)}")


@zd.cli.command("add-client")
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
    with zd.get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM clients WHERE slug = ?", (slug.lower(),)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE clients SET name = ?, rate = ? WHERE slug = ?",
                (name, rate, slug.lower()),
            )
            click.echo(f"  ↺  Updated: {name} @ ${rate:.2f}/hr  (slug: {slug.lower()})")
            zd._worklog(f"- [zd client] {date.today().isoformat()} | {slug.lower()} | \"{name}\" | ${rate:.2f}/hr | updated")
        else:
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                (slug.lower(), name, rate),
            )
            click.echo(f"  ✓  Added: {name} @ ${rate:.2f}/hr  (slug: {slug.lower()})")
            zd._worklog(f"- [zd client] {date.today().isoformat()} | {slug.lower()} | \"{name}\" | ${rate:.2f}/hr | added")

    zd._sync_client_to_config(name)
