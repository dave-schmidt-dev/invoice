"""Row-level billing helpers shared by the zd invoice command."""

from decimal import Decimal, InvalidOperation

import click


def mark_billed(conn, table, ids, set_clause, set_params):
    """Set ``set_clause`` on exactly ``ids`` that are still unbilled.

    Raises ClickException if any id was billed (or deleted) since it was
    priced, so the surrounding transaction rolls back instead of committing an
    invoice whose total does not match the rows it claims.
    """
    if not ids:
        return
    if table not in ("sessions", "expenses"):
        raise ValueError(f"unexpected table {table!r}")
    # Chunked to stay under SQLite's bound-parameter limit.
    updated = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE {table} SET {set_clause} "
            f"WHERE id IN ({marks}) AND invoice_id IS NULL",
            [*set_params, *chunk],
        )
        updated += cur.rowcount
    if updated != len(ids):
        raise click.ClickException(
            f"{len(ids) - updated} of {len(ids)} {table} changed while the "
            "invoice was being prepared (billed by another run?). Nothing was "
            "committed; re-run `zd invoice`."
        )



def parse_flat_total(flat_amount, flat_description, summarize_weeks, to_money):
    """Validate the --flat options up front and return the exact Decimal total.

    Runs before any DB/PDF work so a bad combination fails fast with a clear
    message and no side effects.
    """
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
        raise click.ClickException(f"--flat amount must be a number, got '{flat_amount}'.")
    if not parsed.is_finite():
        raise click.ClickException("--flat amount must be finite (not inf/nan).")
    # A finite Decimal can still exceed the default 28-digit context and blow
    # up quantize() (e.g. 1e999): fail as a clean ClickException, not a traceback.
    try:
        flat_total = to_money(parsed)
    except InvalidOperation:
        raise click.ClickException(f"--flat amount is too large to bill: '{flat_amount}'.")
    if flat_total <= 0:
        raise click.ClickException("--flat amount must be greater than 0.")
    return flat_total
