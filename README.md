# invoice

Command-line invoice generator for creating PDF invoices and tracking them in a CSV log.

Run this project through `./invoice-wrapper` or the project virtual environment. Do not use bare `python3 invoice.py` unless the virtualenv is already activated, because your system Python may not have the repo dependencies installed.

## Features

**invoice.py:**
- Interactive invoice creation (`invoice.py new`)
- PDF generation with line items and totals
- Client profiles and reusable config in `~/.invoice_config.json`
- Quick file shortcuts for opening the configured ledger and a specific invoice PDF
- Invoice status tracking (`Draft`, `Sent`, `Paid`, `Overdue`)
- Filtered listing (`invoice.py list --status sent`)
- Safer file handling: atomic writes and lock-protected CSV updates
- Money handling with `Decimal` for consistent currency math

**zd (optional time tracker):**
- Log billable sessions and expenses per client via SQLite DB
- Edit sessions and expenses after the fact (`zd edit`, `zd edit-expense`)
- Automatic grouping of sessions into calendar-week line items
- PDF invoice generation that integrates with invoice.py
- Month-scoped invoicing for a single client (`zd invoice acme --month 2026-04`)
- Flat-rate invoicing for a fixed amount instead of hours×rate (`zd invoice acme --flat 1500 --description "Monthly retainer"`)
- Optional local Gemma weekly summaries for invoice line items (`--summarize-weeks`)
- Regenerate invoices after config or data corrections (`--regenerate`)
- Sync paid status back to invoice.py's CSV ledger, with an explicit paid date (`zd paid 2026-0001 --date 2026-05-01`)
- Reconcile the CSV ledger back to the authoritative DB (`zd reconcile [--fix]`)
- Auto-sync new clients to `~/.invoice_config.json` for invoice generation
- Automatic timestamped backups of DB, config, and CSV before writes (last 20 kept)
- Rotating debug logs on demand (`--debug`, written to `/tmp/zd.log`)
- Portable wrappers: both `invoice-wrapper` and `zd-wrapper` resolve symlinks for cross-computer compatibility

## Requirements

- Python 3.8+
- Dependencies in `requirements.txt`

Install:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Preferred command paths:

```bash
./invoice-wrapper ...
```

or, if you already activated the virtualenv:

```bash
python invoice.py ...
```

## Quick Start

1. Configure payee/client/payment/storage:

```bash
./invoice-wrapper config
```

2. Create an invoice:

```bash
./invoice-wrapper new
```

3. List invoices:

```bash
./invoice-wrapper list
```

4. Open the configured invoice ledger:

```bash
./invoice-wrapper --ledger
```

5. Open a specific invoice PDF:

```bash
./invoice-wrapper --invoice 2026-0001
```

6. Filter by status:

```bash
./invoice-wrapper list --status sent
```

7. Update status:

```bash
./invoice-wrapper status 2026-0001 Paid
```

## Using `invoice-wrapper`

The repo includes `invoice-wrapper`, which runs `invoice.py` through the virtual environment automatically.

Use this wrapper for normal CLI usage. It avoids the common failure mode where `python3` resolves to a system interpreter that does not have `click` or the other project dependencies installed.

Examples:

```bash
./invoice-wrapper --ledger
./invoice-wrapper --invoice 2026-0001
./invoice-wrapper list --status all
./invoice-wrapper new
./zd-wrapper invoice acme --month 2026-04
./zd-wrapper invoice acme --month 2026-04 --summarize-weeks
```

## Time Tracking with `zd`

This project includes **zd** (Zero Delta), an optional time tracker that logs billable sessions and expenses per client, then generates invoices by calling `invoice.py`'s PDF/CSV machinery directly.

zd maintains a SQLite database at `~/.zd.db` with clients, sessions, expenses, and invoice records. When you generate an invoice through zd, it creates the PDF via `invoice.py`, appends to the CSV ledger, and marks sessions/expenses as billed.

Setup and typical workflow:

