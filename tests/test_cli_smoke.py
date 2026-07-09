"""Everyday `zd` CLI smoke/integration tests.

Existing coverage (test_zd_invoice.py, test_invoice_numbering.py, etc.)
exercises `zd invoice`, transaction integrity, invoice numbering, money
totals, immutability, and crash regressions in depth — but none of it
drives `zd log`, `zd expense`, `zd status`, `zd sessions`, or `zd add-client`
through CliRunner. Those are the primary everyday-workflow commands, so this
file adds one hermetic, passing integration test per command against a temp
SQLite DB (never the real ~/.zd.db).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import zd


class ZdCliSmokeTests(unittest.TestCase):
    def _seed(self, tmpdir):
        """Point zd at a temp DB + temp HOME and initialize the schema.

        Returns the CliRunner to use for invocations.
        """
        db_path = Path(tmpdir) / "zd.db"
        patchers = [
            patch.object(zd, "ZD_DB", db_path),
            patch.dict(os.environ, {"HOME": tmpdir}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        zd.init_db()
        return CliRunner()

    # ------------------------------------------------------------------
    # zd add-client
    # ------------------------------------------------------------------
    def test_add_client_creates_row_with_slug_name_and_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            with patch.object(zd, "CONFIG_FILE", config_path):
                result = runner.invoke(
                    zd.cli, ["add-client", "acme", "Acme Corp", "95.00"]
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Added: Acme Corp @ $95.00/hr", result.output)

            with zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT slug, name, rate FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["slug"], "acme")
            self.assertEqual(row["name"], "Acme Corp")
            self.assertEqual(row["rate"], 95.00)

            # _sync_client_to_config also writes a minimal client entry to the
            # invoice.py config so PDF generation can find the client by name.
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(
                any(c.get("name") == "Acme Corp" for c in config.get("clients", []))
            )

    def test_add_client_updates_rate_when_slug_already_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            with patch.object(zd, "CONFIG_FILE", config_path):
                first = runner.invoke(
                    zd.cli, ["add-client", "acme", "Acme Corp", "95.00"]
                )
                self.assertEqual(first.exit_code, 0, msg=first.output)

                second = runner.invoke(
                    zd.cli, ["add-client", "acme", "Acme Corp", "110.00"]
                )

            self.assertEqual(second.exit_code, 0, msg=second.output)
            self.assertIn("Updated: Acme Corp @ $110.00/hr", second.output)

            with zd.get_conn() as conn:
                rows = conn.execute(
                    "SELECT rate FROM clients WHERE slug = ?", ("acme",)
                ).fetchall()
            # Update, not insert — exactly one row, with the new rate.
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["rate"], 110.00)

    # ------------------------------------------------------------------
    # zd log
    # ------------------------------------------------------------------
    def test_log_inserts_unbilled_session_for_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )

            result = runner.invoke(
                zd.cli,
                ["log", "acme", "1.5", "reviewed contracts", "--date", "2026-03-18"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("2026-03-18", result.output)
            self.assertIn("1.5h @ $100.00/hr = $150.00", result.output)

            with zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT work_date, hours, notes, invoice_id FROM sessions"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["work_date"], "2026-03-18")
            self.assertEqual(row["hours"], 1.5)
            self.assertEqual(row["notes"], "reviewed contracts")
            self.assertIsNone(row["invoice_id"])  # unbilled

    def test_log_rejects_non_positive_hours(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )

            result = runner.invoke(zd.cli, ["log", "acme", "0", "no-op"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Hours must be greater than 0.", result.output)
            with zd.get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            self.assertEqual(count, 0)

    # ------------------------------------------------------------------
    # zd expense
    # ------------------------------------------------------------------
    def test_expense_inserts_unbilled_expense_for_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )

            result = runner.invoke(
                zd.cli,
                ["expense", "acme", "42.00", "domain renewal", "--date", "2026-03-15"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("$42.00", result.output)
            self.assertIn("domain renewal", result.output)

            with zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT expense_date, amount, description, invoice_id FROM expenses"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["expense_date"], "2026-03-15")
            self.assertEqual(row["amount"], 42.00)
            self.assertEqual(row["description"], "domain renewal")
            self.assertIsNone(row["invoice_id"])  # unbilled

    def test_expense_rejects_non_positive_amount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )

            result = runner.invoke(zd.cli, ["expense", "acme", "0", "free"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Amount must be greater than 0.", result.output)
            with zd.get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()["n"]
            self.assertEqual(count, 0)

    # ------------------------------------------------------------------
    # zd status
    # ------------------------------------------------------------------
    def test_status_reports_unbilled_totals_and_outstanding_invoices(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                    (acme_id, "2026-03-18", 2.0, "dev work"),
                )
                conn.execute(
                    "INSERT INTO expenses (client_id, expense_date, amount, description) VALUES (?,?,?,?)",
                    (acme_id, "2026-03-18", 10.00, "hosting"),
                )
                conn.execute(
                    """INSERT INTO invoices
                           (invoice_number, client_id, invoice_date, total, status, pdf_path)
                       VALUES (?,?,?,?,?,?)""",
                    ("2026-0001", acme_id, "2026-02-01", 500.00, "Sent", "/tmp/x.pdf"),
                )

            result = runner.invoke(zd.cli, ["status"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("UNBILLED", result.output)
            self.assertIn("Acme Corp", result.output)
            # 2.0h @ $100/hr = $200.00 labor + $10.00 expenses = $210.00 total.
            self.assertIn("210.00", result.output)
            self.assertIn("OUTSTANDING INVOICES", result.output)
            self.assertIn("2026-0001", result.output)
            self.assertIn("500.00", result.output)

    def test_status_reports_all_billed_when_no_unbilled_work(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )

            result = runner.invoke(zd.cli, ["status"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("All hours billed.", result.output)
            self.assertIn("None.", result.output)  # no outstanding invoices

    # ------------------------------------------------------------------
    # zd sessions
    # ------------------------------------------------------------------
    def test_sessions_lists_unbilled_sessions_for_one_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                conn.executemany(
                    "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                    [
                        (acme_id, "2026-03-18", 1.5, "reviewed contracts"),
                        (acme_id, "2026-03-19", 2.5, "development work"),
                    ],
                )

            result = runner.invoke(zd.cli, ["sessions", "acme"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Acme Corp", result.output)
            self.assertIn("unbilled sessions", result.output)
            self.assertIn("reviewed contracts", result.output)
            self.assertIn("development work", result.output)
            # total_h = 4.0, total_amt = $400.00
            self.assertIn("4.0", result.output)
            self.assertIn("400.00", result.output)

    def test_sessions_excludes_billed_rows_unless_all_flag_given(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self._seed(tmpdir)
            with zd.get_conn() as conn:
                conn.execute(
                    "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                    ("acme", "Acme Corp", 100.00),
                )
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                conn.execute(
                    """INSERT INTO invoices
                           (invoice_number, client_id, invoice_date, total, status, pdf_path)
                       VALUES (?,?,?,?,?,?)""",
                    ("2026-0001", acme_id, "2026-02-01", 100.00, "Sent", "/tmp/x.pdf"),
                )
                invoice_id = conn.execute(
                    "SELECT id FROM invoices WHERE invoice_number = ?", ("2026-0001",)
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO sessions (client_id, work_date, hours, notes, invoice_id) VALUES (?,?,?,?,?)",
                    (acme_id, "2026-01-15", 1.0, "already billed", invoice_id),
                )

            default_result = runner.invoke(zd.cli, ["sessions", "acme"])
            all_result = runner.invoke(zd.cli, ["sessions", "acme", "--all"])

            self.assertEqual(default_result.exit_code, 0, msg=default_result.output)
            self.assertIn("No unbilled sessions for Acme Corp.", default_result.output)
            self.assertNotIn("already billed", default_result.output)

            self.assertEqual(all_result.exit_code, 0, msg=all_result.output)
            self.assertIn("already billed", all_result.output)
            self.assertIn("2026-0001", all_result.output)  # billed row shows invoice number


if __name__ == "__main__":
    unittest.main()
