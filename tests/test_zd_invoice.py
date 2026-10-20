"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin


class ZdInvoiceTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def test_invoice_month_limits_sessions_and_expenses_to_selected_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Month: 2026-04", result.output)
            self.assertIn("5.0 hours @ $100.00/hr = $500.00", result.output)
            self.assertIn("Expenses: $20.00", result.output)
            self.assertIn("Total: $520.00", result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                billed_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NOT NULL
                       ORDER BY work_date"""
                ).fetchall()
                unbilled_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NULL
                       ORDER BY work_date"""
                ).fetchall()
                billed_expenses = conn.execute(
                    """SELECT expense_date FROM expenses
                       WHERE invoice_id IS NOT NULL
                       ORDER BY expense_date"""
                ).fetchall()
                unbilled_expenses = conn.execute(
                    """SELECT expense_date FROM expenses
                       WHERE invoice_id IS NULL
                       ORDER BY expense_date"""
                ).fetchall()

            self.assertEqual([row["work_date"] for row in billed_sessions], ["2026-04-01", "2026-04-30"])
            self.assertEqual(
                [row["work_date"] for row in unbilled_sessions],
                ["2026-03-31", "2026-04-15", "2026-05-01"],
            )
            self.assertEqual([row["expense_date"] for row in billed_expenses], ["2026-04-15"])
            self.assertEqual(
                [row["expense_date"] for row in unbilled_expenses],
                ["2026-03-31", "2026-05-01"],
            )

    def test_invoice_status_matches_between_csv_ledger_and_zd_db(self):
        """zd invoice writes the same status to the CSV ledger and the zd
        DB so the two sources of truth never disagree on a fresh invoice."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                input="y\n",
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                db_row = conn.execute(
                    "SELECT invoice_number, status FROM invoices ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertIsNotNone(db_row, "expected an invoice row in the zd DB")

            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            csv_match = next(
                (r for r in csv_rows if r.get("invoice_number") == db_row["invoice_number"]),
                None,
            )
            self.assertIsNotNone(csv_match, "expected the invoice in the CSV ledger")
            self.assertEqual(
                csv_match["status"], db_row["status"],
                "CSV ledger status must match zd DB status for a fresh invoice",
            )
            self.assertEqual(db_row["status"], "Sent")

    def test_invoice_without_month_still_invoices_all_unbilled_client_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-04-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIn("Month:", result.output)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                billed_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NOT NULL
                       ORDER BY work_date"""
                ).fetchall()
                unbilled_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NULL
                       ORDER BY work_date"""
                ).fetchall()

            self.assertEqual(
                [row["work_date"] for row in billed_sessions],
                ["2026-03-31", "2026-04-01", "2026-04-30", "2026-05-01"],
            )
            self.assertEqual([row["work_date"] for row in unbilled_sessions], ["2026-04-15"])

    def test_invoice_month_with_no_matching_work_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-02", "--date", "2026-02-28"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(
                "No unbilled sessions or expenses for Acme Corp in 2026-02.",
                result.output,
            )

    def test_invoice_rejects_malformed_date_with_clean_error(self):
        """A malformed --date must fail as a clean ClickException (as cmd_log
        does), never a raw ValueError traceback, and write no invoice. The
        numbering step parses --date via date.fromisoformat, so this guards
        that path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-13-45"],
                input="y\n",
            )

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Date must be YYYY-MM-DD", result.output)
            # A clean ClickException, not a leaked ValueError from fromisoformat.
            self.assertNotIsInstance(result.exception, ValueError)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn(readonly=True) as conn:
                count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            self.assertEqual(count, 0, "no invoice row may be written on a bad date")

    def test_invoice_rejects_invalid_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["invoice", "acme", "--month", "2026-4"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Month must be YYYY-MM format.", result.output)

    def test_invoice_help_documents_month_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            patcher = patch.object(zd, "ZD_DB", Path(tmpdir) / "zd.db")
            patcher.start()
            self.addCleanup(patcher.stop)
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["invoice", "--help"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--month YYYY-MM", result.output)
        self.assertIn("Only invoice unbilled items in this calendar month", result.output)
        self.assertIn("zd invoice acme --month 2026-04 --date 2026-04-30", result.output)
        self.assertIn("--summarize-weeks", result.output)
        self.assertNotIn("--summary-provider", result.output)

    def test_invoice_help_does_not_initialize_database_for_process_invocation(self):
        runner = CliRunner()

        with patch.object(zd, "init_db", side_effect=AssertionError("init_db called")), patch.object(
            sys, "argv", ["zd", "invoice", "--help"]
        ):
            result = runner.invoke(zd.cli, ["invoice", "--help"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--month YYYY-MM", result.output)
