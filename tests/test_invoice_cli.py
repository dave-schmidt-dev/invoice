import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import invoice


class InvoiceCliTests(unittest.TestCase):
    def test_load_config_backfills_ledger_file_from_legacy_csv_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "invoice-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "storage": {
                            "csv_file": "~/legacy-invoices/invoices.csv",
                            "invoices_dir": "~/legacy-invoices",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(invoice, "CONFIG_FILE", config_path):
                config = invoice.load_config()

        expected_ledger = str(Path("~/legacy-invoices/invoices.csv").expanduser())
        self.assertEqual(config["storage"]["ledger_file"], expected_ledger)
        self.assertEqual(config["storage"]["csv_file"], expected_ledger)

    def test_resolve_invoice_pdf_path_uses_exact_path_from_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "invoices.csv"
            expected_pdf = Path(tmpdir) / "custom-location" / "invoice-2026-0001.pdf"
            expected_pdf.parent.mkdir(parents=True, exist_ok=True)
            expected_pdf.touch()

            with open(ledger_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=invoice.CSV_HEADERS)
                writer.writeheader()
                writer.writerow(
                    {
                        "invoice_number": "2026-0001",
                        "date": "2026-03-14",
                        "payee_name": "Zero Delta LLC",
                        "payer_name": "Acme Corp",
                        "line_items": "Consulting (2 hrs @ $100.00/hr)",
                        "total": "200.00",
                        "pdf_file": str(expected_pdf),
                        "status": "Draft",
                    }
                )

            config = {"storage": {"ledger_file": str(ledger_path), "invoices_dir": str(Path(tmpdir) / "pdfs")}}
            resolved = invoice._resolve_invoice_pdf_path(config, "2026-0001")

        self.assertEqual(resolved, expected_pdf)

    def test_cli_ledger_flag_opens_configured_ledger(self):
        runner = CliRunner()
        with patch.object(invoice, "load_config", return_value={"storage": {"ledger_file": "/tmp/invoices.csv"}}), patch.object(
            invoice, "_open_path", return_value=Path("/tmp/invoices.csv")
        ) as mock_open:
            result = runner.invoke(invoice.cli, ["--ledger"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Opened invoice ledger: /tmp/invoices.csv", result.output)
        mock_open.assert_called_once_with(Path("/tmp/invoices.csv"))

    def test_cli_invoice_flag_opens_exact_invoice_pdf(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "invoices.csv"
            expected_pdf = Path(tmpdir) / "records" / "Acme_Invoice_2026-0001.pdf"
            expected_pdf.parent.mkdir(parents=True, exist_ok=True)
            expected_pdf.touch()

            with open(ledger_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=invoice.CSV_HEADERS)
                writer.writeheader()
                writer.writerow(
                    {
                        "invoice_number": "2026-0001",
                        "date": "2026-03-14",
                        "payee_name": "Zero Delta LLC",
                        "payer_name": "Acme Corp",
                        "line_items": "Consulting (2 hrs @ $100.00/hr)",
                        "total": "200.00",
                        "pdf_file": str(expected_pdf),
                        "status": "Draft",
                    }
                )

            config = {"storage": {"ledger_file": str(ledger_path), "invoices_dir": str(Path(tmpdir) / "pdfs")}}
            with patch.object(invoice, "load_config", return_value=config), patch.object(
                invoice, "_open_path", return_value=expected_pdf
            ) as mock_open:
                result = runner.invoke(invoice.cli, ["--invoice", "2026-0001"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn(f"Opened invoice PDF: {expected_pdf}", result.output)
        mock_open.assert_called_once_with(expected_pdf)

    def test_cli_shortcut_flags_are_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(invoice.cli, ["--ledger", "--invoice", "2026-0001"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Use either --ledger or --invoice, not both.", result.output)


if __name__ == "__main__":
    unittest.main()
