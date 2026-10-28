"""`invoice.py new` must not reuse numbers or PDFs that zd already issued (review finding 3)."""
import tests  # noqa: F401 - HOME/log isolation guard
import csv
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import invoice
from tests import test_invoice_numbering as numbering


class InvoiceNewZdCollisionTests(unittest.TestCase):
    """Reuses the numbering tests' config/stdin helpers."""

    helper = numbering.InvoiceNumberingTests()

    def _seed_zd_db(self, tmpdir, number):
        db_path = Path(tmpdir) / "zd.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE invoices (invoice_number TEXT)")
            conn.execute("INSERT INTO invoices VALUES (?)", (number,))
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _run_new(self, config, db_path, number_input):
        with patch.object(invoice, "load_config", return_value=config), patch.object(
            invoice, "_zd_db_path", return_value=db_path
        ):
            return CliRunner().invoke(
                invoice.cli, ["new"], input=self.helper._new_invoice_input(number_input)
            )

    def test_default_number_skips_numbers_only_the_zd_db_holds(self):
        year = date.today().year
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.helper._config(tmpdir)
            db_path = self._seed_zd_db(tmpdir, f"{year}-0005")
            result = self._run_new(config, db_path, "")  # blank -> default
            self.assertEqual(result.exit_code, 0, msg=result.output)
            with open(config["storage"]["ledger_file"], newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([r["invoice_number"] for r in rows], [f"{year}-0006"])

    def test_explicit_number_held_by_zd_db_is_refused_and_pdf_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.helper._config(tmpdir)
            db_path = self._seed_zd_db(tmpdir, "2026-0005")
            pdf = self.helper._expected_pdf_path(config, "2026-0005")
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF zd original\n")

            result = self._run_new(config, db_path, "2026-0005")

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("zd database", result.output)
            self.assertEqual(pdf.read_bytes(), b"%PDF zd original\n")
            self.assertFalse(Path(config["storage"]["ledger_file"]).exists())

    def test_existing_final_pdf_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.helper._config(tmpdir)
            db_path = Path(tmpdir) / "absent.db"
            pdf = self.helper._expected_pdf_path(config, "2026-0009")
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF orphan\n")

            result = self._run_new(config, db_path, "2026-0009")

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("already exists", result.output)
            self.assertEqual(pdf.read_bytes(), b"%PDF orphan\n")
            self.assertFalse(Path(config["storage"]["ledger_file"]).exists())
            self.assertEqual([p for p in pdf.parent.iterdir() if ".tmp-" in p.name], [])


if __name__ == "__main__":
    unittest.main()
