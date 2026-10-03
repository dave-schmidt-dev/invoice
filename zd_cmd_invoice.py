"""The zd invoice command. zd-owned names are looked up on the zd module at call time."""

import logging
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import click
import zd
from invoice_ledger import update_ledger_rows


@zd.cli.command("invoice")
@click.argument("client", shell_complete=zd._complete_client)
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
@click.option(
    "--flat",
    "flat_amount",
    type=str,
    default=None,
    metavar="AMOUNT",
    help=(
        "Bill a single fixed AMOUNT (e.g. 500 or 1500.00) instead of "
        "hours×rate. Requires --description. The invoice total is exactly "
        "AMOUNT regardless of logged hours. Scoped unbilled SESSIONS are "
        "marked billed; reimbursable EXPENSES are left UNBILLED for a normal "
        "invoice (CR-9). Cannot be combined with --summarize-weeks."
    ),
)
@click.option(
    "--description",
    "flat_description",
    default=None,
    help="Line-item description for a --flat invoice (required with --flat).",
)
@click.option("--regenerate", default=None, help="Regenerate PDF for an existing invoice number")
def cmd_invoice(client, invoice_date, invoice_month, summarize_weeks, flat_amount, flat_description, regenerate):
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
      zd invoice acme --flat 1500 --description "Fixed-scope engagement"
      zd invoice acme --regenerate 2026-0002
    """
    month_start = None
    month_end = None
    if invoice_month is not None:
        month_start, month_end = zd._month_bounds(invoice_month)

    # ---- Flat-fee invoice option validation (E.1) ----
    # Validate flags up front, before any DB/PDF work, so a bad combination
    # fails fast with a clear message and no side effects.
    flat_mode = flat_amount is not None
    flat_total = None
    if flat_mode:
        if summarize_weeks:
            raise click.ClickException(
                "--flat cannot be combined with --summarize-weeks "
                "(a flat invoice has no weekly line items to summarize)."
            )
        if not flat_description or not flat_description.strip():
            raise click.ClickException("--flat requires --description \"...\".")
        # Money via Decimal; reject non-finite and non-positive amounts (INV-4).
        try:
            parsed = Decimal(str(flat_amount))
        except (InvalidOperation, ValueError):
            raise click.ClickException(
                f"--flat amount must be a number, got '{flat_amount}'."
            )
        if not parsed.is_finite():
            raise click.ClickException("--flat amount must be finite (not inf/nan).")
        # A finite Decimal can still exceed the default 28-digit context and
        # blow up quantize() with InvalidOperation (e.g. 1e999, or a fat-fingered
        # 40-digit paste). Catch it here so it fails as a clean ClickException
        # rather than a raw traceback — no real invoice is anywhere near this.
        try:
            flat_total = zd.to_money(parsed)
        except InvalidOperation:
            raise click.ClickException(
                f"--flat amount is too large to bill: '{flat_amount}'."
            )
        if flat_total <= 0:
            raise click.ClickException("--flat amount must be greater than 0.")

    # --- Load invoice.py config and machinery ---
    try:
        inv_mod = zd._load_invoice()
    except Exception as e:
        raise click.ClickException(f"Could not load invoice.py: {e}")

    config = inv_mod.load_config()
    summary_settings = zd._weekly_summary_config(config)
    effective_summarize_weeks = bool(summarize_weeks or summary_settings["enabled"])

    # Single choke point for the non-loopback warning (INV-1): this fires at
    # most once per `zd invoice` run, right where the run commits to actually
    # using summaries — before branching into the regenerate/new-invoice
    # paths (each of which has its own summarize call site further down).
    # flat_mode never reaches either summarize call site (a flat invoice
    # renders a single fixed line item), so exclude it here too — otherwise a
    # config-enabled-but-unused summarizer would warn for a run that never
    # talks to it.
    if effective_summarize_weeks and not flat_mode:
        host, _port = zd._parse_host_port(summary_settings["base_url"])
        if not zd._is_loopback_host(host):
            click.echo(
                f"  ⚠  Weekly summaries are configured to use a non-local endpoint "
                f"({summary_settings['base_url']}). Client session notes will be "
                "sent off this machine to generate summaries."
            )

    def _summary_func(label, week_sessions):
        return zd.summarize_week_with_local_gemma(
            label,
            week_sessions,
            base_url=summary_settings["base_url"],
            model=summary_settings["model"],
            timeout=summary_settings["timeout_seconds"],
        )

    with zd.get_conn() as conn:
        c = zd.get_client(conn, client)
        zd._auto_converge(conn)

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

            # INV-3: price the regenerated line items at the rate that was
            # BILLED, not the client's current rate. billed_rate is snapshotted
            # at billing time (A.3); COALESCE falls back to the live client rate
            # only for legacy rows that predate the snapshot column.
            sessions = conn.execute(
                """SELECT s.*, COALESCE(s.billed_rate, cl.rate) AS rate FROM sessions s
                   JOIN clients cl ON cl.id = s.client_id
                   WHERE s.invoice_id = ?
                   ORDER BY s.work_date""",
                (inv_id,),
            ).fetchall()

            # Never silently substitute the current rate: if any billed session
            # predates the billed_rate snapshot (legacy NULL), warn prominently
            # that its historical rate is unverified and the current client rate
            # is standing in for it.
            legacy_sessions = [s for s in sessions if s["billed_rate"] is None]
            if legacy_sessions:
                click.echo(
                    f"\n  ⚠  WARNING: {len(legacy_sessions)} of {len(sessions)} billed "
                    f"sessions on invoice {invoice_number} have no snapshotted "
                    "billed_rate (billed before rate-locking).",
                    err=True,
                )
                click.echo(
                    f"     Their historical rate is UNVERIFIED; the current client "
                    f"rate (${c['rate']:.2f}/hr) is being used for those rows. The "
                    "regenerated total may not match what was originally billed.",
                    err=True,
                )

            expenses = conn.execute(
                "SELECT * FROM expenses WHERE invoice_id = ?",
                (inv_id,),
            ).fetchall()

            billing_mode = inv_row["billing_mode"] if "billing_mode" in inv_row.keys() else None

            if billing_mode == "flat":
                # E.1: a flat invoice bills one fixed amount, not hours×rate.
                # Rebuild it as a SINGLE line item summing to the stored total
                # rather than the (hourly) weekly grouping, so the regenerated
                # PDF shows one flat line — never hourly rows that don't add up
                # to the flat total. The stored total remains authoritative
                # (INV-3). NOTE: the original --description is not persisted (no
                # DB column), so a generic "Flat fee" label stands in on
                # regenerate; only the TOTAL is guaranteed preserved.
                stored_total = zd.to_money(str(inv_row["total"]))
                line_items = [{
                    "description": "Flat fee",
                    "hours": 0,
                    "rate": 0,
                    "amount": float(stored_total),
                }]
            else:
                summary_func = _summary_func if effective_summarize_weeks else None
                if effective_summarize_weeks:
                    try:
                        with zd._summary_server_context(summary_settings):
                            line_items = zd.group_sessions_by_week(sessions, summary_provider=summary_func)
                    except (zd.SummaryServerError, OSError) as exc:
                        # OSError covers the spawn path too: mkdir/open/Popen can
                        # raise FileNotFoundError (llama-server gone) / PermissionError.
                        logging.getLogger("zd").warning("summary server unavailable: %s", exc)
                        click.echo(
                            f"  ⚠  Summary server unavailable ({exc}); using plain week labels."
                        )
                        line_items = zd.group_sessions_by_week(sessions, summary_provider=None)
                else:
                    line_items = zd.group_sessions_by_week(sessions, summary_provider=summary_func)
                for e in expenses:
                    line_items.append({
                        "description": f"Expense: {e['description']}",
                        "hours": 0,
                        "rate": 0,
                        "amount": float(zd.to_money(e["amount"])),
                    })

            # Confirmation total derives from the SAME line_items handed to
            # generate_pdf, quantized the way the new-invoice path does it, so
            # confirmation == persisted (INV-4). For a FLAT invoice the total is
            # immutable: reuse the stored total rather than recomputing from
            # hours*rate.
            total_hours = sum(s["hours"] for s in sessions)
            if billing_mode == "flat":
                # Flat invoices bill a fixed amount, not hours*rate. The stored
                # invoice total is authoritative; reproduce it exactly.
                total = zd.to_money(str(inv_row["total"]))
            else:
                total_labor = sum(
                    (zd.to_money(str(li["amount"])) for li in line_items
                     if li.get("hours") or li.get("rate")),
                    Decimal("0.00"),
                )
                total_exp = sum(
                    (zd.to_money(str(li["amount"])) for li in line_items
                     if not li.get("hours") and not li.get("rate")),
                    Decimal("0.00"),
                )
                total = zd.to_money(sum(
                    (zd.to_money(str(li["amount"])) for li in line_items),
                    Decimal("0.00"),
                ))

            click.echo(f"\n  Regenerating invoice {invoice_number} for {c['name']}")
            click.echo(f"  {len(sessions)} sessions → {len(line_items)} weekly line items")
            if billing_mode == "flat":
                click.echo(f"  Flat invoice — reusing stored total ${total:,.2f}")
            else:
                click.echo(f"  {total_hours:.1f} hours = ${total_labor:,.2f}")
                if total_exp:
                    click.echo(f"  Expenses: ${total_exp:,.2f}")
            click.echo(f"  Total: ${total:,.2f}")

            if not click.confirm("\n  Proceed?"):
                click.echo("  Cancelled.")
                return

            # ------------------------------------------------------------------
            # Durable write ordering (INV-2 / INV-6, mirrors the new-invoice
            # path). The customer-facing PDF is placed atomically BEFORE the
            # ledgers are updated, and no delete can crash after the ledgers
            # change:
            #   1. Render to a TEMP PDF (never the final path).
            #   2. os.replace -> final PDF (atomic).
            #   3. UPDATE invoices + explicit conn.commit() (point of no return).
            #   4. Patch the CSV ledger row atomically, backing it up first.
            #   5. Delete the OLD PDF only if the path changed, guarded so a
            #      delete failure can never crash after the ledgers are updated.
            # ------------------------------------------------------------------
            invoices_dir = str(inv_mod._invoices_dir_from_config(config))
            Path(invoices_dir).mkdir(parents=True, exist_ok=True)
            client_slug = inv_mod._sanitize_filename_component(c["name"], "Client")
            safe_num = inv_mod._sanitize_filename_component(invoice_number, "invoice")
            pdf_filename = f"{client_slug}_Invoice_{safe_num}.pdf"
            pdf_path = str(Path(invoices_dir) / pdf_filename)
            temp_pdf = f"{pdf_path}.tmp-{os.getpid()}"

            # Step 1 — render to the temp path.
            committed = False
            try:
                actual_total = inv_mod.generate_pdf(
                    invoice_number, invoice_date, config, line_items, temp_pdf,
                    client=matched_client, payment_terms="Net 30",
                )

                # For a flat invoice the persisted total is immutable: keep the
                # stored figure regardless of what generate_pdf computed from the
                # (hours-priced) line items.
                if billing_mode == "flat":
                    actual_total = total

                # Step 2 — promote the temp PDF to its final path atomically
                # BEFORE any ledger mutation.
                os.replace(temp_pdf, pdf_path)

                # Step 3 — explicit transaction for the DB ledger. Do not rely on
                # the implicit block-exit commit; make the point of no return
                # explicit and ordered after the PDF is in place.
                conn.execute(
                    "UPDATE invoices SET total = ?, pdf_path = ? WHERE id = ?",
                    (float(actual_total), pdf_path, inv_id),
                )
                conn.commit()
                committed = True
            except BaseException:
                # Failure BEFORE the commit: clean up the temp PDF (guarded) and
                # re-raise. Nothing durable to the ledgers has changed yet.
                if not committed:
                    try:
                        os.remove(temp_pdf)
                    except OSError:
                        pass
                raise

            # Step 4 — patch the CSV ledger row atomically. Back up the ledger
            # first (INV-6) so the pre-edit state is recoverable.
            csv_file = str(inv_mod._ledger_path_from_config(config))
            csv_path = Path(csv_file)
            if csv_path.exists():
                try:
                    update_ledger_rows(
                        csv_path,
                        {invoice_number: {"total": f"{float(actual_total):.2f}", "pdf_file": pdf_path}},
                        backup=zd._backup_file,
                        write_unmatched=True,
                    )
                except Exception as e:
                    click.echo(f"  ⚠  Could not update CSV ledger: {e}")

            # Step 5 — remove the OLD PDF only if the path changed. A delete
            # failure here must NEVER crash: the ledgers are already updated, so
            # warn and continue (a stale extra PDF is harmless).
            old_path = inv_row["pdf_path"]
            if old_path and old_path != pdf_path and Path(old_path).exists():
                try:
                    Path(old_path).unlink()
                except OSError as e:
                    click.echo(
                        f"  ⚠  Could not remove the old PDF ({old_path}): {e}",
                        err=True,
                    )

            click.echo(f"\n  ✓  Invoice {invoice_number} regenerated: {pdf_path}")
            click.echo(f"  ✓  Total: ${actual_total:,.2f}")
            zd._worklog(f"- [zd invoice] {invoice_date} | {invoice_number} | {c['name']} | ${actual_total:,.2f} | regenerated")
            return

        # ---- New invoice ----
        if invoice_date is None:
            invoice_date = date.today().isoformat()
        else:
            # Validate up front (as cmd_log/cmd_expense do): the numbering step
            # below parses this via date.fromisoformat, so a malformed --date
            # must fail as a clean ClickException, never a raw ValueError.
            try:
                date.fromisoformat(invoice_date)
            except ValueError:
                raise click.ClickException("Date must be YYYY-MM-DD format.")

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
        # Flat invoices do NOT consume expenses (CR-9): reimbursable expenses
        # stay unbilled so they can go on a normal invoice. Only query them for
        # the hourly path.
        expenses = (
            [] if flat_mode
            else conn.execute(expense_query, expense_params).fetchall()
        )

        # A flat invoice is authoritative on its AMOUNT and may be issued even
        # with no logged sessions in scope; only the hourly path requires
        # something to bill.
        if not flat_mode and not sessions and not expenses:
            scope = f" in {invoice_month}" if invoice_month else ""
            click.echo(f"  No unbilled sessions or expenses for {c['name']}{scope}.")
            return

        if flat_mode:
            # E.1: one fixed line item. hours/rate are 0 so save_to_csv omits
            # the "(0 hrs @ $0.00/hr)" suffix (A.2) and the description renders
            # cleanly. The invoice total is exactly the --flat amount regardless
            # of logged hours.
            line_items = [{
                "description": flat_description,
                "hours": 0,
                "rate": 0,
                "amount": float(flat_total),
            }]
        else:
            # Build line items grouped by week. When summarization is enabled,
            # ensure the local llama-server is up for the entire grouping pass
            # — _summary_server_context spawns it cold if needed and tears it
            # down when we exit, so no orphan server lingers.
            summary_func = _summary_func if effective_summarize_weeks else None
            if effective_summarize_weeks:
                click.echo("  Summarizing weekly line items with local Gemma model...")
                try:
                    with zd._summary_server_context(summary_settings):
                        line_items = zd.group_sessions_by_week(sessions, summary_provider=summary_func)
                except (zd.SummaryServerError, OSError) as exc:
                    # OSError covers the spawn path too: mkdir/open/Popen can
                    # raise FileNotFoundError (llama-server gone) / PermissionError.
                    logging.getLogger("zd").warning("summary server unavailable: %s", exc)
                    click.echo(
                        f"  ⚠  Summary server unavailable ({exc}); using plain week labels."
                    )
                    line_items = zd.group_sessions_by_week(sessions, summary_provider=None)
            else:
                line_items = zd.group_sessions_by_week(sessions, summary_provider=summary_func)

            # Add expense line items if any
            for e in expenses:
                line_items.append({
                    "description": f"Expense: {e['description']}",
                    "hours": 0,
                    "rate": 0,
                    "amount": float(zd.to_money(e["amount"])),
                })

        # Get next invoice number — derive the YEAR from the invoice's own
        # date (not "today") so a backdated --date gets a number in ITS
        # year, and compute the next suffix as an INTEGER maximum across
        # both the CSV ledger and the zd DB (never a lexical/string max,
        # which breaks once a year passes 9999 invoices).
        csv_file = str(inv_mod._ledger_path_from_config(config))
        year = date.fromisoformat(invoice_date).year
        year_prefix = f"{year}-"

        max_suffix = 0
        csv_path_for_numbering = Path(csv_file)
        if csv_path_for_numbering.exists():
            with inv_mod._file_lock(csv_path_for_numbering):
                numbering_rows, numbering_headers = inv_mod._read_csv_with_headers(
                    csv_path_for_numbering
                )
            numbering_inv_key = (
                inv_mod._csv_field_key(numbering_headers, "invoice_number")
                or "invoice_number"
            )
            for r in numbering_rows:
                num_str = str(r.get(numbering_inv_key, ""))
                if not num_str.startswith(year_prefix):
                    continue
                try:
                    suffix = int(num_str.split("-", 1)[1])
                except (ValueError, IndexError):
                    continue
                max_suffix = max(max_suffix, suffix)

        db_rows = conn.execute(
            "SELECT invoice_number FROM invoices WHERE invoice_number LIKE ?",
            (f"{year_prefix}%",),
        ).fetchall()
        for row in db_rows:
            num_str = row["invoice_number"] or ""
            try:
                suffix = int(num_str.split("-", 1)[1])
            except (ValueError, IndexError):
                continue
            max_suffix = max(max_suffix, suffix)

        invoice_number = f"{year}-{max_suffix + 1:04d}"
        click.echo(f"\n  Generating invoice {invoice_number} for {c['name']}")
        if invoice_month:
            click.echo(f"  Month: {invoice_month}")

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
        total = zd.to_money(sum(
            (zd.to_money(str(li["amount"])) for li in line_items),
            Decimal("0.00"),
        ))

        if flat_mode:
            # Flat invoices get their OWN echo: a single fixed line, not the
            # hourly labor/expense split (a 0-hours/0-rate item would otherwise
            # fall into the expense display bucket). The total is exactly the
            # --flat amount regardless of the logged hours below it.
            click.echo(f"  Flat fee: {flat_description}")
            click.echo(f"  {len(sessions)} sessions marked billed "
                       f"({total_hours:.1f} logged hours, not priced)")
            click.echo(f"  Total: ${total:,.2f}")
        else:
            click.echo(f"  {len(sessions)} sessions → {len(line_items)} weekly line items")
            # Labor = per-week line items (hours or rate set); expenses = the rest.
            total_labor = sum(
                (zd.to_money(str(li["amount"])) for li in line_items
                 if li.get("hours") or li.get("rate")),
                Decimal("0.00"),
            )
            total_exp = sum(
                (zd.to_money(str(li["amount"])) for li in line_items
                 if not li.get("hours") and not li.get("rate")),
                Decimal("0.00"),
            )
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

            # For a flat invoice the persisted total is immutable and equals the
            # --flat AMOUNT, regardless of what generate_pdf computed from the
            # line items (INV-4). Pin it here.
            if flat_mode:
                actual_total = flat_total

            billing_mode = "flat" if flat_mode else "hourly"

            # Step 3 — Explicit transaction. After this commit the DB is
            # authoritative: the invoice exists and its sessions/expenses are
            # billed. Snapshot the client's current rate into
            # sessions.billed_rate so the billed amount is immutable against
            # later rate changes (INV-3). For a flat invoice the rate snapshot
            # is not used for pricing (the flat total is authoritative) but is
            # still recorded for consistency.
            conn.execute("BEGIN")
            conn.execute(
                """INSERT INTO invoices
                       (invoice_number, client_id, invoice_date, total, status, pdf_path, billing_mode)
                   VALUES (?,?,?,?,?,?,?)""",
                (invoice_number, c["id"], invoice_date, float(actual_total),
                 "Sent", pdf_path, billing_mode),
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
            # CR-9: a flat invoice does NOT consume expenses — leave all
            # reimbursable expenses unbilled so they can go on a normal invoice.
            if not flat_mode:
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
    zd._worklog(f"- [zd invoice] {invoice_date} | {invoice_number} | {c['name']} | ${actual_total:,.2f} | {len(sessions)} sessions, {sum(s['hours'] for s in sessions):.1f}h | status: Sent")
