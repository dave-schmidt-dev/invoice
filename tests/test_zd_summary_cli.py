"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin

class ZdSummaryCliTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def test_invoice_falls_back_to_plain_labels_when_summary_server_unavailable(self):
        """End-to-end: if _summary_server_context raises SummaryServerError
        on entry (server can't start), `zd invoice` must still exit 0 and
        produce line items with plain date-range labels rather than
        crashing the whole invoice."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            with patch.object(
                zd, "_summary_server_context",
                side_effect=zd.SummaryServerError("llama-server not found on PATH"),
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30",
                     "--summarize-weeks"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(
                "Summary server unavailable (llama-server not found on PATH); "
                "using plain week labels.",
                result.output,
            )

            # The invoice still generated: the CSV ledger row's line_items
            # column carries the plain date-range labels (no " - <summary>"
            # suffix) for both weeks in the April scope. The labels come from
            # the session dates themselves, so an April-scoped invoice shows
            # "Apr 1" / "Apr 30" and never leaks the enclosing weeks' March
            # Monday.
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                inv_num = conn.execute(
                    "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
                ).fetchone()["invoice_number"]
            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            match = next(
                (r for r in csv_rows if r.get("invoice_number") == inv_num), None
            )
            self.assertIsNotNone(match, "expected the invoice in the CSV ledger")
            self.assertIn("Apr 1", match["line_items"])
            self.assertIn("Apr 30", match["line_items"])
            self.assertNotIn("Mar ", match["line_items"])

    def test_invoice_falls_back_to_plain_labels_when_summary_spawn_raises_oserror(self):
        """The summary-server spawn does mkdir/open/Popen, which can raise
        OSError (FileNotFoundError if llama-server vanished, PermissionError if
        the log dir is unwritable). That must degrade to plain labels, not
        crash `zd invoice` with a raw traceback."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            with patch.object(
                zd, "_summary_server_context",
                side_effect=FileNotFoundError("llama-server: No such file or directory"),
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30",
                     "--summarize-weeks"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIsInstance(result.exception, OSError)
            self.assertIn("using plain week labels.", result.output)
            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            self.assertTrue(csv_rows, "expected the invoice in the CSV ledger")
            self.assertIn("Apr 1", csv_rows[-1]["line_items"])

    def test_invoice_uses_weekly_summary_config_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["zd"]["weekly_summaries"]["enabled"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            with patch.object(
                zd,
                "summarize_week_with_local_gemma",
                return_value="Configured local summary",
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                    input="y\n",
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Summarizing weekly line items with local Gemma model", result.output)

    def test_invoice_summary_flag_overrides_disabled_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            with patch.object(
                zd,
                "summarize_week_with_local_gemma",
                return_value="Forced local summary",
            ):
                result = runner.invoke(
                    zd.cli,
                    [
                        "invoice",
                        "acme",
                        "--month",
                        "2026-04",
                        "--date",
                        "2026-04-30",
                        "--summarize-weeks",
                    ],
                    input="y\n",
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Summarizing weekly line items with local Gemma model", result.output)

    def test_is_loopback_host_recognizes_local_spellings(self):
        self.assertTrue(zd._is_loopback_host("127.0.0.1"))
        self.assertTrue(zd._is_loopback_host("127.5.5.5"))
        self.assertTrue(zd._is_loopback_host("localhost"))
        self.assertTrue(zd._is_loopback_host("LOCALHOST"))
        self.assertTrue(zd._is_loopback_host("::1"))
        self.assertFalse(zd._is_loopback_host("192.168.1.50"))
        self.assertFalse(zd._is_loopback_host("example.com"))
        self.assertFalse(zd._is_loopback_host(""))

    def test_invoice_warns_when_summary_endpoint_is_non_loopback(self):
        """Summaries enabled with a LAN base_url must warn exactly once that
        client session notes will leave the machine. _summary_server_context
        is patched to a no-op so no real server is contacted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["zd"]["weekly_summaries"]["enabled"] = True
            config["zd"]["weekly_summaries"]["base_url"] = "http://192.168.1.50:8086"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            @contextlib.contextmanager
            def _noop_server_context(settings):
                yield

            with patch.object(zd, "_summary_server_context", _noop_server_context), \
                 patch.object(
                     zd, "summarize_week_with_local_gemma", return_value="Remote summary"
                 ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("non-local endpoint", result.output)
            self.assertIn("http://192.168.1.50:8086", result.output)
            # Fires exactly once per run, not once per week (two weeks are in scope here).
            self.assertEqual(result.output.count("non-local endpoint"), 1)

    def test_invoice_does_not_warn_for_loopback_summary_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["zd"]["weekly_summaries"]["enabled"] = True
            config["zd"]["weekly_summaries"]["base_url"] = "http://127.0.0.1:8086"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            @contextlib.contextmanager
            def _noop_server_context(settings):
                yield

            with patch.object(zd, "_summary_server_context", _noop_server_context), \
                 patch.object(
                     zd, "summarize_week_with_local_gemma", return_value="Local summary"
                 ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIn("non-local endpoint", result.output)

    def test_invoice_does_not_warn_when_summaries_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            # Summaries disabled entirely, even though base_url is non-loopback.
            config["zd"]["weekly_summaries"]["enabled"] = False
            config["zd"]["weekly_summaries"]["base_url"] = "http://192.168.1.50:8086"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIn("non-local endpoint", result.output)
