"""Regression tests for HOME-derived invoice defaults."""

import tests  # noqa: F401 - HOME/log isolation guard (see tests/__init__.py)

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


INVOICE_PY = Path(__file__).resolve().parent.parent / "invoice.py"


def _fresh_load():
    spec = importlib.util.spec_from_file_location("invoice", INVOICE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FreshLoadTests(unittest.TestCase):
    def test_fresh_load_uses_current_home(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch.dict(os.environ, {"HOME": first}):
                _fresh_load()
            with patch.dict(os.environ, {"HOME": second}):
                module = _fresh_load()

                home = Path(second)
                self.assertEqual(module.CONFIG_FILE, home / ".invoice_config.json")
                self.assertEqual(
                    module.DEFAULT_CONFIG["storage"]["ledger_file"],
                    str(home / "invoices" / "invoices.csv"),
                )
                self.assertEqual(
                    module._normalize_storage_config({})["invoices_dir"],
                    str(home / "invoices"),
                )
