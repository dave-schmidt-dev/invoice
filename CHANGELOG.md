# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `invoice.py status` no longer changes the status of an invoice that is tracked in the
  authoritative zd database. It now exits non-zero without writing anything and tells you to
  use `zd paid <N>`, so the CSV ledger and `zd status` can no longer disagree. CSV-only
  (legacy) invoices and machines without a zd database behave as before. If the zd database
  exists but cannot be read, the command fails closed.
