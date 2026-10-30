"""DB-authoritative invoice rendering, reservation and projection outcomes.

The SQLite commit is the point of no return for both new and regenerated
invoices. Only a temporary PDF exists before it; projection failures after it
are returned explicitly, never mistaken for a completed ledger write.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

import click
import zd
from invoice_ledger import update_ledger_rows
from zd_billing import mark_billed


@dataclass(frozen=True)
class RenderedInvoice:
    """A rendered temporary PDF, not yet committed or published."""

    number: str
    invoice_date: str
    pdf_path: str
    temp_pdf: str
    total: object


@dataclass(frozen=True)
class WriteOutcome:
    """Projection status after the authoritative DB commit succeeded."""

    pdf_finalized: bool
    csv_updated: bool


def check_number_available(conn, inv_mod, csv_file, invoice_number):
    """Refuse an existing CSV or DB identity before rendering a new invoice."""
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



def discard_render(rendered):
    """Remove only this attempt's temporary PDF; cleanup must not mask failure."""
    try:
        os.remove(rendered.temp_pdf)
    except OSError:
        pass


def render_invoice(inv_mod, config, client_name, matched_client, number,
                   invoice_date, line_items, *, fixed_total=None, new=False):
    """Render once to a unique temporary path; never replace a final PDF here."""
    directory = inv_mod._invoices_dir_from_config(config)
    Path(directory).mkdir(parents=True, exist_ok=True)
    slug = inv_mod._sanitize_filename_component(client_name, "Client")
    safe_num = inv_mod._sanitize_filename_component(number, "invoice")
    pdf_path = str(Path(directory) / f"{slug}_Invoice_{safe_num}.pdf")
    if new and Path(pdf_path).exists():
        raise click.ClickException(
            f"A PDF already exists at {pdf_path}. Refusing to overwrite it; "
            "resolve the existing file before invoicing."
        )
    fd, temp_pdf = tempfile.mkstemp(prefix=Path(pdf_path).name + ".tmp-", dir=directory)
    os.close(fd)
    rendered = RenderedInvoice(number, invoice_date, pdf_path, temp_pdf, fixed_total)
    try:
        actual_total = inv_mod.generate_pdf(
            number, invoice_date, config, line_items, temp_pdf,
            client=matched_client, payment_terms="Net 30",
        )
        if fixed_total is not None:
            actual_total = fixed_total
        return RenderedInvoice(number, invoice_date, pdf_path, temp_pdf, actual_total)
    except BaseException:
        discard_render(rendered)
        raise


def reserve_invoice(conn, rendered, client, sessions, expenses, *, flat=False,
                    existing_id=None):
    """Commit the invoice identity and exact billed rows, or roll back the render.

    Regeneration updates only the existing invoice, retaining billed rates,
    status, date and row ownership. Neither path changes the final PDF before
    the DB accepts the transaction.
    """
    try:
        conn.execute("BEGIN")
        if existing_id is not None:
            cursor = conn.execute(
                "UPDATE invoices SET total = ?, pdf_path = ? WHERE id = ? "
                "AND invoice_number = ? AND client_id = ?",
                (float(rendered.total), rendered.pdf_path, existing_id,
                 rendered.number, client["id"]),
            )
            if cursor.rowcount != 1:
                raise click.ClickException("Invoice changed before regeneration; nothing was written.")
        else:
            cursor = conn.execute(
                """INSERT INTO invoices
                       (invoice_number, client_id, invoice_date, total, status, pdf_path, billing_mode)
                   VALUES (?,?,?,?,?,?,?)""",
                (rendered.number, client["id"], rendered.invoice_date,
                 float(rendered.total), "Sent", rendered.pdf_path,
                 "flat" if flat else "hourly"),
            )
            inv_id = cursor.lastrowid
            mark_billed(
                conn, "sessions", [s["id"] for s in sessions],
                "invoice_id = ?, billed_rate = ?", [inv_id, float(client["rate"])],
            )
            if not flat:
                mark_billed(
                    conn, "expenses", [e["id"] for e in expenses],
                    "invoice_id = ?", [inv_id],
                )
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        discard_render(rendered)
        raise


def write_projection(inv_mod, config, matched_client, line_items, rendered,
                     *, regenerate=False, old_path=None):
    """Project a committed invoice to PDF and CSV, reporting each result.

    CSV updates retain the shared locked/backup/atomic writer. An old PDF is
    retired only after an existing CSV row was successfully repointed.
    """
    try:
        os.replace(rendered.temp_pdf, rendered.pdf_path)
    except OSError as exc:
        discard_render(rendered)
        click.echo(
            "\n  ⚠  Invoice committed to the zd DB (authoritative), but the "
            f"PDF could not be finalized: {exc}"
        )
        click.echo(
            "     The DB record is safe and the CSV row was NOT written. "
            "Regenerate from the DB to repair."
        )
        return WriteOutcome(False, False)

    csv_updated = False
    try:
        if regenerate:
            csv_path = inv_mod._ledger_path_from_config(config)
            if Path(csv_path).exists():
                matched = update_ledger_rows(
                    csv_path,
                    {rendered.number: {"total": f"{float(rendered.total):.2f}",
                                       "pdf_file": rendered.pdf_path}},
                    backup=zd._backup_file, write_unmatched=True,
                )
                if rendered.number not in matched:
                    raise ValueError(f"no ledger row for {rendered.number}")
                csv_updated = True
            else:
                raise ValueError("ledger file is missing")
        else:
            inv_mod.save_to_csv(
                rendered.number, rendered.invoice_date, config, line_items,
                rendered.total, rendered.pdf_path, client=matched_client, status="Sent",
            )
            csv_updated = True
    except Exception as exc:
        if regenerate:
            click.echo(
                f"  ⚠  Could not update the CSV ledger row for {rendered.number}: {exc}",
                err=True,
            )
        else:
            click.echo(
                "\n  ⚠  Invoice committed to the zd DB (authoritative), but the "
                f"CSV ledger row could not be written: {exc}"
            )
            click.echo("     The DB record is safe; the CSV projection may need repair.")

    if (regenerate and csv_updated and old_path and old_path != rendered.pdf_path
            and Path(old_path).exists()):
        try:
            Path(old_path).unlink()
        except OSError as exc:
            click.echo(f"  ⚠  Could not remove the old PDF ({old_path}): {exc}", err=True)
    return WriteOutcome(True, csv_updated)
