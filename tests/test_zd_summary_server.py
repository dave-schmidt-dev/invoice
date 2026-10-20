"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin

class ZdSummaryServerTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def test_summary_server_context_uses_existing_server_without_spawning(self):
        """If /health already responds, we use the existing server as-is
        and do NOT terminate it on exit (it belongs to someone else)."""
        settings = {
            "base_url": "http://127.0.0.1:8086",
            "model": "summarizer",
            "model_path": "/dev/null",  # never read because server is "up"
            "log_path": "/tmp/zd-summary-server.log",
            "timeout_seconds": 30,
        }
        with patch.object(zd, "_server_alive", return_value=True) as alive, \
             patch.object(zd, "_spawn_summary_server") as spawn, \
             patch.object(zd, "_shutdown_summary_server") as shutdown:
            with zd._summary_server_context(settings):
                pass
        alive.assert_called()
        spawn.assert_not_called()
        shutdown.assert_not_called()

    def test_summary_server_context_spawns_and_tears_down_when_absent(self):
        """If /health is down on entry, we spawn llama-server, wait for
        readiness, run the block, and terminate the server on exit."""
        settings = {
            "base_url": "http://127.0.0.1:8086",
            "model": "summarizer",
            "model_path": "/dev/null",
            "log_path": "/tmp/zd-summary-server.log",
            "timeout_seconds": 30,
        }
        fake_proc = Mock()
        with patch.object(zd, "_server_alive", return_value=False), \
             patch.object(zd, "_spawn_summary_server", return_value=fake_proc) as spawn, \
             patch.object(zd, "_wait_for_summary_server") as wait, \
             patch.object(zd, "_shutdown_summary_server") as shutdown:
            with zd._summary_server_context(settings):
                pass
        spawn.assert_called_once()
        wait.assert_called_once_with("http://127.0.0.1:8086")
        shutdown.assert_called_once_with(fake_proc)

    def test_summary_server_context_shuts_down_on_readiness_timeout(self):
        """If llama-server never reports /health ready, the spawned
        process is killed before the SummaryServerError propagates."""
        settings = {
            "base_url": "http://127.0.0.1:8086",
            "model": "summarizer",
            "model_path": "/dev/null",
            "log_path": "/tmp/zd-summary-server.log",
            "timeout_seconds": 30,
        }
        fake_proc = Mock()
        with patch.object(zd, "_server_alive", return_value=False), \
             patch.object(zd, "_spawn_summary_server", return_value=fake_proc), \
             patch.object(zd, "_wait_for_summary_server",
                          side_effect=zd.SummaryServerError("timeout")), \
             patch.object(zd, "_shutdown_summary_server") as shutdown:
            with self.assertRaises(zd.SummaryServerError):
                with zd._summary_server_context(settings):
                    self.fail("body must not run when startup fails")
        shutdown.assert_called_once_with(fake_proc)

    def test_spawn_summary_server_creates_log_file_with_owner_only_perms(self):
        """The summary-server log can carry client session notes (INV-1), so
        it must be created 0600 regardless of the process umask. Drive the
        real _spawn_summary_server with shutil.which/model-path/Popen
        patched so no real llama-server is required."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "logs" / "summary.log"
            fake_proc = Mock()

            old_umask = os.umask(0o022)
            try:
                with patch.object(zd.shutil, "which", return_value="/usr/local/bin/llama-server"), \
                     patch.object(Path, "exists", return_value=True), \
                     patch("subprocess.Popen", return_value=fake_proc):
                    proc = zd._spawn_summary_server(
                        model_path="/fake/model.gguf",
                        base_url="http://127.0.0.1:8086",
                        alias="summarizer",
                        log_path=str(log_path),
                    )
            finally:
                os.umask(old_umask)

            self.assertIs(proc, fake_proc)
            self.assertTrue(log_path.exists())
            mode = log_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    def test_spawn_summary_server_tightens_perms_on_preexisting_looser_log(self):
        """os.open's mode only applies at CREATION time, so a log file that
        already exists with looser permissions (e.g. from before this
        hardening, or an odd umask) must still end up 0600 via the
        best-effort os.chmod."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "summary.log"
            log_path.write_text("old content\n", encoding="utf-8")
            os.chmod(log_path, 0o644)
            fake_proc = Mock()

            with patch.object(zd.shutil, "which", return_value="/usr/local/bin/llama-server"), \
                 patch.object(Path, "exists", return_value=True), \
                 patch("subprocess.Popen", return_value=fake_proc):
                zd._spawn_summary_server(
                    model_path="/fake/model.gguf",
                    base_url="http://127.0.0.1:8086",
                    alias="summarizer",
                    log_path=str(log_path),
                )

            mode = log_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")
