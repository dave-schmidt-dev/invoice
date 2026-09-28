"""Test package initializer — redirects the CLIs' log files during tests.

In production the CLIs log to ``/tmp/zd.log`` / ``/tmp/invoice.log`` (INV-1
owner-only files). ``zd.LOG_FILE`` / ``invoice.LOG_FILE`` resolve those paths
from the ``ZD_LOG_FILE`` / ``INVOICE_LOG_FILE`` env vars at import time, so
this module points them at a throwaway temp directory before any
``tests.test_*`` module runs ``import zd`` / ``import invoice``. That keeps the
suite from appending test noise to a developer's real operational log.

IMPORTANT — how this gets run: ``python -m unittest discover`` (from the repo
root) treats ``tests/`` as a package and executes this ``__init__`` first, so
the redirect is in place. ``discover -s tests`` does NOT import the package
``__init__`` (a documented unittest quirk), so run the suite from the repo root
(bare ``discover``). Under any other runner, set ``ZD_LOG_FILE`` /
``INVOICE_LOG_FILE`` explicitly to get the same isolation. ``setdefault`` below
leaves any such explicit override in place.

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
