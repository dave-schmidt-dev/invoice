"""Test package initializer — redirects the CLIs' log files during tests.

In production the CLIs log to ``/tmp/zd.log`` / ``/tmp/invoice.log`` (INV-1
owner-only files). ``zd.LOG_FILE`` / ``invoice.LOG_FILE`` resolve those paths
from the ``ZD_LOG_FILE`` / ``INVOICE_LOG_FILE`` env vars at import time, so
this module points them at a throwaway temp directory before any
``tests.test_*`` module runs ``import zd`` / ``import invoice``. That keeps the
suite from appending test noise to a developer's real operational log.

How this always runs: bare ``python -m unittest discover`` (from the repo
root) imports this package ``__init__`` first. ``discover -s tests`` does NOT
(it makes ``tests/`` the top-level directory), so every ``tests/test_*.py``
module also starts with ``import tests`` before any project import; that runs
this guard under any runner. ``tests/test_test_isolation.py`` enforces the
import order. ``setdefault`` below leaves an explicit ``ZD_LOG_FILE`` /
``INVOICE_LOG_FILE`` override in place.

``zd`` and ``invoice`` compute ``~/.zd.db`` and ``~/.invoice_config.json``
from ``Path.home()`` at import time. Redirecting HOME here keeps imports from
touching a developer's real data.
"""

import atexit
import os
import pwd
import shutil
import tempfile
from pathlib import Path

_LOG_DIR = tempfile.mkdtemp(prefix="invoice-test-logs-")
os.environ.setdefault("ZD_LOG_FILE", os.path.join(_LOG_DIR, "zd.log"))
os.environ.setdefault("INVOICE_LOG_FILE", os.path.join(_LOG_DIR, "invoice.log"))
atexit.register(lambda: shutil.rmtree(_LOG_DIR, ignore_errors=True))

_HOME_DIR = tempfile.mkdtemp(prefix="invoice-test-home-")
_ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
os.environ["HOME"] = _HOME_DIR
atexit.register(lambda: shutil.rmtree(_HOME_DIR, ignore_errors=True))

if Path.home().resolve() == _ACCOUNT_HOME.resolve():
    raise RuntimeError("tests must redirect HOME before importing zd or invoice")
