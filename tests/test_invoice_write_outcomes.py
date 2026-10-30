"""Synthetic fault injection at the DB/PDF/CSV boundaries of both invoice modes."""
import tests  # noqa: F401 - HOME/log isolation guard
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
import zd
from tests.test_invoice_immutability import _RegenerateHarness


class InvoiceWriteOutcomeTests(_RegenerateHarness):
    def test_regenerate_commit_failure_keeps_original_pdf_db_and_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db, _, ledger, directory = self._seed(tmpdir)
            runner = CliRunner()
            self._bill(runner)
            before = self._invoice_row(db)
            pdf = Path(before['pdf_path'])
            pdf.write_bytes(b'original customer-facing PDF')
            csv_before = ledger.read_bytes()
            self._null_billed_rate(db)
            self._set_client_rate(db, 'acme', 150)

            class FailingCommit(zd._ClosingConnection):
                def commit(self):
                    raise RuntimeError('injected commit failure')

            connect = sqlite3.connect

            def fail_connect(*args, **kwargs):
                kwargs['factory'] = FailingCommit
                return connect(*args, **kwargs)

            with patch.object(zd.sqlite3, 'connect', side_effect=fail_connect):
                result = runner.invoke(zd.cli, ['invoice', 'acme', '--regenerate',
                                               before['invoice_number'], '--reprice'], input='y\n')
            self.assertIsInstance(result.exception, RuntimeError, result.output)
            self.assertIn('injected commit failure', str(result.exception))
            self.assertEqual(pdf.read_bytes(), b'original customer-facing PDF')
            self.assertEqual(ledger.read_bytes(), csv_before)
            self.assertEqual(dict(self._invoice_row(db)), dict(before))
            self.assertEqual(list(directory.glob('*.tmp-*')), [])

    def test_new_csv_failure_is_reported_without_rebilling_on_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db, _, ledger, _ = self._seed(tmpdir)
            runner = CliRunner()
            inv_mod = zd._load_invoice()
            with patch.object(zd, '_load_invoice', return_value=inv_mod), patch.object(
                    inv_mod, 'save_to_csv', side_effect=OSError('injected CSV failure')):
                result = self._bill(runner)
            self.assertIn('injected CSV failure', result.output)
            self.assertNotIn('✓  Ledger updated.', result.output)
            self.assertIn('CSV ledger NOT updated', result.output)
            before = self._invoice_row(db)
            self.assertTrue(Path(before['pdf_path']).exists())
            retry = self._bill(runner)
            self.assertIn('No unbilled', retry.output)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM invoices').fetchone()[0], 1)
            self.assertEqual(self._csv_total(ledger, before['invoice_number']), '500.00')

    def test_regenerate_finalize_failure_keeps_old_pdf_and_skips_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db, _, ledger, directory = self._seed(tmpdir)
            runner = CliRunner()
            self._bill(runner)
            before = self._invoice_row(db)
            pdf = Path(before['pdf_path'])
            original_pdf, original_csv = pdf.read_bytes(), ledger.read_bytes()
            self._null_billed_rate(db)
            self._set_client_rate(db, 'acme', 150)
            replace = zd.os.replace

            def fail_pdf(src, dst):
                if '.tmp-' in str(src):
                    raise OSError('injected finalize failure')
                return replace(src, dst)

            with patch.object(zd.os, 'replace', side_effect=fail_pdf):
                result = runner.invoke(zd.cli, ['invoice', 'acme', '--regenerate',
                                               before['invoice_number'], '--reprice'], input='y\n')
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('projection is incomplete', result.output)
            self.assertEqual(self._invoice_row(db)['total'], 750)
            self.assertEqual(pdf.read_bytes(), original_pdf)
            self.assertEqual(ledger.read_bytes(), original_csv)
            self.assertEqual(list(directory.glob('*.tmp-*')), [])
            retry = self._regenerate(runner, before['invoice_number'])
            self.assertEqual(retry.exit_code, 0, retry.output)
            self.assertEqual(self._csv_total(ledger, before['invoice_number']), '750.00')

    def test_disappearing_csv_is_not_reported_as_regeneration_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db, _, ledger, _ = self._seed(tmpdir)
            runner = CliRunner()
            self._bill(runner)
            number = self._invoice_row(db)['invoice_number']
            inv_mod = zd._load_invoice()
            generate = inv_mod.generate_pdf

            def render_then_remove_ledger(*args, **kwargs):
                total = generate(*args, **kwargs)
                ledger.unlink()
                return total

            with patch.object(zd, '_load_invoice', return_value=inv_mod), patch.object(
                    inv_mod, 'generate_pdf', side_effect=render_then_remove_ledger):
                result = self._regenerate(runner, number)
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn('CSV ledger was NOT updated', result.output)
            self.assertNotIn('✓  Invoice', result.output)
            self.assertTrue(Path(self._invoice_row(db)['pdf_path']).exists())


if __name__ == '__main__':
    unittest.main()
