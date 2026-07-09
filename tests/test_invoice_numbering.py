"""INV-5 gate: unique invoice numbers, no PDF overwrite, no orphan ledger rows.

`cmd_new` treats the CSV ledger as the authoritative store. It validates the
invoice number is unique against the ledger BEFORE rendering the PDF, renders
to a temp path, os.replace()s it into place, then appends the ledger row. This
guarantees:

  * A duplicate number aborts before any existing PDF's bytes change (INV-5 —
    no invoice's PDF is ever overwritten).
  * The happy path leaves the appended ledger row pointing at a PDF that
    actually exists (CR-4 — no ledger row ever references a missing PDF).

These tests drive the interactive `invoice.py new` command through CliRunner
against a temp ledger + invoices dir. They NEVER touch real files.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import invoice


class InvoiceNumberingTests(unittest.TestCase):
    def _config(self, tmpdir):
        """Minimal config pointing ledger + invoices dir at temp paths."""
        return {
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
            # Single client with no email -> no client-selection prompt and no
            # "open email client?" confirm prompt, keeping stdin deterministic.
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
            "payment": {"bank_name": "", "routing": "", "account": "", "description": ""},
            "storage": {
                "ledger_file": str(Path(tmpdir) / "invoices.csv"),
                "invoices_dir": str(Path(tmpdir) / "invoices"),
            },
        }

    def _new_invoice_input(self, invoice_number, invoice_date="2026-03-14"):
        """Build the stdin stream for one `invoice.py new` run.

        Prompt order in cmd_new:
          1. Invoice number
          2. Invoice date
          3. Payment description (blank -> default)
          4. (single client -> no prompt)
          5. Payment terms (blank -> default choice 2)
          6. Line items: description, hours, rate, then blank to finish
        """
        return "\n".join(
            [
                invoice_number,   # invoice number
                invoice_date,     # invoice date
                "",               # payment description -> default
                "",               # payment terms -> default (Net 30)
                "Consulting",     # line item description
                "2",              # hours
                "100",            # rate
                "",               # blank description -> finish line items
            ]
        ) + "\n"

    def _expected_pdf_path(self, config, invoice_number):
        """Mirror cmd_new's PDF path construction for a given number."""
        invoices_dir = config["storage"]["invoices_dir"]
        client_name = invoice._sanitize_filename_component("Acme Corp", "Client")
        safe_number = invoice._sanitize_filename_component(invoice_number, "invoice")
        return Path(invoices_dir) / f"{client_name}_Invoice_{safe_number}.pdf"

    def _read_rows(self, path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def _run_new(self, config, invoice_number, invoice_date="2026-03-14"):
        runner = CliRunner()
        with patch.object(invoice, "load_config", return_value=config):
            return runner.invoke(
                invoice.cli,
                ["new"],
                input=self._new_invoice_input(invoice_number, invoice_date),
            )

    # ------------------------------------------------------------------
    # INV-5: a duplicate number never overwrites an existing invoice's PDF.
    # ------------------------------------------------------------------
    def test_duplicate_number_aborts_without_touching_existing_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])
            invoices_dir = Path(config["storage"]["invoices_dir"])
            invoices_dir.mkdir(parents=True, exist_ok=True)

            dup_number = "2026-0001"

            # Seed a ledger row for dup_number whose PDF path matches the path
            # cmd_new would compute, and drop a SENTINEL PDF at that path.
            target_pdf = self._expected_pdf_path(config, dup_number)
            sentinel_bytes = b"%PDF-1.4 ORIGINAL INVOICE DO NOT OVERWRITE\n"
            target_pdf.write_bytes(sentinel_bytes)

            with open(ledger, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=invoice.CSV_HEADERS)
                writer.writeheader()
                writer.writerow(
                    {
                        "invoice_number": dup_number,
                        "date": "2026-03-01",
                        "payee_name": "Zero Delta LLC",
                        "payer_name": "Acme Corp",
                        "line_items": "Consulting (1 hrs @ $100.00/hr)",
                        "total": "100.00",
                        "pdf_file": str(target_pdf),
                        "status": "Sent",
                    }
                )
            original_ledger_bytes = ledger.read_bytes()

            # Attempt a brand-new invoice reusing the SAME number.
            result = self._run_new(config, dup_number)

            # It must abort with a non-zero exit and a clear message.
            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("already exists", result.output)

            # The original PDF's bytes are UNCHANGED — never overwritten.
            self.assertEqual(target_pdf.read_bytes(), sentinel_bytes)

            # The ledger is untouched: still exactly the one original row.
            self.assertEqual(ledger.read_bytes(), original_ledger_bytes)
            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["invoice_number"], dup_number)

            # No temp PDF (`.tmp-<pid>`) was left lying around.
            leftover_tmp = [p for p in invoices_dir.iterdir() if ".tmp-" in p.name]
            self.assertEqual(leftover_tmp, [], f"stray temp PDFs: {leftover_tmp}")

    # ------------------------------------------------------------------
    # Happy path: fresh number succeeds; PDF present; one ledger row that
    # references an existing PDF (CR-4 — no row ever points at a missing PDF).
    # ------------------------------------------------------------------
    def test_fresh_number_succeeds_with_pdf_and_single_ledger_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])
            invoices_dir = Path(config["storage"]["invoices_dir"])

            number = "2026-0007"
            result = self._run_new(config, number)

            self.assertEqual(result.exit_code, 0, msg=result.output)

            # Exactly one ledger row for the new invoice.
            self.assertTrue(ledger.exists())
            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["invoice_number"], number)
            self.assertEqual(row["total"], "200.00")

            # The PDF the row references EXISTS on disk (CR-4: no ledger row
            # ever points at a missing PDF).
            recorded_pdf = Path(row["pdf_file"])
            self.assertTrue(
                recorded_pdf.exists(),
                f"ledger row references a missing PDF: {recorded_pdf}",
            )
            self.assertEqual(recorded_pdf, self._expected_pdf_path(config, number))
            self.assertGreater(recorded_pdf.stat().st_size, 0)

            # No temp PDF was left behind by the temp-then-rename step.
            leftover_tmp = [p for p in invoices_dir.iterdir() if ".tmp-" in p.name]
            self.assertEqual(leftover_tmp, [], f"stray temp PDFs: {leftover_tmp}")

    # ------------------------------------------------------------------
    # Every committed ledger row references a PDF that exists (CR-4), across
    # a couple of sequential invoices.
    # ------------------------------------------------------------------
    def test_no_ledger_row_references_a_missing_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])

            for number in ("2026-0001", "2026-0002"):
                result = self._run_new(config, number)
                self.assertEqual(result.exit_code, 0, msg=result.output)

            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [r["invoice_number"] for r in rows], ["2026-0001", "2026-0002"]
            )
            for row in rows:
                recorded_pdf = Path(row["pdf_file"])
                self.assertTrue(
                    recorded_pdf.exists(),
                    f"ledger row {row['invoice_number']} references missing PDF: "
                    f"{recorded_pdf}",
                )


if __name__ == "__main__":
    unittest.main()
