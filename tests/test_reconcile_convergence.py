"""Focused CSV and database reconciliation convergence tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import csv as _csv
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd

class ReconcileConvergenceTests(unittest.TestCase):
    """Task A.6: `zd reconcile [--fix]` and the auto-convergence hook.

    Covers the DB-authoritative / CSV-projection contract:
      - DB-ahead-of-CSV (a missing row, or DB status=Paid not yet reflected
        in the CSV) is the ONLY drift that gets repaired, and only ever by
        writing the CSV.
      - `zd reconcile` (no --fix) is report-only; `--fix` applies repairs.
      - CSV-only orphans are reported, NEVER imported into the DB.
      - Session-sum vs stored-total drift is flagged, NEVER auto-corrected.
      - A CSV that already says "Paid" while the DB disagrees (the
        dangerous direction) is never downgraded.

    Reuses the sandbox shape from InvoiceTransactionIntegrityTests: a
    TemporaryDirectory sandbox, HOME/ZD_DB/CONFIG_FILE patched, a
    storage.ledger_file CSV, and CliRunner with input="y\n" for the
    "Proceed?" confirmation on `zd invoice`.
    """

    def _write_config(self, tmpdir):
        config_path = Path(tmpdir) / ".invoice_config.json"
        invoices_dir = Path(tmpdir) / "invoices"
        ledger_file = Path(tmpdir) / "invoices.csv"
        config = {
            "invoice_header": {"title": "INVOICE", "logo_path": ""},
            "payee": {
                "name": "Zero Delta LLC",
                "address": "",
                "city": "",
                "state": "",
                "zip": "",
                "email": "",
                "phone": "",
            },
            "clients": [
                {
                    "name": "Acme Corp",
                    "address": "",
                    "city": "",
                    "state": "",
                    "zip": "",
                    "contact": "",
                }
            ],
            "payment": {
                "bank_name": "",
                "routing": "",
                "account": "",
                "description": "",
            },
            "storage": {
                "ledger_file": str(ledger_file),
                "invoices_dir": str(invoices_dir),
            },
            "zd": {
                "weekly_summaries": {
                    "enabled": False,
                    "base_url": "http://127.0.0.1:8086",
                    "model": "summarizer",
                    "timeout_seconds": 30,
                }
            },
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path, Path(ledger_file), Path(invoices_dir)

    def _seed(self, tmpdir, rate=100.00):
        """Seed a client (Acme) with two unbilled sessions + one expense."""
        db_path = Path(tmpdir) / "zd.db"
        config_path, ledger_file, invoices_dir = self._write_config(tmpdir)
        patchers = [
            patch.object(zd, "ZD_DB", db_path),
            patch.object(zd, "CONFIG_FILE", config_path),
            patch.dict(os.environ, {"HOME": tmpdir}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        zd.init_db()
        with zd.get_conn() as conn:
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("acme", "Acme Corp", rate),
            )
            acme_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("acme",)
            ).fetchone()["id"]
            conn.executemany(
                "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                [
                    (acme_id, "2026-04-01", 2.0, "kickoff"),
                    (acme_id, "2026-04-15", 3.0, "midpoint"),
                ],
            )
            conn.execute(
                "INSERT INTO expenses (client_id, expense_date, amount, description) VALUES (?,?,?,?)",
                (acme_id, "2026-04-10", 40.00, "filing fee"),
            )
        return db_path, config_path, ledger_file, invoices_dir

    def _bill_invoice(self, date_str="2026-04-30"):
        """Bill the seeded Acme sessions/expenses via `zd invoice`, returning
        the invoice_number that was created."""
        runner = CliRunner()
        result = runner.invoke(
            zd.cli, ["invoice", "acme", "--date", date_str], input="y\n"
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        with zd.get_conn(readonly=True) as conn:
            row = conn.execute(
                "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["invoice_number"]

    @staticmethod
    def _csv_rows(ledger_file):
        if not Path(ledger_file).exists():
            return []
        with open(ledger_file, newline="", encoding="utf-8-sig") as f:
            return list(_csv.DictReader(f))

    @staticmethod
    def _csv_bytes(ledger_file):
        return Path(ledger_file).read_bytes() if Path(ledger_file).exists() else None

    @staticmethod
    def _write_csv_rows(ledger_file, rows, fieldnames):
        with open(ledger_file, "w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _invoice_count(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 1. DB-ahead missing row auto-repaired by an ordinary command
    # ------------------------------------------------------------------
    def test_missing_csv_row_auto_repaired_by_status_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, invoices_dir = self._seed(tmpdir)
            invoice_number = self._bill_invoice()

            # Confirm the CSV row exists post-bill, then blow it away to
            # simulate the CSV falling behind the DB (e.g. a hand-edit or a
            # lost write).
            rows = self._csv_rows(ledger_file)
            self.assertEqual(len(rows), 1)
            fieldnames = list(rows[0].keys())
            self._write_csv_rows(ledger_file, [], fieldnames)
            self.assertEqual(self._csv_rows(ledger_file), [])

            with zd.get_conn(readonly=True) as conn:
                db_row = conn.execute(
                    "SELECT total, status FROM invoices WHERE invoice_number = ?",
                    (invoice_number,),
                ).fetchone()

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["status"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Synced", result.output)

            repaired = self._csv_rows(ledger_file)
            self.assertEqual(len(repaired), 1)
            self.assertEqual(repaired[0]["invoice_number"], invoice_number)
            self.assertEqual(float(repaired[0]["total"]), float(db_row["total"]))
            self.assertEqual(repaired[0]["status"], db_row["status"])

            # The DB itself must be completely unchanged by convergence.
            self.assertEqual(self._invoice_count(db_path), 1)

    # ------------------------------------------------------------------
    # 2. `zd reconcile` (no --fix) is report-only
    # ------------------------------------------------------------------
    def test_reconcile_without_fix_is_report_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, ledger_file, _ = self._seed(tmpdir)
            invoice_number = self._bill_invoice()

            rows = self._csv_rows(ledger_file)
            fieldnames = list(rows[0].keys())
            self._write_csv_rows(ledger_file, [], fieldnames)
            before_bytes = self._csv_bytes(ledger_file)

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["reconcile"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(invoice_number, result.output)
            self.assertIn("Would repair", result.output)

            # Byte-for-byte unchanged: no write happened.
            self.assertEqual(self._csv_bytes(ledger_file), before_bytes)

    # ------------------------------------------------------------------
    # 3. `zd reconcile --fix` repairs it
    # ------------------------------------------------------------------
    def test_reconcile_with_fix_repairs_missing_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, ledger_file, _ = self._seed(tmpdir)
            invoice_number = self._bill_invoice()

            rows = self._csv_rows(ledger_file)
            fieldnames = list(rows[0].keys())
            original_total = rows[0]["total"]
            self._write_csv_rows(ledger_file, [], fieldnames)

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["reconcile", "--fix"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Repaired", result.output)

            repaired = self._csv_rows(ledger_file)
            self.assertEqual(len(repaired), 1)
            self.assertEqual(repaired[0]["invoice_number"], invoice_number)
            self.assertEqual(repaired[0]["total"], original_total)

    # ------------------------------------------------------------------
    # 4. CSV-only orphan is reported, NEVER imported into the DB
    # ------------------------------------------------------------------
    def test_csv_only_orphan_reported_not_imported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed(tmpdir)
            self._bill_invoice()

            rows = self._csv_rows(ledger_file)
            fieldnames = list(rows[0].keys())
            orphan_row = dict(rows[0])
            orphan_row["invoice_number"] = "2099-9999"
            rows.append(orphan_row)
            self._write_csv_rows(ledger_file, rows, fieldnames)

            invoices_before = self._invoice_count(db_path)

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["reconcile"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("2099-9999", result.output)
            self.assertIn("NOT imported", result.output)
            self.assertEqual(self._invoice_count(db_path), invoices_before)

            # --fix must not import it either.
            result_fix = runner.invoke(zd.cli, ["reconcile", "--fix"])
            self.assertEqual(result_fix.exit_code, 0, msg=result_fix.output)
            self.assertEqual(self._invoice_count(db_path), invoices_before)
            self.assertIn("2099-9999", result_fix.output)

    # ------------------------------------------------------------------
    # 5. status-behind auto-synced; the reverse is NEVER downgraded
    # ------------------------------------------------------------------
    def test_status_behind_synced_and_reverse_never_downgraded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, ledger_file, _ = self._seed(tmpdir)
            invoice_number = self._bill_invoice()

            # Mark Paid in the DB ONLY (direct UPDATE), leaving the CSV at
            # "Sent". A convergence-triggering command must flip the CSV to
            # Paid.
            with zd.get_conn() as conn:
                conn.execute(
                    "UPDATE invoices SET status = 'Paid' WHERE invoice_number = ?",
                    (invoice_number,),
                )

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["status"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Synced", result.output)

            rows = self._csv_rows(ledger_file)
            self.assertEqual(rows[0]["status"], "Paid")

            # Reverse case: CSV says Paid, DB says Sent -> must NOT downgrade.
            with zd.get_conn() as conn:
                conn.execute(
                    "UPDATE invoices SET status = 'Sent' WHERE invoice_number = ?",
                    (invoice_number,),
                )
            # rows[0] is already "Paid" in the CSV from the sync above; leave
            # it as-is (CSV Paid, DB Sent) and run convergence again.
            result2 = runner.invoke(zd.cli, ["status"])
            self.assertEqual(result2.exit_code, 0, msg=result2.output)

            rows_after = self._csv_rows(ledger_file)
            self.assertEqual(rows_after[0]["status"], "Paid")
            with zd.get_conn(readonly=True) as conn:
                db_status = conn.execute(
                    "SELECT status FROM invoices WHERE invoice_number = ?",
                    (invoice_number,),
                ).fetchone()["status"]
            self.assertEqual(db_status, "Sent")

    # ------------------------------------------------------------------
    # 6. session-sum vs stored-total drift flagged, stored total untouched
    # ------------------------------------------------------------------
    def test_session_sum_vs_stored_total_drift_flagged_not_changed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed(tmpdir)
            invoice_number = self._bill_invoice()

            with zd.get_conn() as conn:
                inv_row = conn.execute(
                    "SELECT id, total FROM invoices WHERE invoice_number = ?",
                    (invoice_number,),
                ).fetchone()
                stored_total_before = inv_row["total"]
                # Hand-craft drift: bump a billed session's hours so
                # hours*rate no longer matches the stored total.
                conn.execute(
                    "UPDATE sessions SET hours = hours + 100 WHERE invoice_id = ? "
                    "AND id = (SELECT MIN(id) FROM sessions WHERE invoice_id = ?)",
                    (inv_row["id"], inv_row["id"]),
                )

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["reconcile"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(invoice_number, result.output)
            self.assertIn("drift", result.output.lower())

            with zd.get_conn(readonly=True) as conn:
                stored_total_after = conn.execute(
                    "SELECT total FROM invoices WHERE invoice_number = ?",
                    (invoice_number,),
                ).fetchone()["total"]
            self.assertEqual(stored_total_after, stored_total_before)

            # --fix must also never change the stored total.
            result_fix = runner.invoke(zd.cli, ["reconcile", "--fix"])
            self.assertEqual(result_fix.exit_code, 0, msg=result_fix.output)
            with zd.get_conn(readonly=True) as conn:
                stored_total_after_fix = conn.execute(
                    "SELECT total FROM invoices WHERE invoice_number = ?",
                    (invoice_number,),
                ).fetchone()["total"]
            self.assertEqual(stored_total_after_fix, stored_total_before)

    def test_correct_invoice_with_fractional_hours_not_flagged_as_drift(self):
        """Regression: the drift check must recompute the total the way billing
        does — per-WEEK grouping (to_money(weekly_hours * rate)) — not a
        per-session pre-round of to_money(hours) * to_money(rate). Two 0.125h
        sessions in one week bill to to_money(0.25 * 100) = $25.00, but a
        per-session round would give to_money(0.125)*100 = $13.00 each = $26.00,
        fabricating a $1 phantom discrepancy on a perfectly correct invoice.
        Reconcile must report NO drift here."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed(tmpdir, rate=100.00)
            # Replace the integer-hours seed data with two sub-cent-hours
            # sessions in the SAME week and no expenses, so the invoice is a
            # single weekly line item whose per-week total is exact.
            with zd.get_conn() as conn:
                conn.execute("DELETE FROM expenses")
                conn.execute("DELETE FROM sessions")
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = 'acme'"
                ).fetchone()["id"]
                conn.executemany(
                    "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                    [
                        (acme_id, "2026-04-06", 0.125, "a"),  # Monday
                        (acme_id, "2026-04-06", 0.125, "b"),  # same week
                    ],
                )

            invoice_number = self._bill_invoice()

            runner = CliRunner()
            result = runner.invoke(zd.cli, ["reconcile"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            # The drift flag line uniquely contains "vs session-sum"; a correctly
            # billed invoice must never appear in one.
            self.assertNotIn(
                "vs session-sum", result.output,
                msg=f"phantom drift flagged on a correct invoice:\n{result.output}",
            )
