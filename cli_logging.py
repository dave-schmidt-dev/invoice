"""File-logger bootstrap shared by invoice.py and zd.py (INV-1)."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / ".logs"


def default_log_file(name):
    """Return the default log path for CLI ``name``: the gitignored ``.logs/<name>.log``."""
    return str(LOG_DIR / f"{name}.log")


def configure_file_logger(name, log_file, debug):
    """Configure the named logger (never the root logger, so we don't capture
    click/fpdf2/third-party log noise or spam stderr).

    Idempotent: repeated calls (e.g. across CliRunner invocations in tests)
    never stack duplicate handlers; only the level is refreshed on repeat
    calls. Level is DEBUG when ``debug`` is true, WARNING otherwise, set on
    both the logger and the handler.

    The parent directory (by default the project's ``.logs/``) is created
    owner-only if missing. INV-1 defense-in-depth: the file is best-effort
    chmod'd to owner-only (0600) after the handler creates it. Log CONTENT
    must stay PII-free regardless of the caller.
    """
    logger = logging.getLogger(name)
    logger.propagate = False
    level = logging.DEBUG if debug else logging.WARNING
    if not logger.handlers:
        Path(log_file).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_file, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        handler.setLevel(level)
        logger.addHandler(handler)
        try:
            os.chmod(log_file, 0o600)
        except OSError:
            pass
    else:
        for handler in logger.handlers:
            handler.setLevel(level)
    logger.setLevel(level)
    return logger
