## 2026-03-14

- Added root CLI shortcuts: `--ledger` opens the configured invoice ledger and `--invoice INVOICE_NUMBER` opens the exact PDF path recorded for that invoice.
- Made the ledger path explicit in config as `storage.ledger_file` while keeping backward compatibility with legacy `storage.csv_file` values.
- Added regression coverage for ledger-path migration, invoice PDF lookup, and the new shortcut-flag behavior.

## 2026-03-03

- Fixed `invoice list` table formatting to use dynamic column widths instead of tab characters, which resolves header/column misalignment for variable-length values.
- Hardened `invoice list` output with fixed column widths and ellipsis truncation so long values do not shift or break table alignment.
- Adjusted `invoice list` again to right-align the `Total` header/value column consistently and restore full PDF filenames (no truncation).
- Refactored core safety paths: secure Apple Mail launch (no script interpolation), sanitized invoice/PDF filename components, validated invoice numbers, and removed duplicate `list` command definitions.
- Improved data integrity: Decimal-based money handling, atomic config/CSV writes, and lock-protected CSV mutations to reduce race/truncation risk.
- Added sanity protections: spreadsheet-formula-safe CSV fields, literal `\\n` address splitting support, and defensive config parsing with clear error messages.
- Fixed `invoice-wrapper` so it reliably invokes `invoice.py` both inside and outside an active virtualenv.
- Added lightweight regression tests for key safety helpers in `tests/test_invoice_safety.py`.
- Rewrote `README.md` for accuracy and maintainability: updated command usage, removed obsolete workaround guidance, and corrected CSV format docs to include `status`.
