## 2026-05-30 (later)

- Aligned `zd invoice`'s CSV ledger and zd SQLite DB on the same `status`
  for fresh invoices: `save_to_csv` now accepts a `status` kwarg (default
  `"Draft"` so interactive `invoice.py new` is unchanged), and `cmd_invoice`
  passes `status="Sent"`. Added a regression test that asserts CSV row
  status equals zd DB row status after a `zd invoice` run.
- Replaced the heavyweight default summary model (`gemma-4-26b-a4b-it-4bit`,
  26B MoE on MLX, port 8001) with the small Gemma 4 E2B GGUF that megalodon
  already validated as "good enough for one-line summaries" (~2B active,
  ~3GB at Q4_K_M, served via `llama-server` on port 8086).
- Added an auto-start / auto-teardown context manager around `--summarize-
  weeks`: zd probes `<base_url>/health` first, spawns `llama-server` with
  megalodon's locked argv only if the server is down, waits for readiness,
  runs summarization, and tears the server back down on exit so nothing
  lingers. If a server is already up, zd uses it and leaves it alone.
- New env overrides for the summary path: `ZD_SUMMARY_BASE_URL`,
  `ZD_SUMMARY_MODEL`, `ZD_SUMMARY_MODEL_PATH`, `ZD_SUMMARY_LOG`. Config
  example and README updated accordingly.

## 2026-05-30

- Standardized the invoice PDF's section headers to title case (`From:`, `Bill To:`, `Payment Information:`), added a small gap between each header and its data lines, and tightened the data-line spacing so all three blocks have the same visual rhythm.
- Added a three-branch line-item renderer in `generate_pdf`: rows with `hours=0, rate=0, amount=0` span the full 170mm description width (no wasted `0.00 / $0.00 / $0.00` cells); rows with `hours=0, rate=0, amount>0` span 145mm with the amount column on the right (clean flat-fee/expense layout); standard hourly rows keep the existing 4-column layout. Driven by line-item shape — no schema change.
- Added a centered `Page X of Y` footer to invoice PDFs via an `_InvoicePDF` subclass override, using fpdf2's automatic `{nb}` substitution.
- Added look-ahead pagination (`_multi_cell_height`) that predicts each row's rendered height and forces a page break before drawing if the row wouldn't fit. Fixes a latent rendering bug where `multi_cell` auto-pagination could leave the Hours/Rate/Amount columns stranded at invalid coordinates (or a phantom shaded box) on the next page when a row crossed a page boundary.
- Documented the project-wide no-PII rule in the README's Security section. Test fixtures renamed off any real-world client names to match the `acme` / `Acme Corp` convention used everywhere else in the docs.

## 2026-05-14

- Added `zd invoice <client> --month YYYY-MM` to create month-scoped invoices for a single client while leaving other unbilled work untouched.
- Updated `zd invoice --help` and README examples to document month-scoped invoicing, and made help output avoid DB initialization/backups.
- Added regression coverage for month filtering, invalid month values, help output, and unchanged all-unbilled invoice behavior.
- Added Pillow to project requirements because configured invoice logos require it during PDF generation.
- Added `zd invoice --summarize-weeks` using the local Gemma OpenAI-compatible server to add concise weekly line-item summaries, plus config-backed defaults under `zd.weekly_summaries`, with fallback to plain week labels if summarization fails.
- Documented the new `zd invoice` month filter and weekly summary options in the README, including the `zd.weekly_summaries` config block.

## 2026-03-31

- Fixed CSV ledger crash when the file has non-standard headers (e.g. legacy payment-tracking spreadsheets exported from Numbers). All CSV read/write paths now preserve the file's actual headers instead of forcing `CSV_HEADERS`.
- Added `_read_csv_with_headers()` and `_csv_field_key()` helpers for header-agnostic CSV operations with fuzzy column name matching.
- Fixed append corruption when CSV files don't end with a trailing newline (common with Numbers/Excel exports).
- Switched all CSV reads to `utf-8-sig` encoding to handle BOM characters from spreadsheet exports.
- Fixed config `ledger_file` pointing to the wrong CSV file.

## 2026-03-14

- Added root CLI shortcuts: `--ledger` opens the configured invoice ledger and `--invoice INVOICE_NUMBER` opens the exact PDF path recorded for that invoice.
- Made the ledger path explicit in config as `storage.ledger_file` while keeping backward compatibility with legacy `storage.csv_file` values.
- Added regression coverage for ledger-path migration, invoice PDF lookup, and the new shortcut-flag behavior.
- Updated the docs to make virtualenv-backed execution explicit: prefer `./invoice-wrapper` for normal use, or run `python invoice.py ...` only from an activated project venv.
- Documented the dependency pitfall behind bare `python3 invoice.py`: system interpreters may not have repo packages such as `click` installed.

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
