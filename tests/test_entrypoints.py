"""Subprocess smoke tests for the repository script entry points."""

import tests  # noqa: F401 - HOME/log isolation guard (see tests/__init__.py)

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class EntrypointTests(unittest.TestCase):
    """Verify entry points work independently of the test process."""

    def _run(self, script, *args):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            home = workdir / "home"
            home.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "ZD_LOG_FILE": str(workdir / "zd.log"),
                    "INVOICE_LOG_FILE": str(workdir / "invoice.log"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / script), *args],
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return (
                result,
                (home / ".zd.db").is_file(),
                (workdir / ".zd.db").exists(),
            )

    def test_zd_help_lists_all_commands(self):
        result, _, _ = self._run("zd.py", "--help")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for command in (
            "clients",
            "log",
            "expense",
            "reconcile",
            "status",
            "sessions",
            "edit",
            "edit-expense",
            "invoice",
            "paid",
            "backfill",
            "add-client",
            "completion",
        ):
            self.assertIn(command, result.stdout)

    def test_zd_clients_creates_database_in_home_only(self):
        result, home_database_exists, workdir_database_exists = self._run(
            "zd.py", "clients"
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(home_database_exists)
        self.assertFalse(workdir_database_exists)

    def test_invoice_help_lists_all_commands(self):
        result, _, _ = self._run("invoice.py", "--help")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for command in ("config", "new", "status", "list"):
            self.assertIn(command, result.stdout)
