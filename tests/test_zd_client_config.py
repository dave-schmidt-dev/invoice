"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin

class ZdClientConfigTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def test_add_client_fails_closed_on_corrupt_config_and_leaves_it_untouched(self):
        """A CORRUPT ~/.invoice_config.json (exists but doesn't parse) must
        cause `zd add-client` to fail with a clean ClickException — never
        silently overwrite the file with a near-empty {clients: [name]}
        object (the original data-loss bug)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            corrupt_bytes = b'{ not json'
            config_path.write_bytes(corrupt_bytes)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "widget", "Widget Co", "150.00"]
            )

            self.assertNotEqual(result.exit_code, 0)
            # A clean ClickException, not a leaked traceback.
            self.assertNotIsInstance(result.exception, json.JSONDecodeError)
            # The corrupt file must be left BYTE-FOR-BYTE unchanged.
            self.assertEqual(config_path.read_bytes(), corrupt_bytes)

    def test_add_client_persists_new_client_and_preserves_existing_config(self):
        """A new client added to a VALID config round-trips, and the
        pre-existing payee/payment/other-client data must survive (not be
        wiped by the sync)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["payee"]["name"] = "Zero Delta LLC"
            config["payee"]["email"] = "billing@zerodelta.example"
            config["payment"]["bank_name"] = "First National"
            config["payment"]["routing"] = "111000025"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "widget", "Widget Co", "150.00"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            # New client persisted.
            names = [c.get("name") for c in updated["clients"]]
            self.assertIn("Widget Co", names)
            # Pre-existing client(s) preserved.
            self.assertIn("Acme Corp", names)
            # Pre-existing payee/payment data preserved (not wiped).
            self.assertEqual(updated["payee"]["name"], "Zero Delta LLC")
            self.assertEqual(updated["payee"]["email"], "billing@zerodelta.example")
            self.assertEqual(updated["payment"]["bank_name"], "First National")
            self.assertEqual(updated["payment"]["routing"], "111000025")

    def test_add_client_creates_config_when_genuinely_absent(self):
        """A genuinely MISSING config file (not corrupt, just never created)
        is the one case allowed to start from {}."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "zd.db"
            config_path = Path(tmpdir) / ".invoice_config.json"
            patchers = [
                patch.object(zd, "ZD_DB", db_path),
                patch.object(zd, "CONFIG_FILE", config_path),
                patch.dict(os.environ, {"HOME": tmpdir}),
            ]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)
            zd.init_db()
            self.assertFalse(config_path.exists())
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "widget", "Widget Co", "150.00"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual([c["name"] for c in config["clients"]], ["Widget Co"])

    def test_add_client_is_noop_when_already_present_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            before = config_path.read_text(encoding="utf-8")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "acme", "ACME CORP", "150.00"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            after = config_path.read_text(encoding="utf-8")
            self.assertEqual(before, after)
