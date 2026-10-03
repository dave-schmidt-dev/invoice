"""INV-1: the CLIs' default log files live in the project's gitignored .logs/.

They used to default to /tmp/zd.log and /tmp/invoice.log, a shared directory
outside the project. ``ZD_LOG_FILE`` / ``INVOICE_LOG_FILE`` still override.
"""

import tests  # noqa: F401 - HOME/log isolation guard (see tests/__init__.py)

import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cli_logging

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / ".logs"


class DefaultLogLocationTests(unittest.TestCase):
    def test_default_log_file_is_in_the_project_logs_dir(self):
        self.assertEqual(Path(cli_logging.default_log_file("zd")), LOG_DIR / "zd.log")

    def test_cli_modules_default_to_project_logs_without_overrides(self):
        with tempfile.TemporaryDirectory() as scratch_home:
            env = {
                k: v for k, v in os.environ.items()
                if k not in ("ZD_LOG_FILE", "INVOICE_LOG_FILE")
            }
            env["HOME"] = scratch_home
            probe = (
                "import json, zd, invoice\n"
                "print(json.dumps([zd.LOG_FILE, invoice.LOG_FILE]))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        zd_log, invoice_log = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(Path(zd_log), LOG_DIR / "zd.log")
        self.assertEqual(Path(invoice_log), LOG_DIR / "invoice.log")

    def test_logs_dir_is_gitignored_by_the_tracked_gitignore(self):
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".logs/", lines)


class ConfigureFileLoggerTests(unittest.TestCase):
    def test_creates_a_missing_log_directory_and_owner_only_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "fresh" / "nested" / "probe.log"
            logger = cli_logging.configure_file_logger(
                "invoice-test-log-location", str(log_file), debug=False
            )
            try:
                logger.warning("probe")
                self.assertTrue(log_file.exists())
                self.assertEqual(log_file.stat().st_mode & 0o777, 0o600)
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)
                logging.Logger.manager.loggerDict.pop("invoice-test-log-location", None)


if __name__ == "__main__":
    unittest.main()
