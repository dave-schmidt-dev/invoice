# Task Tracking

Status key: pending | in progress | done | blocked

## 2026-05-14 - Month-Scoped Invoicing

### Task 1: Add April-only invoice support
- **Status:** done
- **Description:** Add a `zd invoice --month YYYY-MM` option so one client can be invoiced for a calendar month without billing other unbilled work.
- **Blocked by:** none
- **Tests:** `./venv/bin/python -m unittest tests.test_zd_invoice -v`
- **Done when:**
  - Month-scoped invoices include only matching client sessions and expenses in the selected month.
  - Other unbilled client and non-client work remains unbilled.
  - Help and README document the new option.

### Task 2: Add local weekly invoice summaries
- **Status:** done
- **Description:** Add optional `zd invoice --summarize-weeks` support that uses the local Gemma server to summarize weekly session notes into one-line invoice descriptions.
- **Blocked by:** none
- **Tests:** `./venv/bin/python -m unittest tests.test_zd_invoice -v`
- **Done when:**
  - Weekly line-item descriptions can include model summaries.
  - Missing or failed local model calls fall back to plain week labels.
  - Config can enable summaries by default.
  - Help and README document the local server requirement.

### Task 3: Sync docs with new invoice options
- **Status:** done
- **Description:** Update README, HISTORY, TASKS, and the config example to match the month-scoped invoice and local weekly summary options.
- **Blocked by:** none
- **Tests:** none
- **Done when:**
  - README reflects `--month`, `--summarize-weeks`, and weekly summary config.
  - HISTORY records the documentation sync.
  - The config example matches the current `zd.weekly_summaries` structure.

## 2026-05-30 - Follow-ups surfaced during May invoicing

### Task 1: Auto-start llama-server when `--summarize-weeks` runs
- **Status:** done (2026-05-30)
- **Description:** `summarize_week_with_local_gemma` previously failed with `urlopen error [Errno 61] Connection refused` if the local server was not already running, silently falling back to plain week labels. (Originally mis-tagged as "Ollama"; the actual runtime is `llama-server`.)
- **Resolution:** Added `_summary_server_context` in `zd.py` that probes `/health`, spawns `llama-server` with megalodon's locked argv only if needed, waits for readiness, and tears the server back down on exit. Switched the default model to the small Gemma 4 E2B GGUF (~2B active) for fast cold start. Tests cover the three branches (server already up, spawn-and-teardown, readiness-timeout cleanup).

### Task 2: Fix CSV ledger / zd DB `status` mismatch on invoice creation
- **Status:** done (2026-05-30)
- **Description:** zd DB row was inserted with `status='Sent'` (`zd.py:1146-1150`) but the CSV ledger row from `inv_mod.save_to_csv` defaulted to `status='Draft'`, leaving the two ledgers inconsistent until `zd paid` synced both to `Paid`.
- **Resolution:** Added a `status` kwarg to `save_to_csv` (default `"Draft"` so interactive `invoice.py new` is unchanged). `cmd_invoice` now passes `status="Sent"`. Regression test `test_invoice_status_matches_between_csv_ledger_and_zd_db` asserts CSV status equals DB status after a fresh `zd invoice` run.

### Task 3: Repair the project venv's broken pytest install
- **Status:** pending
- **Description:** `./venv/bin/python -m pytest` fails with `ImportError: cannot import name 'ExceptionInfo' from '_pytest._code'`. The `_pytest/_code/` directory in the venv contains only a `__pycache__` with no source files — an incomplete install. Tests are runnable via `./venv/bin/python -m unittest discover tests -v` as a workaround. Either repair the venv (`pip install --force-reinstall pytest`) or recreate it from `requirements.txt`.
- **Blocked by:** none
- **Tests:** `./venv/bin/python -m pytest tests/ -q` runs to completion.
- **Done when:**
  - pytest works again so CI/local invocations match.