```bash
# One-time: add a client with hourly rate
zd add-client acme "Acme Corp" 95.00

# As you work, log sessions
zd log acme 2.0 "sprint planning"
zd log acme 1.5 "dev work" --date 2026-03-18

# Log reimbursable expenses
zd expense acme 42.00 "domain renewal"

# Review unbilled totals
zd status

# Inspect line items before invoicing (one client)
zd sessions acme

# Inspect all clients at once
zd sessions
zd sessions --all   # include already-billed sessions

# Edit a session (use zd sessions to find the ID)
zd edit 14 --date 2026-03-20
zd edit 14 --hours 2.0 --notes "corrected"
zd edit-expense 3 --amount 50.00

# Generate invoice PDF (groups sessions by week)
zd invoice acme

# Generate a month-scoped invoice for one client
zd invoice acme --month 2026-04 --date 2026-04-30

# Generate month-scoped invoice with local Gemma weekly summaries
zd invoice acme --month 2026-04 --date 2026-04-30 --summarize-weeks

# Bill a fixed amount instead of hours×rate (e.g. a flat monthly retainer)
zd invoice acme --flat 1500 --description "Monthly retainer, April 2026"

# Regenerate an existing invoice (e.g. after fixing config or editing sessions)
zd invoice acme --regenerate 2026-0002

# Mark paid when check arrives (records today's date, or pass --date)
zd paid 2026-0001
zd paid 2026-0001 --date 2026-05-01

# Reconcile the CSV ledger against the authoritative DB (report only)
zd reconcile
zd reconcile --fix        # append missing rows / sync Paid status
```

The `zd sessions` command shows an ID column for each session — use these IDs with `zd edit`. Omitting the client argument shows sessions across all clients; `--all` includes already-billed sessions.

Run `zd --help` for a complete command reference with examples.

For invoice-specific options, run:

```bash
zd invoice --help
```

`zd invoice <client> --month YYYY-MM` limits a new invoice to unbilled sessions and expenses in that calendar month. Other unbilled work for the same client remains available for a later invoice.

Sessions are grouped into weekly line items (Monday-anchored, year-inclusive so weeks in different years never merge), but each line item is *labelled* with the dates actually worked: `Aug 5`, `Aug 3-7`, `Aug 31-Sep 2`. A label therefore never shows a date outside the invoiced period — a weekend-only week at the start of a month reads `Aug 1-2`, not `Week of Jul 27`.

`--summarize-weeks` adds one-line weekly summaries to invoice line items via a local OpenAI-compatible Gemma server. zd auto-starts the server when needed (`llama-server` serving a small Gemma GGUF) and shuts it down again on exit, so there is no manual server-management step. You can enable summaries by default in `~/.invoice_config.json`:

```json
"zd": {
  "weekly_summaries": {
    "enabled": true,
    "base_url": "http://127.0.0.1:8086",
    "model": "summarizer",
    "model_path": "/Users/you/models/narrator-bench/gemma-e2b/gemma-4-E2B-it-Q4_K_M.gguf",
    "log_path": "/tmp/zd-summary-server.log",
    "timeout_seconds": 30
  }
}
```

`model_path` points at a GGUF weights file. The default is a small Gemma 4 E2B (~2B active params, ~3GB at Q4_K_M) — fast enough that cold-starting the server per `zd invoice` run is unobtrusive, small enough that nothing lingers when zd exits.

Requirements:

- `llama-server` (current llama.cpp) on PATH. macOS: `brew install llama.cpp`.
- A GGUF weights file at `model_path`.

