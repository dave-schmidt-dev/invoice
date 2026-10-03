# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The pre-commit hook also runs `ruff check --select F401` on staged Python files so unused imports and stale re-exports cannot regrow (skipped with a notice when ruff is not installed).

### Changed

- Removed unused re-exports and imports left behind by the module split from `invoice.py` and `zd.py`; `invoice.py` is back under 800 lines. No behavior change.
- Ledger CSV row patches (`invoice.py status`, `zd paid`, `zd reconcile --fix`, `zd invoice --regenerate`) now share one helper, `invoice_ledger.update_ledger_rows`, so the lock, backup and atomic-write sequence lives in one place. Behavior is unchanged.
- The four copies of the fresh-load-invoice.py snippet in zd now share `zd._load_invoice()`. Behavior is unchanged.
- `zd.py` and `invoice.py` share one file-logger bootstrap, `cli_logging.configure_file_logger`. Behavior is unchanged.

### Fixed

- `invoice.py status` no longer changes the status of an invoice that is tracked in the
  authoritative zd database. It now exits non-zero without writing anything and tells you to
  use `zd paid <N>`, so the CSV ledger and `zd status` can no longer disagree. CSV-only
  (legacy) invoices and machines without a zd database behave as before. If the zd database
  exists but cannot be read, the command fails closed.
