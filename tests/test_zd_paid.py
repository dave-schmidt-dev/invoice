"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin

class ZdPaidTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def _seed_invoice_row(self, tmpdir, invoice_number="2026-0001", status="Sent"):
        """Insert a minimal invoices row for `zd paid` tests."""
        db_path, config_path = self._seed_invoice_data(tmpdir)
        with zd.get_conn() as conn:
            acme_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("acme",)
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO invoices
                   (invoice_number, client_id, invoice_date, total, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (invoice_number, acme_id, "2026-05-01", 500.00, status),
            )
        return db_path, config_path

    def _write_ledger_csv(self, config_path, rows):
        """Write a minimal invoices.csv ledger matching `rows` (dicts)."""
        import csv as _csv
        with open(config_path) as f:
            config = json.load(f)
        csv_path = Path(config["storage"]["ledger_file"])
        fieldnames = ["invoice_number", "client", "invoice_date", "total", "status", "pdf_file"]
        with open(csv_path, "w", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return csv_path

    def test_paid_with_no_date_stores_today_and_sets_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_row(tmpdir, "2026-0001")
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT status, paid_date FROM invoices WHERE invoice_number = ?",
                    ("2026-0001",),
                ).fetchone()
            self.assertEqual(row["status"], "Paid")
            self.assertEqual(row["paid_date"], zd.date.today().isoformat())

    def test_paid_with_explicit_date_stores_that_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_row(tmpdir, "2026-0001")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["paid", "2026-0001", "--date", "2026-05-01"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT status, paid_date FROM invoices WHERE invoice_number = ?",
                    ("2026-0001",),
                ).fetchone()
            self.assertEqual(row["status"], "Paid")
            self.assertEqual(row["paid_date"], "2026-05-01")

    def test_paid_rejects_malformed_date_without_db_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_row(tmpdir, "2026-0001")
            runner = CliRunner()

            for bad_date in ("2026-13-40", "not-a-date"):
                result = runner.invoke(
                    zd.cli, ["paid", "2026-0001", "--date", bad_date]
                )
                self.assertNotEqual(result.exit_code, 0, msg=result.output)
                with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                    row = conn.execute(
                        "SELECT status, paid_date FROM invoices WHERE invoice_number = ?",
                        ("2026-0001",),
                    ).fetchone()
                self.assertEqual(row["status"], "Sent")
                self.assertIsNone(row["paid_date"])

    def test_paid_updates_matching_csv_row_and_reports_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_row(tmpdir, "2026-0001")
            csv_path = self._write_ledger_csv(
                config_path,
                [
                    {
                        "invoice_number": "2026-0001",
                        "client": "Acme Corp",
                        "invoice_date": "2026-05-01",
                        "total": "500.00",
                        "status": "Sent",
                        "pdf_file": "",
                    }
                ],
            )
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("marked Paid in zd DB and CSV ledger", result.output)
            import csv as _csv
            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            self.assertEqual(csv_rows[0]["status"], "Paid")

    def test_paid_with_no_matching_csv_row_does_not_falsely_claim_csv_update(self):
        # This isolates cmd_paid's OWN honest-reporting logic (H.4). A.6 later
        # added an auto-convergence pass (_auto_converge) that would heal this
        # exact DB-ahead drift by appending the missing CSV row before cmd_paid
        # patches it — the correct integrated behavior, covered by
        # ReconcileConvergenceTests. Here we disable that layer so cmd_paid
        # faces a genuinely-absent row (the degraded path where convergence
        # could not heal) and must still report honestly rather than claim a
        # CSV update it did not make.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_row(tmpdir, "2026-0001")
            csv_path = self._write_ledger_csv(
                config_path,
                [
                    {
                        "invoice_number": "2026-9999",
                        "client": "Acme Corp",
                        "invoice_date": "2026-05-01",
                        "total": "999.00",
                        "status": "Sent",
                        "pdf_file": "",
                    }
                ],
            )
            runner = CliRunner()

            with patch.object(zd, "_auto_converge", lambda conn: None):
                result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            # DB must still be updated to Paid.
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM invoices WHERE invoice_number = ?",
                    ("2026-0001",),
                ).fetchone()
            self.assertEqual(row["status"], "Paid")
            # Output must NOT falsely claim the CSV ledger was updated.
            self.assertNotIn("marked Paid in zd DB and CSV ledger", result.output)
            self.assertIn("No matching row for 2026-0001 found in the CSV ledger", result.output)
            self.assertNotIn("zd reconcile", result.output)
            # Nothing matched, so the ledger was neither rewritten nor backed up.
            self.assertEqual(
                list(csv_path.parent.glob(f"{csv_path.name}.*.bak")), [],
                "an unmatched paid must not back up or rewrite the CSV ledger",
            )

    def test_paid_backs_up_csv_before_patching_matched_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_row(tmpdir, "2026-0001")
            csv_path = self._write_ledger_csv(
                config_path,
                [
                    {
                        "invoice_number": "2026-0001",
                        "client": "Acme Corp",
                        "invoice_date": "2026-05-01",
                        "total": "500.00",
                        "status": "Sent",
                        "pdf_file": "",
                    }
                ],
            )
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            backups = list(csv_path.parent.glob(f"{csv_path.name}.*.bak"))
            self.assertTrue(backups, "expected a .bak backup of the CSV ledger before the patch")
