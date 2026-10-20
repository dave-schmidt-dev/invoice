"""Effective hook tests using disposable repositories and synthetic tools."""

import tests  # noqa: F401 - HOME/log isolation guard

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _project_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "hooks" / "pre-commit").is_file():
            return candidate
    raise RuntimeError("could not locate project hooks")


PROJECT = _project_root()


class QualityHookTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.remote = Path(self.temp.name) / "remote.git"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "quality-hooks@example.invalid")
        self._git("config", "user.name", "Quality Hook Test")
        self._git("config", "commit.gpgsign", "false")
        (self.repo / "hooks").mkdir()
        (self.repo / "scripts").mkdir()
        shutil.copy2(PROJECT / "hooks" / "pre-commit", self.repo / "hooks" / "pre-commit")
        self.assertTrue(os.access(PROJECT / "hooks" / "pre-push", os.X_OK))
        shutil.copy2(PROJECT / "hooks" / "pre-push", self.repo / "hooks" / "pre-push")
        shutil.copy2(
            PROJECT / "scripts" / "check_file_size.py",
            self.repo / "scripts" / "check_file_size.py",
        )
        os.chmod(self.repo / "hooks" / "pre-commit", 0o755)
        os.chmod(self.repo / "hooks" / "pre-push", 0o755)
        (self.repo / "hooks" / "pii-patterns.txt").write_text("zzsyntheticprivate\n")
        (self.repo / "baseline.txt").write_text("baseline\n")
        self._git("add", "hooks/pii-patterns.txt", "baseline.txt")
        self._git("commit", "-q", "-m", "fixture baseline")
        self._git("config", "core.hooksPath", "hooks")
        self.bin = self.repo / "fake-bin"
        self.bin.mkdir()
        self._write_fake_ruff()

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args, env=None, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            self.fail(result.stderr)
        return result

    def _environment(self):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return env

    def _write_fake_ruff(self):
        ruff = self.bin / "ruff"
        ruff.write_text(
            "#!/bin/sh\n"
            "input=$(cat)\n"
            "case \"$input\" in\n"
            "  *RUFF_TOOL_ERROR*) echo 'synthetic Ruff configuration output' >&2; exit 2;;\n"
            "  *STAGED_BAD*) echo \"$input\" >&2; exit 1;;\n"
            "  *zzsyntheticprivate*) echo \"F401: $input\" >&2; exit 1;;\n"
            "esac\n"
            "echo \"checked stdin: $input\"\n"
        )
        os.chmod(ruff, 0o755)

    def _write_fake_python(self, status):
        interpreter = self.repo / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text(
            "#!/bin/sh\n"
            "printf 'cwd=%s\\n' \"$PWD\"\n"
            "printf 'args=%s\\n' \"$*\"\n"
            f"exit {status}\n"
        )
        os.chmod(interpreter, 0o755)
        return interpreter

    def _commit_staged(self, message="fixture change"):
        return self._git("commit", "-m", message, env=self._environment(), check=False)

    def _prepare_remote(self):
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self._git("branch", "-M", "main")
        self._git("remote", "add", "origin", str(self.remote))
        (self.repo / "push-change.txt").write_text("push change\n")
        self._git("add", "push-change.txt")
        self._git("commit", "-q", "-m", "push fixture")
        self._write_fake_python(0)

    def test_precommit_lints_staged_blob_not_working_tree(self):
        path = self.repo / "module.py"
        path.write_text("STAGED_BAD\n")
        self._git("add", "module.py")
        path.write_text("clean working copy\n")
        result = self._commit_staged()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ruff found staged unused imports (F401)", result.stderr)
        self.assertIn("module.py", result.stderr)
        self.assertNotIn("STAGED_BAD", result.stdout + result.stderr)

        path.write_text("clean staged copy\n")
        self._git("add", "module.py")
        path.write_text("STAGED_BAD working-only\n")
        result = self._commit_staged()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ruff_failure_does_not_echo_private_staged_comment(self):
        path = self.repo / "module.py"
        path.write_text("import zzsyntheticprivate\n")
        self._git("add", "module.py")
        result = self._commit_staged()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ruff found staged unused imports (F401)", result.stderr)
        self.assertNotIn("zzsyntheticprivate", result.stdout + result.stderr)

    def test_ruff_tool_failure_is_distinguished_from_lint_failure(self):
        path = self.repo / "module.py"
        path.write_text("RUFF_TOOL_ERROR\n")
        self._git("add", "module.py")
        result = self._commit_staged()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ruff tool/config failure", result.stderr)
        self.assertIn("(exit 2)", result.stderr)
        self.assertIn("module.py", result.stderr)
        self.assertNotIn("Ruff found staged unused imports (F401)", result.stderr)
        self.assertNotIn("synthetic Ruff configuration output", result.stdout + result.stderr)
        self.assertNotIn("RUFF_TOOL_ERROR", result.stdout + result.stderr)

    def test_precommit_missing_size_checker_fails_closed(self):
        (self.repo / "scripts" / "check_file_size.py").unlink()
        (self.repo / "clean.txt").write_text("safe synthetic content\n")
        self._git("add", "clean.txt")
        result = self._commit_staged()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staged source files exceed the size policy", result.stderr)

    def test_precommit_blocks_oversized_staged_source(self):
        path = self.repo / "oversized.py"
        path.write_text("pass\n" * 801)
        self._git("add", "oversized.py")
        result = self._commit_staged()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("maximum 800", result.stderr)

    def test_pre_push_runs_verbose_suite_from_repo_root_before_successful_push(self):
        self._prepare_remote()
        result = self._git("push", "-u", "origin", "main", env=self._environment(), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cwd={self.repo.resolve()}", result.stdout)
        self.assertIn("args=-m unittest discover -v", result.stdout)
        remote_head = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/main"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        local_head = self._git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(remote_head, local_head)

    def test_pre_push_suite_failure_blocks_remote_update(self):
        self._prepare_remote()
        self._write_fake_python(17)
        result = self._git("push", "origin", "main", env=self._environment(), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unittest discovery failed with exit 17", result.stderr)
        missing_remote_ref = subprocess.run(
            ["git", "--git-dir", str(self.remote), "show-ref", "--verify", "refs/heads/main"],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(missing_remote_ref.returncode, 0)

    def test_pre_push_missing_interpreter_blocks_with_actionable_error(self):
        self._prepare_remote()
        (self.repo / "venv" / "bin" / "python").unlink()
        result = self._git("push", "origin", "main", env=self._environment(), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project interpreter is missing", result.stderr)
        self.assertIn("Create the project venv", result.stderr)


if __name__ == "__main__":
    unittest.main()
