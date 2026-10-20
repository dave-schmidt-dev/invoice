"""Synthetic Git coverage for the staged and working-tree size checker."""

import tests  # noqa: F401 - HOME/log isolation guard

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _project_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "scripts" / "check_file_size.py").is_file():
            return candidate
    raise RuntimeError("could not locate scripts/check_file_size.py")


CHECKER = _project_root() / "scripts" / "check_file_size.py"


class FileSizeCheckerTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self._git("init", "-q")
        self._git("config", "user.email", "file-size@example.invalid")
        self._git("config", "user.name", "File Size Test")
        (self.repo / "scripts").mkdir()
        shutil.copy2(CHECKER, self.repo / "scripts" / "check_file_size.py")

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args, check=True):
        result = subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True
        )
        if check and result.returncode:
            self.fail(result.stderr)
        return result

    def _write_lines(self, name, count, final_newline=True):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "pass\n" * max(0, count - 1) + ("pass\n" if final_newline else "pass")
        path.write_text(text)
        return path

    def _stage(self, *paths):
        self._git("add", "--", *paths)

    def _commit(self, *paths):
        self._stage(*paths)
        self._git("commit", "-q", "-m", "fixture baseline")

    def _check(self, mode="--staged"):
        return subprocess.run(
            [sys.executable, "scripts/check_file_size.py", mode],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )

    def test_warning_and_failure_thresholds_use_actual_checker_output(self):
        self._write_lines("warning.py", 501)
        self._write_lines("limit.py", 800)
        self._write_lines("over.py", 801)
        self._stage("warning.py", "limit.py", "over.py")
        result = self._check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("warning.py has 501 lines (target 500)", result.stdout)
        self.assertIn("limit.py has 800 lines (target 500)", result.stdout)
        self.assertIn("over.py has 801 lines (maximum 800)", result.stderr)

    def test_final_unterminated_line_counts_toward_maximum(self):
        self._write_lines("unterminated.py", 800, final_newline=False)
        self._stage("unterminated.py")
        result = self._check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unterminated.py has 800 lines", result.stdout)
        # Add one more unterminated content line and verify the hard ceiling.
        with (self.repo / "unterminated.py").open("a") as stream:
            stream.write("\nlast")
        self._stage("unterminated.py")
        result = self._check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("has 801 lines (maximum 800)", result.stderr)

    def test_staged_mode_uses_index_content_and_all_mode_uses_working_content(self):
        self._write_lines("content.py", 801)
        self._stage("content.py")
        self._write_lines("content.py", 2)
        staged = self._check("--staged")
        self.assertEqual(staged.returncode, 1)
        self.assertIn("has 801 lines", staged.stderr)

        self._write_lines("content.py", 2)
        self._stage("content.py")
        self._write_lines("content.py", 801)
        staged = self._check("--staged")
        all_files = self._check("--all")
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual(all_files.returncode, 1)
        self.assertIn("has 801 lines", all_files.stderr)

    def test_reasoned_exception_is_staged_and_malformed_entries_are_rejected(self):
        self._write_lines("large.py", 801)
        (self.repo / ".file-size-exceptions").write_text(
            "large.py legacy retained for compatibility\n"
        )
        self._stage("large.py", ".file-size-exceptions")
        accepted = self._check()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("legacy exception", accepted.stdout)

        (self.repo / ".file-size-exceptions").write_text(
            "large.py\nlarge.py duplicate reason\ncap.py 801 old cap\n"
        )
        self._stage(".file-size-exceptions")
        rejected = self._check()
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("exception entry needs a reason", rejected.stderr)
        self.assertIn("duplicate exception path large.py", rejected.stderr)
        self.assertIn("line caps are no longer supported", rejected.stderr)

    def test_exception_removal_rechecks_all_tracked_sources(self):
        self._write_lines("large.py", 801)
        (self.repo / ".file-size-exceptions").write_text(
            "large.py legacy retained for compatibility\n"
        )
        self._commit("large.py", ".file-size-exceptions")
        (self.repo / ".file-size-exceptions").write_text("")
        self._stage(".file-size-exceptions")
        result = self._check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("large.py has 801 lines (maximum 800)", result.stderr)

    def test_spaces_renames_and_deletions_are_nul_safe(self):
        self._write_lines("old name.py", 3)
        self._commit("old name.py")
        (self.repo / "old name.py").rename(self.repo / "new name.py")
        self._write_lines("new name.py", 801)
        self._stage("old name.py", "new name.py")
        renamed = self._check()
        self.assertEqual(renamed.returncode, 1)
        self.assertIn("new name.py has 801 lines", renamed.stderr)

        self._git("commit", "-q", "-m", "oversized tracked fixture")
        (self.repo / "new name.py").unlink()
        self._stage("new name.py")
        deleted = self._check()
        self.assertEqual(deleted.returncode, 0, deleted.stderr)

    def test_git_errors_fail_closed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\necho synthetic git failure >&2\nexit 1\n")
        fake_git.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(fake_bin)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            env.pop(name, None)
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--all"],
            cwd=outside,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("synthetic git failure", result.stderr)


if __name__ == "__main__":
    unittest.main()