If a server is already responding at `base_url` when zd starts, zd will use it instead of spawning a new one (and won't shut it down at the end — that server belongs to someone else). If spawning fails, or if `/health` doesn't respond within the startup timeout, zd aborts cleanly. If the summary API call fails for any other reason, invoice generation falls back to the plain date-range labels.

Environment overrides: `ZD_SUMMARY_BASE_URL`, `ZD_SUMMARY_MODEL`, `ZD_SUMMARY_MODEL_PATH`, `ZD_SUMMARY_LOG`, `ZD_SUMMARY_TIMEOUT`.

### Flat-rate invoicing

`zd invoice <client> --flat AMOUNT --description "..."` bills a single fixed
amount instead of hours×rate — useful for a flat monthly retainer or a
milestone. The invoice total is exactly `AMOUNT` regardless of logged hours,
and `--description` (required) becomes the single line item. Scoped unbilled
sessions are still marked billed so they don't get re-billed later;
reimbursable expenses are left unbilled for a normal invoice. `--flat` cannot
be combined with `--summarize-weeks`. The amount is parsed as `Decimal` and
rejected if it is non-positive or too large to represent as money.

### Reconciling the ledger against the DB

The zd SQLite database is authoritative; the CSV ledger is a projection that
zd keeps converging to it automatically after each invoice/paid action. `zd
reconcile` reports any benign DB-ahead-of-CSV drift (a ledger row that never
got appended, or a DB `Paid` status not yet mirrored to the CSV) without
touching anything; `zd reconcile --fix` applies the repair (append the missing
row, sync the status). It only ever heals in the safe direction — it never
removes or rewrites an existing ledger row, so it cannot double-bill.

### Debug logging

Both CLIs log at `WARNING` and above to `/tmp/zd.log` and `/tmp/invoice.log`
(rotating, 1 MB × 2 backups, created `0600`). Pass `--debug` to either tool to
drop the threshold to `DEBUG` for that run. Logs are written to be free of
client identities, notes, amounts, and invoice numbers. Override the log path
with the `ZD_LOG_FILE` / `INVOICE_LOG_FILE` environment variables.

### Tab completion

Add to `~/.zshrc` (after `compinit`):

```zsh
autoload -Uz compinit && compinit
eval "$(_ZD_COMPLETE=zsh_source zd)"
```

For bash, add to `~/.bash_profile`:

```bash
eval "$(_ZD_COMPLETE=bash_source zd)"
```

Then `source` the file. Tab completion works for subcommands, flags, and client names.

### Worklog

zd appends a structured entry to a markdown worklog after every action. Configure the path in `~/.invoice_config.json`:

```json
"storage": {
  "worklog_file": "/path/to/your/Worklog.md"
}
```

If `worklog_file` is unset, logging is silently skipped.

## Commands

```text
./invoice-wrapper --ledger
./invoice-wrapper --invoice INVOICE_NUMBER
./invoice-wrapper config
./invoice-wrapper new [--date YYYY-MM-DD]
./invoice-wrapper list [--status all|draft|sent|paid|overdue]
./invoice-wrapper status INVOICE_NUMBER {draft|sent|paid|overdue}
```

Activated-venv equivalent:

```text
python invoice.py --ledger
python invoice.py --invoice INVOICE_NUMBER
python invoice.py config
python invoice.py new [--date YYYY-MM-DD]
python invoice.py list [--status all|draft|sent|paid|overdue]
python invoice.py status INVOICE_NUMBER {draft|sent|paid|overdue}
```

## Configuration

**invoice.py** runtime config is stored at:

```text
~/.invoice_config.json
```

See [`config.example.json`](config.example.json) for a full template.

**zd** maintains its own database at:

```text
~/.zd.db  (SQLite)
```

When zd generates invoices, it reads from `~/.invoice_config.json` to match clients and route PDF output, so both tools share the same config file.

Key config sections:

- `invoice_header`: title and optional logo path
- `payee`: your business/contact details
- `clients`: one or more client profiles
- `payment`: bank/payment instructions shown on invoice
- `storage`: ledger path and invoice output directory

The `storage.ledger_file` value is the exact file opened by `invoice.py --ledger`. Existing configs that still use `storage.csv_file` are migrated automatically.

Address fields support literal `\n` in input and are rendered as separate lines in the PDF.

## CSV Log Format

| Column | Description | Example |
|---|---|---|
| `invoice_number` | Invoice identifier | `2026-0001` |
| `date` | Invoice date (ISO-8601) | `2026-03-03` |
| `payee_name` | Payee name/company | `Acme Corporation` |
| `payer_name` | Client name/company | `Globex International` |
| `line_items` | Flattened line-item summary | `Website redesign (12 hrs @ $150.00/hr)` |
| `total` | Total amount (2 decimals, no currency symbol) | `2175.00` |
| `pdf_file` | Full path to generated PDF | `/Users/you/invoices/Acme_Invoice_2026-0001.pdf` |
| `status` | Invoice lifecycle status | `Draft` |

### Backups

Both `zd` and `invoice.py` automatically create timestamped backups before writing to `~/.zd.db`, `~/.invoice_config.json`, or the CSV ledger. Backups are named like `.zd.db.20260324-174500.bak` and the last 20 are kept per file; older ones are pruned automatically.

## Security and Data-Safety Notes

- Invoice numbers are validated to safe characters.
- Filename components are sanitized before writing PDFs.
- CSV writes are lock-protected to reduce race conditions.
- Critical rewrites use atomic replace patterns.
- CSV text fields are protected against spreadsheet formula injection.
- Automatic backups before every destructive write (last 20 retained).
- Debug/summary logs are created `0600` and written to be PII-free.

### Payment and banking details

Payment instructions — including any bank name, routing, and account numbers
shown on an invoice — live in `~/.invoice_config.json`, which stays **outside
the repository** (never committed; written `0600`). This tool keeps them in
that local config file rather than a dedicated secrets manager: it is a
grandfathered legacy config-based project, and migrating these values into a
secrets store is deferred to the next storage migration. Until then, protect
`~/.invoice_config.json` like any other credential file — do not copy it into
the repo, a backup that syncs to a shared location, or a paste buffer.

### No PII in the project

**Hard rule: no personally identifiable information or real-world client data
in the repository.** This applies to every committed artifact — source, tests,
fixtures, docs, HISTORY, TASKS, commit messages, branch names, and PR bodies.

Specifically, never commit:

- Real third-party names (people, contacts, counsel, counterparties).
- Real-world client / company names that identify an actual engagement. Use
  `acme` / `Acme Corp` and `globex` / `Globex Inc` as example fixtures.
- Real email addresses, phone numbers, or postal addresses (yours or others').
- Internal invoice numbers, contract terms, hourly rates, retainer amounts,
  or hours worked tied to a real engagement.
- Anything found in `~/.zd.db`, `~/.invoice_config.json`, or any generated
  PDF — those files live outside the repo deliberately and stay outside.

If you find yourself reaching for a real value to make an example concrete,
substitute a placeholder (`acme`, `globex`, `you@example.com`, `123 Main St`,
`$100.00`). `config.example.json` is the canonical source of sanitized
placeholders.

A version-controlled pre-commit hook (`hooks/pre-commit`) automatically blocks
any staged diff that adds content matching patterns in `hooks/pii-patterns.txt`.
Install it on this clone with the idempotent helper (or the raw `git config`
it wraps):

```bash
./scripts/install-hooks.sh        # points core.hooksPath at hooks/
# equivalent to: git config core.hooksPath hooks
```

The pre-commit hook only sees the staged diff. To audit everything already
committed, run the full-tree scanner:

```bash
./scripts/scan-pii.sh
```

Read its honesty header before trusting a clean result: it matches
names/tokens only and **cannot** detect financial identifiers (retainer
amounts, rates, invoice numbers), because sanctioned placeholders are
digit-shape-identical to real values. The durable control for those is keeping
operational files out of the repo (see `.gitignore`) plus human review — a
clean scan does not by itself mean "PII-safe."

If the hook blocks, fix the staged content — do not bypass with `--no-verify`.
When you discover a new leak vector, add a pattern to `hooks/pii-patterns.txt`
in the same commit that scrubs the existing content.

If PII has already been pushed, raise it immediately — removing it requires
rewriting history and a force-push, both of which need an explicit decision.

## Known Limitations

- On macOS, Apple Mail compose includes attachment automatically.
- On Linux/Windows, the default mail client opens via `mailto:` and may require manual attachment.

## Development

Run tests from the repository root:

```bash
./venv/bin/python -m unittest discover -v
```

If the virtualenv is already active:

```bash
python -m unittest discover -v
```

Run the suite from the repo root (bare `discover`, not `discover -s tests`) so
`tests/__init__.py` runs and redirects the CLIs' log files into a throwaway
temp directory instead of the real `/tmp/zd.log` and `/tmp/invoice.log`. To get
the same isolation under any other runner, set the `ZD_LOG_FILE` and
`INVOICE_LOG_FILE` environment variables explicitly.

## License

See [LICENSE](LICENSE).
