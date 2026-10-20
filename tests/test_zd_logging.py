"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import zd

class ZdLoggingTests(unittest.TestCase):

    def tearDown(self):
        # The "zd" logger is a module-level named singleton, so a handler
        # left over from one test (often pointed at a tmpdir that is about
        # to be deleted) would silently break unrelated tests. Always strip
        # every handler we may have added.
        logger = logging.getLogger("zd")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def _seed_minimal_client(self, tmpdir):
        """Seed just enough (one client) for `zd log` to succeed."""
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
        with zd.get_conn() as conn:
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("zztopclient", "ZZTOPCLIENT", 100.00),
            )
        return db_path, config_path

    def test_debug_flag_sets_zd_logger_to_debug_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_minimal_client(tmpdir)
            log_path = Path(tmpdir) / "zd-debug.log"
            with patch.object(zd, "LOG_FILE", str(log_path)):
                runner = CliRunner()
                result = runner.invoke(zd.cli, ["--debug", "clients"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(logging.getLogger("zd").level, logging.DEBUG)

    def test_without_debug_flag_zd_logger_is_warning_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_minimal_client(tmpdir)
            log_path = Path(tmpdir) / "zd-warning.log"
            with patch.object(zd, "LOG_FILE", str(log_path)):
                runner = CliRunner()
                result = runner.invoke(zd.cli, ["clients"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(logging.getLogger("zd").level, logging.WARNING)

    def test_rotating_file_handler_configured_with_expected_limits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_minimal_client(tmpdir)
            log_path = Path(tmpdir) / "zd-rotate.log"
            with patch.object(zd, "LOG_FILE", str(log_path)):
                runner = CliRunner()
                result = runner.invoke(zd.cli, ["--debug", "clients"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            handlers = [
                h for h in logging.getLogger("zd").handlers
                if isinstance(h, RotatingFileHandler)
            ]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].maxBytes, 1024 * 1024)
            self.assertEqual(handlers[0].backupCount, 2)

    def test_repeated_invocations_do_not_stack_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_minimal_client(tmpdir)
            log_path = Path(tmpdir) / "zd-idempotent.log"
            with patch.object(zd, "LOG_FILE", str(log_path)):
                runner = CliRunner()
                result1 = runner.invoke(zd.cli, ["--debug", "clients"])
                result2 = runner.invoke(zd.cli, ["--debug", "clients"])
            self.assertEqual(result1.exit_code, 0, msg=result1.output)
            self.assertEqual(result2.exit_code, 0, msg=result2.output)
            handlers = [
                h for h in logging.getLogger("zd").handlers
                if isinstance(h, RotatingFileHandler)
            ]
            self.assertEqual(len(handlers), 1)

    def test_debug_logging_never_leaks_client_name_or_notes_inv1(self):
        """INV-1 regression: a distinctive client name and session notes
        string must never appear in the log file, even at DEBUG level."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_minimal_client(tmpdir)
            log_path = Path(tmpdir) / "zd-inv1.log"
            with patch.object(zd, "LOG_FILE", str(log_path)):
                runner = CliRunner()
                result = runner.invoke(
                    zd.cli,
                    ["--debug", "log", "zztopclient", "1.5", "SECRETNOTE12345"],
                )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertTrue(log_path.exists())
            log_contents = log_path.read_text(encoding="utf-8")
            self.assertNotIn("SECRETNOTE12345", log_contents)
            self.assertNotIn("ZZTOPCLIENT", log_contents)
            self.assertNotIn("zztopclient", log_contents)

    def test_log_file_perms_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_minimal_client(tmpdir)
            log_path = Path(tmpdir) / "zd-perms.log"
            with patch.object(zd, "LOG_FILE", str(log_path)):
                runner = CliRunner()
                result = runner.invoke(zd.cli, ["--debug", "clients"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertTrue(log_path.exists())
            mode = log_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
