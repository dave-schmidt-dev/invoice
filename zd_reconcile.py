"""Converge the invoice.py CSV ledger to the authoritative zd DB (moved verbatim from zd.py)."""

from decimal import Decimal
from pathlib import Path
from invoice_ledger import update_ledger_rows
from zd_store import _backup_file, to_money
from zd_summary import group_sessions_by_week


# Locate invoice.py relative to this script (they live in the same project dir)
_SCRIPT_DIR = Path(__file__).resolve().parent


INVOICE_PY = _SCRIPT_DIR / "invoice.py"


def _load_invoice():
    """Execute invoice.py fresh and return the module.

    Loaded anew on every call so its HOME-derived defaults are current
    (tests/test_invoice_fresh_load.py). The module is built through
    ``importlib.util.spec_from_file_location`` looked up at call time, which
    the INV-2 race-loader test hooks.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("invoice", INVOICE_PY)
    inv_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv_mod)
    return inv_mod


class _ReconcileResult:
    """Summary of a single _converge_db_to_csv pass.

    `ok` is False only when the DB/config/ledger could not even be loaded
    (a soft, non-fatal degrade — see _converge_db_to_csv). `warning` carries
    a one-line human explanation for that case. The four lists below hold
    human-readable strings describing each drifted invoice, for reporting
    by `zd reconcile` and the auto-convergence callers.
    """

    def __init__(self):
        self.ok = True
        self.warning = None
        self.appended = []       # DB-ahead: rows appended to the CSV
        self.status_synced = []  # DB status="Paid" patched into the CSV
        self.total_drift = []    # session-sum vs stored-total mismatches (flagged only)
        self.orphans = []        # CSV-only rows with no matching DB invoice (flagged only)

    @property
    def changed(self):
        return bool(self.appended or self.status_synced)

    @property
    def flagged(self):
        return bool(self.total_drift or self.orphans)


def _reconstruct_csv_line_items(conn, inv_row, client_row):
    """Rebuild the `line_items` list for inv_row exactly as the `zd invoice
    --regenerate` path does (zd.py's regenerate branch), but WITHOUT ever
    touching the weekly-summary server: summary_provider is always None here.
    Reconcile runs opportunistically (at the top of ordinary commands) and
    must never spawn/await llama-server.

    Mirrors the shape at zd.py:1310-1416 (billed-session query with
    COALESCE(s.billed_rate, cl.rate) AS rate, the flat-mode single "Flat fee"
    item, and per-expense line items).
    """
    sessions = conn.execute(
        """SELECT s.*, COALESCE(s.billed_rate, cl.rate) AS rate FROM sessions s
           JOIN clients cl ON cl.id = s.client_id
           WHERE s.invoice_id = ?
           ORDER BY s.work_date""",
        (inv_row["id"],),
    ).fetchall()

    billing_mode = inv_row["billing_mode"] if "billing_mode" in inv_row.keys() else None

    if billing_mode == "flat":
        stored_total = to_money(str(inv_row["total"]))
        return [{
            "description": "Flat fee",
            "hours": 0,
            "rate": 0,
            "amount": float(stored_total),
        }]

    line_items = group_sessions_by_week(sessions, summary_provider=None)

    expenses = conn.execute(
        "SELECT * FROM expenses WHERE invoice_id = ?",
        (inv_row["id"],),
    ).fetchall()
    for e in expenses:
        line_items.append({
            "description": f"Expense: {e['description']}",
            "hours": 0,
            "rate": 0,
            "amount": float(to_money(e["amount"])),
        })
    return line_items


def _converge_db_to_csv(conn, *, apply, report=True, echo_prefix=""):
    """Reconcile the CSV ledger (a projection) against the zd DB (the
    authoritative store). Only ever writes the CSV, and only in the
    DB-ahead-of-CSV direction:

      - a DB invoice missing from the CSV gets APPENDED (reconstructed from
        the DB via _reconstruct_csv_line_items);
      - a DB invoice with status="Paid" whose CSV row still says something
        else gets its CSV status field PATCHED to "Paid" in place.

    It NEVER writes the DB, NEVER imports a CSV-only row into the DB, NEVER
    deletes a CSV row, NEVER changes a stored invoice total, and NEVER
    downgrades a CSV status (a CSV that already says "Paid" while the DB
    disagrees is the dangerous direction and is left completely alone —
    not even flagged as an orphan/drift, since status mismatches other than
    DB-Paid/CSV-behind are intentionally out of scope here).

    Also FLAGS (report-only, never auto-fixed):
      - session-sum vs stored-total drift beyond a cent;
      - CSV-only orphans (a CSV invoice_number absent from the DB).

    Degrades gracefully on any expected failure (missing config, missing
    ledger, load failure) by returning a no-op _ReconcileResult — this
    function is called opportunistically from ordinary commands and must
    NEVER raise out to the caller.
    """
    result = _ReconcileResult()
    try:
        inv_mod = _load_invoice()
        config = inv_mod.load_config()
        csv_path = Path(inv_mod._ledger_path_from_config(config))
    except Exception as e:
        result.ok = False
        result.warning = f"reconcile: could not load invoice.py/config ({e})"
        return result

    try:
        inv_rows = conn.execute(
            """SELECT i.id, i.invoice_number, i.invoice_date, i.total, i.status,
                      i.pdf_path, i.client_id, i.billing_mode, cl.name AS client_name,
                      cl.rate AS client_rate
               FROM invoices i JOIN clients cl ON cl.id = i.client_id
               ORDER BY i.invoice_date, i.invoice_number"""
        ).fetchall()
    except Exception as e:
        result.ok = False
        result.warning = f"reconcile: could not read invoices from the DB ({e})"
        return result

    if not csv_path.exists():
        if not apply:
            # Nothing to compare against; every DB invoice is technically
            # "missing" from a nonexistent ledger, but with no ledger file
            # there's no safe in-place patch target either way. Report
            # nothing rather than a wall of noise for a brand-new setup.
            return result
        # apply=True with no ledger file yet: fall through, each DB invoice
        # will be treated as missing and appended (save_to_csv creates the
        # file).
        csv_rows, csv_headers = [], []
        csv_by_number = {}
    else:
        try:
            csv_rows, csv_headers = inv_mod._read_csv_with_headers(csv_path)
        except Exception as e:
            result.ok = False
            result.warning = f"reconcile: could not read the CSV ledger ({e})"
            return result
        inv_key = inv_mod._csv_field_key(csv_headers, "invoice_number") or "invoice_number"
        csv_by_number = {str(r.get(inv_key, "")): r for r in csv_rows}

    db_numbers = {str(r["invoice_number"]) for r in inv_rows}

    # ---- Orphans: CSV rows with no matching DB invoice (flag only) ----
    # Report-only work; the auto-converge hot path passes report=False since it
    # only ever acts on missing/status-behind rows.
    if report:
        for number, row in csv_by_number.items():
            if number and number not in db_numbers:
                result.orphans.append(number)

    missing_rows = []       # DB invoices absent from the CSV -> append
    status_behind_rows = [] # DB status=Paid, CSV status != Paid -> patch

    for inv_row in inv_rows:
        number = str(inv_row["invoice_number"])
        csv_row = csv_by_number.get(number)

        if csv_row is None:
            missing_rows.append(inv_row)
        else:
            csv_status = csv_row.get(
                inv_mod._csv_field_key(csv_headers, "status") or "status"
            )
            if inv_row["status"] == "Paid" and csv_status != "Paid":
                status_behind_rows.append(inv_row)
            # Any OTHER status mismatch (e.g. CSV says Paid, DB says Sent)
            # is the dangerous direction: never touched, never even flagged
            # here (it is not a DB-ahead condition this function repairs).

        # ---- Session-sum vs stored-total drift (flag only, report path) ----
        # Report-only, so skipped on the auto-converge hot path (report=False).
        # Flat invoices bill a fixed amount, not hours*rate — never flag them.
        if report and inv_row["billing_mode"] != "flat":
            try:
                # Recompute the total EXACTLY as the billing / regenerate paths
                # do: sum to_money(line-item amount) over the reconstructed line
                # items (per-week grouping), NOT a per-session pre-round of
                # hours*rate. A correct invoice must therefore show zero drift —
                # the old per-session rounding fabricated sub-cent discrepancies.
                client_row = {"name": inv_row["client_name"]}
                line_items = _reconstruct_csv_line_items(conn, inv_row, client_row)
                computed = to_money(sum(
                    (to_money(str(li["amount"])) for li in line_items),
                    Decimal("0.00"),
                ))
                stored = to_money(str(inv_row["total"]))
                if abs(computed - stored) > Decimal("0.01"):
                    result.total_drift.append(
                        f"{number}: stored ${stored:,.2f} vs session-sum ${computed:,.2f}"
                    )
            except Exception:
                # Drift detection is best-effort reporting only; never let it
                # abort the (more important) missing-row/status-sync repair.
                pass

    if not apply or (not missing_rows and not status_behind_rows):
        result.appended = [str(r["invoice_number"]) for r in missing_rows]
        result.status_synced = [str(r["invoice_number"]) for r in status_behind_rows]
        return result

    # ---- Apply: append missing rows, patch status-behind rows ----
    for inv_row in missing_rows:
        try:
            client_row = {"name": inv_row["client_name"]}
            line_items = _reconstruct_csv_line_items(conn, inv_row, client_row)
            inv_mod.save_to_csv(
                str(inv_row["invoice_number"]),
                inv_row["invoice_date"],
                config,
                line_items,
                total=str(inv_row["total"]),
                pdf_file=(inv_row["pdf_path"] or ""),
                client=client_row,
                status=inv_row["status"],
            )
            result.appended.append(str(inv_row["invoice_number"]))
        except Exception as e:
            result.warning = f"reconcile: could not append {inv_row['invoice_number']} to the CSV ({e})"

    if status_behind_rows:
        try:
            numbers_to_patch = {str(r["invoice_number"]) for r in status_behind_rows}
            patched = update_ledger_rows(
                csv_path,
                {number: {"status": "Paid"} for number in numbers_to_patch},
                backup=_backup_file,
                first_match_only=False,
            )
            if patched:
                result.status_synced = sorted(numbers_to_patch)
        except Exception as e:
            result.warning = f"reconcile: could not sync Paid status to the CSV ({e})"

    return result
