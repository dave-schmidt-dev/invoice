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

class ZdNumberingTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def _seed_invoice_row_for_numbering(self, conn, client_id, invoice_number, invoice_date):
        conn.execute(
            """INSERT INTO invoices
               (invoice_number, client_id, invoice_date, total, status)
               VALUES (?, ?, ?, ?, ?)""",
            (invoice_number, client_id, invoice_date, 100.00, "Sent"),
        )

    def test_next_invoice_number_past_9999_uses_integer_not_lexical_max(self):
        """With a DB/CSV containing 2026-9999, the next number for a
        2026-dated invoice must be 2026-10000 (integer max + 1), never a
        lexical miscompare (where the string "2026-10000" sorts below
        "2026-9999") and never a reset back to 2026-0001."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-9999", "2026-01-01")

            # Also seed the CSV ledger with the same number so both stores
            # agree on the existing max.
            fieldnames = ["invoice_number", "client", "invoice_date", "total", "status", "pdf_file"]
            with open(csv_path, "w", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow({
                    "invoice_number": "2026-9999",
                    "client": "Acme Corp",
                    "invoice_date": "2026-01-01",
                    "total": "100.00",
                    "status": "Sent",
                    "pdf_file": "",
                })

            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-06-01"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating invoice 2026-10000 for Acme Corp", result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                new_row = conn.execute(
                    "SELECT invoice_number FROM invoices WHERE invoice_date = ?",
                    ("2026-06-01",),
                ).fetchone()
            self.assertEqual(new_row["invoice_number"], "2026-10000")

    def test_backdated_invoice_gets_number_from_its_own_year_not_current_year(self):
        """A --date 2025-06-30 invoice must be numbered from 2025's max
        (or 2025-0001 if none), NOT the current year's series, even
        though the DB already has 2026-000x invoices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-0001", "2026-01-15")
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-0002", "2026-02-15")

            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2025-06-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating invoice 2025-0001 for Acme Corp", result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                new_row = conn.execute(
                    "SELECT invoice_number FROM invoices WHERE invoice_date = ?",
                    ("2025-06-30",),
                ).fetchone()
            self.assertEqual(new_row["invoice_number"], "2025-0001")

    def test_backdated_invoice_continues_its_own_years_series(self):
        """A backdated invoice whose year already has prior invoices gets
        the next suffix within THAT year, not year 2026's series."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                self._seed_invoice_row_for_numbering(conn, acme_id, "2025-0005", "2025-03-01")
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-0001", "2026-01-15")

            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2025-06-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating invoice 2025-0006 for Acme Corp", result.output)
