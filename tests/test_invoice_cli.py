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

    # ------------------------------------------------------------------
    # Crash-hardening regressions (C1-C6)
    # ------------------------------------------------------------------

    def test_cmd_status_backfills_missing_status_column_on_legacy_ledger(self):
        # C1: a legacy ledger with no 'status' column used to blow up inside
        # _atomic_write_csv with "dict contains fields not in fieldnames".
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "invoices.csv"
            legacy_headers = [
                "invoice_number", "date", "payee_name", "payer_name",
                "line_items", "total", "pdf_file",
            ]
            with open(ledger_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=legacy_headers)
                writer.writeheader()
                writer.writerow(
                    {
                        "invoice_number": "2026-0001",
                        "date": "2026-03-14",
                        "payee_name": "Zero Delta LLC",
                        "payer_name": "Acme Corp",
                        "line_items": "Consulting (2 hrs @ $100.00/hr)",
                        "total": "200.00",
                        "pdf_file": str(Path(tmpdir) / "invoice.pdf"),
                    }
                )

            config = {"storage": {"ledger_file": str(ledger_path), "invoices_dir": tmpdir}}
            with patch.object(invoice, "load_config", return_value=config):
                result = runner.invoke(invoice.cli, ["status", "2026-0001", "Paid"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIsNone(result.exception)
            self.assertIn("status updated to: Paid", result.output)

            with open(ledger_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["status"], "Paid")

    def test_multi_cell_height_whitespace_only_description_is_noop(self):
        # C2: a whitespace-only description made text.split() empty, which
        # used to leave `current = words[0]` raising IndexError.
        pdf = invoice._InvoicePDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "", 10)

        height = invoice._multi_cell_height(pdf, "   ", 90, line_h=5)

        self.assertEqual(height, 5)

    def test_run_config_setup_handles_missing_payee_section(self):
        # C3: a config missing the 'payee' section entirely used to raise
        # KeyError on config["payee"] during the wizard.
        config_without_payee = {
            "invoice_header": {"title": "INVOICE", "logo_path": ""},
            "clients": [dict(invoice._DEFAULT_CLIENT)],
            "payment": {"bank_name": "", "routing": "", "account": "", "description": ""},
            "storage": {
                "ledger_file": str(invoice._DEFAULT_LEDGER),
                "invoices_dir": str(invoice._DEFAULT_INVOICES_DIR),
            },
        }

        # Answer every prompt with the default so the wizard completes.
        with patch.object(invoice, "save_config"), patch("click.prompt", side_effect=lambda *a, **k: k.get("default", "")):
            with patch("click.confirm", return_value=False):
                result_config = invoice._run_config_setup(config_without_payee)

        self.assertIn("payee", result_config)
        self.assertEqual(result_config["payee"]["name"], "")

    def test_get_next_invoice_number_handles_none_invoice_cell(self):
        # C4: a None invoice-number cell (e.g. a blank CSV row) used to raise
        # TypeError inside "-" in invoice_str.
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "invoices.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=invoice.CSV_HEADERS)
                writer.writeheader()
                writer.writerow({h: "" for h in invoice.CSV_HEADERS})
                f.write(",,,,,,," + "\n")  # row with no invoice_number value at all

            next_number = invoice.get_next_invoice_number(str(csv_path))

        current_year = invoice.date.today().year
        self.assertEqual(next_number, f"{current_year}-0001")

    def test_load_config_rejects_non_dict_json(self):
        # C5: valid JSON that isn't an object (e.g. a top-level list) used to
        # crash later on dict-style indexing/access.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "invoice-config.json"
            config_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

            with patch.object(invoice, "CONFIG_FILE", config_path):
                with self.assertRaises(invoice.click.ClickException) as ctx:
                    invoice.load_config()

        self.assertIn("not a JSON object", str(ctx.exception))

    def test_read_csv_with_headers_rejects_non_utf8_ledger(self):
        # C6: a non-UTF-8 ledger file used to raise a raw UnicodeDecodeError.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "invoices.csv"
            with open(ledger_path, "wb") as f:
                f.write(b"invoice_number,date\n2026-0001,\xff\xfe invalid \n")

            with self.assertRaises(invoice.click.ClickException) as ctx:
                invoice._read_csv_with_headers(ledger_path)

        self.assertIn(str(ledger_path), str(ctx.exception))

    def test_save_config_round_trips_under_shared_lock(self):
        """H.1: save_config now wraps its write in _file_lock (the same lock
        zd.py's _sync_client_to_config takes on the same CONFIG_FILE path via
        inv_mod._file_lock, so the two writers serialize instead of racing).
        The lock is best-effort/cross-process only; this just confirms
        save_config -> load_config still round-trips correctly with it in
        place."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(json.dumps(invoice.DEFAULT_CONFIG))
            config["payee"]["name"] = "Zero Delta LLC"
            config["clients"][0]["name"] = "Acme Corp"

            with patch.object(invoice, "CONFIG_FILE", config_path):
                invoice.save_config(config)
                loaded = invoice.load_config()

            self.assertEqual(loaded["payee"]["name"], "Zero Delta LLC")
            self.assertEqual(loaded["clients"][0]["name"], "Acme Corp")
            # Written with owner-only permissions (0600), unchanged by the lock wrap.
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
