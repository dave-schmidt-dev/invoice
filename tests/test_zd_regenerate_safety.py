"""zd invoice --regenerate: no silent repricing, no PDF loss (review findings 4 and 5)."""
import tests  # noqa: F401 - HOME/log isolation guard
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

import zd
from tests import test_invoice_immutability as imm


class RegenerateSafetyTests(imm._RegenerateHarness):

    def _billed(self, tmpdir):
        db_path, _, ledger_file, invoices_dir = self._seed(tmpdir, rate=100.00)
        runner = CliRunner()
        self._bill(runner)
        number = self._invoice_row(db_path)["invoice_number"]
        return db_path, ledger_file, invoices_dir, runner, number

    # -- finding 5 ------------------------------------------------------
    def test_legacy_regenerate_that_would_reprice_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, ledger_file, _, runner, number = self._billed(tmpdir)
            self._null_billed_rate(db_path)
            self._set_client_rate(db_path, "acme", 150.00)

            result = self._regenerate(runner, number)

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("--reprice", result.output)
            self.assertIn("500.00", result.output)   # stored
            self.assertIn("750.00", result.output)   # recomputed
            self.assertEqual(self._invoice_row(db_path)["total"], 500.0)
            self.assertEqual(self._csv_total(ledger_file, number), "500.00")

    def test_reprice_flag_accepts_the_new_total(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, ledger_file, _, runner, number = self._billed(tmpdir)
            self._null_billed_rate(db_path)
            self._set_client_rate(db_path, "acme", 150.00)

            result = runner.invoke(
                zd.cli, ["invoice", "acme", "--regenerate", number, "--reprice"], input="y\n"
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(self._invoice_row(db_path)["total"], 750.0)
            self.assertEqual(self._csv_total(ledger_file, number), "750.00")

    # -- finding 4 ------------------------------------------------------
    def _move_pdf_elsewhere(self, db_path, invoices_dir, tmpdir):
        """Point the DB/CSV at an old PDF path so regenerate must change the path."""
        row = self._invoice_row(db_path)
        old = Path(tmpdir) / "old_location.pdf"
        old.write_bytes(Path(row["pdf_path"]).read_bytes())
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE invoices SET pdf_path = ?", (str(old),))
            conn.commit()
        finally:
            conn.close()
        return old

    def test_old_pdf_is_kept_and_failure_reported_when_csv_cannot_be_patched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, ledger_file, invoices_dir, runner, number = self._billed(tmpdir)
            old = self._move_pdf_elsewhere(db_path, invoices_dir, tmpdir)
            # Legacy ledger with no total/pdf_file columns: the patch cannot be applied.
            with open(ledger_file, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["invoice_number", "date", "status"])
                w.writerow([number, "2026-04-30", "Sent"])

            result = self._regenerate(runner, number)

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("CSV ledger", result.output)
            self.assertTrue(old.exists(), "old PDF deleted although the CSV was not repointed")

    def test_old_pdf_is_removed_after_a_successful_csv_patch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, ledger_file, invoices_dir, runner, number = self._billed(tmpdir)
            old = self._move_pdf_elsewhere(db_path, invoices_dir, tmpdir)

            result = self._regenerate(runner, number)

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
