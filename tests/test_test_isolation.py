"""The HOME/log isolation guard in tests/__init__.py cannot be bypassed.

``python -m unittest discover -s tests`` makes ``tests/`` the top-level
directory, so the package ``__init__`` never runs and test modules would import
``zd`` / ``invoice`` against the developer's real HOME (``~/.zd.db``, the real
config and ledger). Every test module therefore imports ``tests`` itself, before
any project module, which runs the guard under any runner.
"""

import tests  # noqa: F401 - HOME/log isolation guard (see tests/__init__.py)

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
PROJECT_MODULES = {p.stem for p in REPO_ROOT.glob("*.py")}


def _top_level(node):
    """Return the top-level module names a module-level import statement binds."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


class GuardImportTests(unittest.TestCase):
    def test_every_test_module_imports_the_guard_before_project_code(self):
        test_files = sorted(TESTS_DIR.glob("test_*.py"))
        self.assertTrue(test_files)
        for path in test_files:
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                guard_seen = False
                for node in tree.body:
                    names = _top_level(node)
                    if "tests" in names:
                        guard_seen = True
                    project = PROJECT_MODULES.intersection(names)
                    self.assertFalse(
                        project and not guard_seen,
                        f"{path.name} imports {sorted(project)} before `import tests`",
                    )
                self.assertTrue(guard_seen, f"{path.name} never imports `tests`")


class StartDirDiscoveryTests(unittest.TestCase):
    def test_discover_with_tests_as_start_dir_still_redirects_home_and_logs(self):
        # Load (never run) the suite the way `discover -s tests` does, with a
        # throwaway HOME standing in for the developer's real one and no log
        # overrides inherited from this process.
        with tempfile.TemporaryDirectory() as fake_home:
            env = {
                k: v for k, v in os.environ.items()
                if k not in ("ZD_LOG_FILE", "INVOICE_LOG_FILE")
            }
            env["HOME"] = fake_home
            probe = (
                "import json, os, unittest\n"
                "unittest.TestLoader().discover('tests')\n"
                "import zd, invoice\n"
                "print(json.dumps({'home': os.environ['HOME'], 'zd_db': str(zd.ZD_DB),"
                " 'config': str(invoice.CONFIG_FILE), 'zd_log': zd.LOG_FILE,"
                " 'invoice_log': invoice.LOG_FILE}))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            seen = json.loads(proc.stdout.strip().splitlines()[-1])
            fake = Path(fake_home).resolve()

            for key in ("home", "zd_db", "config"):
                self.assertNotIn(fake, Path(seen[key]).resolve().parents, msg=seen)
                self.assertNotEqual(Path(seen[key]).resolve(), fake, msg=seen)
            self.assertEqual(os.listdir(fake_home), [])
            for key in ("zd_log", "invoice_log"):
                self.assertIn("invoice-test-logs-", seen[key], msg=seen)


if __name__ == "__main__":
    unittest.main()
