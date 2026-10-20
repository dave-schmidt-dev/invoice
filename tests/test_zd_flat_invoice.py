"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import json
import tempfile
import unittest
from decimal import InvalidOperation
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin

class ZdFlatInvoiceTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def test_flat_invoice_total_is_amount_independent_of_logged_hours(self):
        """A --flat invoice bills exactly AMOUNT regardless of how many
        hours are logged. The seed logs 10.0 unbilled Acme hours @ $100/hr
        ($1000 hourly), but the flat total must be $500.00 in DB and CSV."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            # Sanity: Acme has 10.0 unbilled hours (1+2+3+4), which at $100/hr
            # would be $1000 on an hourly invoice — clearly != the $500 flat.
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                logged_hours = conn.execute(
                    "SELECT COALESCE(SUM(hours),0) AS h FROM sessions "
                    "WHERE client_id = ? AND invoice_id IS NULL",
                    (acme_id,),
                ).fetchone()["h"]
            self.assertEqual(logged_hours, 10.0)

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "500",
                 "--description", "Fixed-scope engagement"],
                input="y\n",
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Flat fee: Fixed-scope engagement", result.output)
            self.assertIn("Total: $500.00", result.output)
            # The hourly labor echo must NOT appear for a flat invoice.
            self.assertNotIn("@ $100.00/hr =", result.output)

            # DB: total is exactly 500.00, billing_mode='flat', sessions billed,
            # expenses left UNBILLED (CR-9).
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                inv = conn.execute(
                    "SELECT invoice_number, total, billing_mode FROM invoices "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                billed_sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE client_id = ? AND invoice_id IS NOT NULL",
                    (acme_id,),
                ).fetchone()["n"]
                unbilled_sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE client_id = ? AND invoice_id IS NULL",
                    (acme_id,),
                ).fetchone()["n"]
                billed_expenses = conn.execute(
                    "SELECT COUNT(*) AS n FROM expenses "
                    "WHERE client_id = ? AND invoice_id IS NOT NULL",
                    (acme_id,),
                ).fetchone()["n"]
                unbilled_expenses = conn.execute(
                    "SELECT COUNT(*) AS n FROM expenses "
                    "WHERE client_id = ? AND invoice_id IS NULL",
                    (acme_id,),
                ).fetchone()["n"]

            self.assertEqual(round(inv["total"], 2), 500.00)
            self.assertEqual(inv["billing_mode"], "flat")
            self.assertEqual(billed_sessions, 4)   # all 4 Acme sessions billed
            self.assertEqual(unbilled_sessions, 0)
            self.assertEqual(billed_expenses, 0)   # CR-9: expenses untouched
            self.assertEqual(unbilled_expenses, 3)

            # CSV ledger: total 500.00, and the line item is the clean
            # description with NO "(0 hrs @ $0.00/hr)" suffix.
            with open(csv_path, newline="") as f:
                rows = list(_csv.DictReader(f))
            match = next(
                (r for r in rows if r.get("invoice_number") == inv["invoice_number"]),
                None,
            )
            self.assertIsNotNone(match, "expected flat invoice in CSV ledger")
            self.assertEqual(match["total"], "500.00")
            self.assertEqual(match["line_items"], "Fixed-scope engagement")
            self.assertNotIn("hrs @", match["line_items"])

    def test_flat_invoice_regenerates_to_same_total_as_single_flat_line(self):
        """Regenerating a flat invoice reproduces its $500.00 total from a
        single flat line item — never the hourly weekly grouping."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            create = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "500",
                 "--description", "Fixed-scope engagement"],
                input="y\n",
            )
            self.assertEqual(create.exit_code, 0, msg=create.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                inv_num = conn.execute(
                    "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
                ).fetchone()["invoice_number"]

            regen = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--regenerate", inv_num],
                input="y\n",
            )
            self.assertEqual(regen.exit_code, 0, msg=regen.output)
            # Flat regenerate reuses the stored total and reports a single line.
            self.assertIn("Flat invoice — reusing stored total $500.00", regen.output)
            self.assertIn("→ 1 weekly line items", regen.output)
            self.assertIn("Total: $500.00", regen.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                total = conn.execute(
                    "SELECT total FROM invoices WHERE invoice_number = ?",
                    (inv_num,),
                ).fetchone()["total"]
            self.assertEqual(round(total, 2), 500.00)

    def test_flat_requires_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()
            result = runner.invoke(zd.cli, ["invoice", "acme", "--flat", "500"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("--flat requires --description", result.output)

    def test_flat_conflicts_with_summarize_weeks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "500",
                 "--description", "x", "--summarize-weeks"],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("cannot be combined with --summarize-weeks", result.output)

    def test_flat_rejects_zero_negative_and_non_finite_amounts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            zero = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "0", "--description", "x"],
            )
            self.assertNotEqual(zero.exit_code, 0)
            self.assertIn("greater than 0", zero.output)

            neg = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "-100", "--description", "x"],
            )
            self.assertNotEqual(neg.exit_code, 0)
            self.assertIn("greater than 0", neg.output)

            for bad in ("inf", "nan", "-inf"):
                res = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--flat", bad, "--description", "x"],
                )
                self.assertNotEqual(res.exit_code, 0, msg=f"{bad!r} should fail")
                self.assertIn("finite", res.output)

            notnum = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "abc", "--description", "x"],
            )
            self.assertNotEqual(notnum.exit_code, 0)
            self.assertIn("must be a number", notnum.output)

            # Regression: a finite-but-enormous amount (scientific or a
            # fat-fingered long paste) exceeds Decimal's default 28-digit
            # context and used to escape as a raw InvalidOperation traceback
            # because quantize() sat outside the try/except. It must now fail
            # as a clean ClickException, never an unhandled exception.
            for huge in ("1e999", "1e400", "9" * 40):
                res = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--flat", huge, "--description", "x"],
                )
                self.assertNotEqual(res.exit_code, 0, msg=f"{huge!r} should fail")
                self.assertIn("too large", res.output, msg=f"{huge!r} output")
                # No raw decimal error leaked: the only surviving exception is
                # click's own SystemExit, not InvalidOperation.
                self.assertNotIsInstance(res.exception, InvalidOperation)

    def test_flat_invoice_help_documents_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            patcher = patch.object(zd, "ZD_DB", Path(tmpdir) / "zd.db")
            patcher.start()
            self.addCleanup(patcher.stop)
            runner = CliRunner()
            result = runner.invoke(zd.cli, ["invoice", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--flat AMOUNT", result.output)
        self.assertIn("--description", result.output)
