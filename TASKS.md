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

### Task 1: Auto-start Ollama when `--summarize-weeks` runs
- **Status:** pending
- **Description:** `summarize_week_with_local_gemma` currently fails with `urlopen error [Errno 61] Connection refused` if Ollama is not already running, causing `zd invoice --summarize-weeks` to fall back silently to plain week labels. Expected behavior: detect the server is down, start it in the background, wait for the API to be ready, run summarization, and optionally shut it down if we started it.
- **Blocked by:** none
- **Tests:** integration test with Ollama stopped; assert summaries succeed end-to-end.
- **Done when:**
  - Running `zd invoice <client> --summarize-weeks` works with Ollama not running.
  - Summaries are produced (not the fallback labels).
  - If we started Ollama, we shut it down cleanly at the end.

### Task 2: Fix CSV ledger / zd DB `status` mismatch on invoice creation
- **Status:** pending
- **Description:** When `cmd_invoice` creates a new invoice, the zd DB row is inserted with `status='Sent'` (`zd.py:1146-1150`) but the CSV ledger row written by `inv_mod.save_to_csv` lands with `status='Draft'`. `zd paid <number>` syncs both to `Paid`, but until then the two ledgers disagree. Pick one default ('Sent' is more accurate for a generated-and-saved invoice) and align both writes.
- **Blocked by:** none
- **Tests:** unit test that creates an invoice via `cmd_invoice` and asserts CSV row and DB row have matching `status`.
- **Done when:**
  - New invoices show the same `status` in the CSV ledger and the zd DB.

### Task 3: Repair the project venv's broken pytest install
- **Status:** pending
- **Description:** `./venv/bin/python -m pytest` fails with `ImportError: cannot import name 'ExceptionInfo' from '_pytest._code'`. The `_pytest/_code/` directory in the venv contains only a `__pycache__` with no source files — an incomplete install. Tests are runnable via `./venv/bin/python -m unittest discover tests -v` as a workaround. Either repair the venv (`pip install --force-reinstall pytest`) or recreate it from `requirements.txt`.
- **Blocked by:** none
- **Tests:** `./venv/bin/python -m pytest tests/ -q` runs to completion.
- **Done when:**
  - pytest works again so CI/local invocations match.
