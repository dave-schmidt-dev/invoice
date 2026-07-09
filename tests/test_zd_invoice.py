import contextlib
import json
import os
import sys
import tempfile
import unittest
from decimal import InvalidOperation
from pathlib import Path
from unittest.mock import Mock, patch

import click
from click.testing import CliRunner

import zd


class ZdInvoiceTests(unittest.TestCase):
    def _write_config(self, tmpdir):
        config_path = Path(tmpdir) / ".invoice_config.json"
        invoices_dir = Path(tmpdir) / "invoices"
        ledger_file = Path(tmpdir) / "invoices.csv"
        config = {
            "invoice_header": {"title": "INVOICE", "logo_path": ""},
            "payee": {
                "name": "Zero Delta LLC",
                "address": "",
                "city": "",
                "state": "",
                "zip": "",
                "email": "",
                "phone": "",
            },
            "clients": [
                {
                    "name": "Acme Corp",
                    "address": "",
                    "city": "",
                    "state": "",
                    "zip": "",
                    "contact": "",
                }
            ],
            "payment": {
                "bank_name": "",
                "routing": "",
                "account": "",
                "description": "",
            },
            "storage": {
                "ledger_file": str(ledger_file),
                "invoices_dir": str(invoices_dir),
            },
            "zd": {
                "weekly_summaries": {
                    "enabled": False,
                    "base_url": "http://127.0.0.1:8001",
                    "model": "mlx-community/gemma-4-26b-a4b-it-4bit",
                    "timeout_seconds": 30,
                }
            },
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    def _seed_invoice_data(self, tmpdir):
        db_path = Path(tmpdir) / "zd.db"
        config_path = self._write_config(tmpdir)
        patchers = [
            patch.object(zd, "ZD_DB", db_path),
            patch.object(zd, "CONFIG_FILE", config_path),
            patch.dict(os.environ, {"HOME": tmpdir}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        zd.init_db()
        with zd.get_conn() as conn:
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("acme", "Acme Corp", 100.00),
            )
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("globex", "Globex Inc", 100.00),
            )
            acme_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("acme",)
            ).fetchone()["id"]
            globex_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("globex",)
            ).fetchone()["id"]
            conn.executemany(
                "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                [
                    (acme_id, "2026-03-31", 1.0, "march carryover"),
                    (acme_id, "2026-04-01", 2.0, "april kickoff"),
                    (acme_id, "2026-04-30", 3.0, "april closeout"),
                    (acme_id, "2026-05-01", 4.0, "may followup"),
                    (globex_id, "2026-04-15", 5.0, "other client"),
                ],
            )
            conn.executemany(
                "INSERT INTO expenses (client_id, expense_date, amount, description) VALUES (?,?,?,?)",
                [
                    (acme_id, "2026-03-31", 10.00, "march expense"),
                    (acme_id, "2026-04-15", 20.00, "april expense"),
                    (acme_id, "2026-05-01", 30.00, "may expense"),
                ],
            )
        return db_path, config_path

    def test_invoice_month_limits_sessions_and_expenses_to_selected_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Month: 2026-04", result.output)
            self.assertIn("5.0 hours @ $100.00/hr = $500.00", result.output)
            self.assertIn("Expenses: $20.00", result.output)
            self.assertIn("Total: $520.00", result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                billed_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NOT NULL
                       ORDER BY work_date"""
                ).fetchall()
                unbilled_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NULL
                       ORDER BY work_date"""
                ).fetchall()
                billed_expenses = conn.execute(
                    """SELECT expense_date FROM expenses
                       WHERE invoice_id IS NOT NULL
                       ORDER BY expense_date"""
                ).fetchall()
                unbilled_expenses = conn.execute(
                    """SELECT expense_date FROM expenses
                       WHERE invoice_id IS NULL
                       ORDER BY expense_date"""
                ).fetchall()

            self.assertEqual([row["work_date"] for row in billed_sessions], ["2026-04-01", "2026-04-30"])
            self.assertEqual(
                [row["work_date"] for row in unbilled_sessions],
                ["2026-03-31", "2026-04-15", "2026-05-01"],
            )
            self.assertEqual([row["expense_date"] for row in billed_expenses], ["2026-04-15"])
            self.assertEqual(
                [row["expense_date"] for row in unbilled_expenses],
                ["2026-03-31", "2026-05-01"],
            )

    def test_invoice_status_matches_between_csv_ledger_and_zd_db(self):
        """zd invoice writes the same status to the CSV ledger and the zd
        DB so the two sources of truth never disagree on a fresh invoice."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                input="y\n",
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                db_row = conn.execute(
                    "SELECT invoice_number, status FROM invoices ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertIsNotNone(db_row, "expected an invoice row in the zd DB")

            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            csv_match = next(
                (r for r in csv_rows if r.get("invoice_number") == db_row["invoice_number"]),
                None,
            )
            self.assertIsNotNone(csv_match, "expected the invoice in the CSV ledger")
            self.assertEqual(
                csv_match["status"], db_row["status"],
                "CSV ledger status must match zd DB status for a fresh invoice",
            )
            self.assertEqual(db_row["status"], "Sent")

    def test_invoice_without_month_still_invoices_all_unbilled_client_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-04-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIn("Month:", result.output)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                billed_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NOT NULL
                       ORDER BY work_date"""
                ).fetchall()
                unbilled_sessions = conn.execute(
                    """SELECT work_date FROM sessions
                       WHERE invoice_id IS NULL
                       ORDER BY work_date"""
                ).fetchall()

            self.assertEqual(
                [row["work_date"] for row in billed_sessions],
                ["2026-03-31", "2026-04-01", "2026-04-30", "2026-05-01"],
            )
            self.assertEqual([row["work_date"] for row in unbilled_sessions], ["2026-04-15"])

    def test_invoice_month_with_no_matching_work_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-02", "--date", "2026-02-28"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(
                "No unbilled sessions or expenses for Acme Corp in 2026-02.",
                result.output,
            )

    def test_group_sessions_by_week_can_use_summary_provider(self):
        sessions = [
            {"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "reviewed evidence"},
            {"work_date": "2026-04-07", "hours": 2.0, "rate": 100.0, "notes": "drafted recovery memo"},
        ]

        line_items = zd.group_sessions_by_week(
            sessions,
            summary_provider=lambda label, week_sessions: "Evidence review and recovery memo drafting",
        )

        self.assertEqual(
            line_items[0]["description"],
            "Week of Apr 6 - Evidence review and recovery memo drafting",
        )
        self.assertEqual(line_items[0]["hours"], 3.0)
        self.assertEqual(line_items[0]["amount"], 300.0)

    def test_clean_week_summary_strips_noise_and_truncates_long_text(self):
        cleaned = zd._clean_week_summary("  'Reviewed evidence and drafted memo.'  ")
        self.assertEqual(cleaned, "Reviewed evidence and drafted memo")

        long_summary = "x" * 200
        truncated = zd._clean_week_summary(long_summary)
        self.assertTrue(truncated.endswith("..."))
        self.assertLessEqual(len(truncated), 140)

    def test_month_bounds_returns_calendar_start_and_exclusive_end(self):
        self.assertEqual(zd._month_bounds("2026-04"), ("2026-04-01", "2026-05-01"))
        self.assertEqual(zd._month_bounds("2026-12"), ("2026-12-01", "2027-01-01"))

        with self.assertRaises(click.ClickException):
            zd._month_bounds("2026-4")

    def test_weekly_summary_config_ignores_invalid_timeout_values(self):
        config = {
            "zd": {
                "weekly_summaries": {
                    "enabled": True,
                    "timeout_seconds": "not-a-number",
                }
            }
        }

        with patch.dict(os.environ, {"ZD_SUMMARY_TIMEOUT": "also-bad"}):
            summary_config = zd._weekly_summary_config(config)

        self.assertEqual(summary_config["timeout_seconds"], 30.0)

        config["zd"]["weekly_summaries"]["timeout_seconds"] = -1
        self.assertEqual(zd._weekly_summary_config(config)["timeout_seconds"], 30.0)

        config["zd"]["weekly_summaries"]["timeout_seconds"] = 5
        self.assertEqual(zd._weekly_summary_config(config)["timeout_seconds"], 5.0)

    def test_group_sessions_by_week_falls_back_when_summary_provider_fails(self):
        sessions = [
            {"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "reviewed evidence"},
        ]

        def failing_provider(label, week_sessions):
            raise zd.WeekSummaryError("model unavailable")

        line_items = zd.group_sessions_by_week(sessions, summary_provider=failing_provider)

        self.assertEqual(line_items[0]["description"], "Week of Apr 6")

    # ------------------------------------------------------------------
    # Task C.7 — summary-server errors fall back to plain week labels
    # ------------------------------------------------------------------

    def test_group_sessions_by_week_falls_back_when_summary_server_unavailable(self):
        """A SummaryServerError raised by the provider (not just
        WeekSummaryError) must also degrade to the plain week label
        instead of propagating and crashing the invoice."""
        sessions = [
            {"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "reviewed evidence"},
        ]

        def server_down_provider(label, week_sessions):
            raise zd.SummaryServerError("llama-server not found on PATH")

        line_items = zd.group_sessions_by_week(sessions, summary_provider=server_down_provider)

        self.assertEqual(line_items[0]["description"], "Week of Apr 6")

    def test_invoice_falls_back_to_plain_labels_when_summary_server_unavailable(self):
        """End-to-end: if _summary_server_context raises SummaryServerError
        on entry (server can't start), `zd invoice` must still exit 0 and
        produce line items with plain 'Week of ...' labels rather than
        crashing the whole invoice."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            with patch.object(
                zd, "_summary_server_context",
                side_effect=zd.SummaryServerError("llama-server not found on PATH"),
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30",
                     "--summarize-weeks"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(
                "Summary server unavailable (llama-server not found on PATH); "
                "using plain week labels.",
                result.output,
            )

            # The invoice still generated: the CSV ledger row's line_items
            # column carries the plain "Week of ..." labels (no " - <summary>"
            # suffix) for both weeks in the April scope — 2026-04-01 kickoff
            # (week of Mar 30) and 2026-04-30 closeout (week of Apr 27).
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                inv_num = conn.execute(
                    "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
                ).fetchone()["invoice_number"]
            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            match = next(
                (r for r in csv_rows if r.get("invoice_number") == inv_num), None
            )
            self.assertIsNotNone(match, "expected the invoice in the CSV ledger")
            self.assertIn("Week of Mar 30", match["line_items"])
            self.assertIn("Week of Apr 27", match["line_items"])

    def test_invoice_falls_back_to_plain_labels_when_summary_spawn_raises_oserror(self):
        """The summary-server spawn does mkdir/open/Popen, which can raise
        OSError (FileNotFoundError if llama-server vanished, PermissionError if
        the log dir is unwritable). That must degrade to plain labels, not
        crash `zd invoice` with a raw traceback."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            with patch.object(
                zd, "_summary_server_context",
                side_effect=FileNotFoundError("llama-server: No such file or directory"),
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30",
                     "--summarize-weeks"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIsInstance(result.exception, OSError)
            self.assertIn("using plain week labels.", result.output)
            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            self.assertTrue(csv_rows, "expected the invoice in the CSV ledger")
            self.assertIn("Week of Mar 30", csv_rows[-1]["line_items"])

    def test_invoice_rejects_malformed_date_with_clean_error(self):
        """A malformed --date must fail as a clean ClickException (as cmd_log
        does), never a raw ValueError traceback, and write no invoice. The
        numbering step parses --date via date.fromisoformat, so this guards
        that path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-13-45"],
                input="y\n",
            )

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Date must be YYYY-MM-DD", result.output)
            # A clean ClickException, not a leaked ValueError from fromisoformat.
            self.assertNotIsInstance(result.exception, ValueError)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn(readonly=True) as conn:
                count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            self.assertEqual(count, 0, "no invoice row may be written on a bad date")

    # ------------------------------------------------------------------
    # Task C.8 — week grouping keyed by full Monday ISO date
    # (fix cross-year collision)
    # ------------------------------------------------------------------

    def test_week_key_is_year_inclusive_monday_iso_date(self):
        # 2026-01-06 and 2032-01-08 both fall in a week whose Monday is
        # "Jan 5" (week_label has no year), but the Mondays themselves are
        # different years: 2026-01-05 vs 2032-01-05.
        self.assertEqual(zd.week_label("2026-01-06"), "Week of Jan 5")
        self.assertEqual(zd.week_label("2032-01-08"), "Week of Jan 5")
        self.assertEqual(zd.week_key("2026-01-06"), "2026-01-05")
        self.assertEqual(zd.week_key("2032-01-08"), "2032-01-05")

    def test_group_sessions_by_week_does_not_merge_same_label_across_years(self):
        """Two sessions that produce the IDENTICAL week_label string
        ("Week of Jan 5") but fall in different years (2026 and 2032) must
        stay as TWO separate line items — grouping must key off the full
        Monday ISO date, not the year-less label string."""
        sessions = [
            {"work_date": "2026-01-06", "hours": 3.0, "rate": 100.0, "notes": "2026 work"},
            {"work_date": "2032-01-08", "hours": 5.0, "rate": 100.0, "notes": "2032 work"},
        ]

        line_items = zd.group_sessions_by_week(sessions)

        self.assertEqual(len(line_items), 2)
        # Both share the same displayed label...
        self.assertEqual(line_items[0]["description"], "Week of Jan 5")
        self.assertEqual(line_items[1]["description"], "Week of Jan 5")
        # ...but hours must NOT be summed together (no silent billing merge).
        hours = sorted(li["hours"] for li in line_items)
        self.assertEqual(hours, [3.0, 5.0])

    def test_group_sessions_by_week_sorts_cross_year_weeks_chronologically(self):
        """Line items must emit in chronological order by the Monday ISO
        key, even when sessions are inserted out of order and share a
        week_label string across years."""
        sessions = [
            {"work_date": "2032-01-08", "hours": 5.0, "rate": 100.0, "notes": "later year"},
            {"work_date": "2026-01-06", "hours": 3.0, "rate": 100.0, "notes": "earlier year"},
        ]

        line_items = zd.group_sessions_by_week(sessions)

        self.assertEqual(len(line_items), 2)
        self.assertEqual(line_items[0]["hours"], 3.0)  # 2026 week first
        self.assertEqual(line_items[1]["hours"], 5.0)  # 2032 week second

    def test_local_gemma_summary_hits_configured_base_url_and_model(self):
        sessions = [
            {"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "reviewed evidence"},
        ]

        response = Mock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "Evidence review"}}]}
        ).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch.object(zd.urllib.request, "urlopen", return_value=response) as mock_urlopen:
            summary = zd.summarize_week_with_local_gemma("Week of Apr 6", sessions)

        self.assertEqual(summary, "Evidence review")
        request = mock_urlopen.call_args.args[0]
        # Defaults: base_url = http://127.0.0.1:8086 (auto-spawned llama-server
        # for the small gemma-e2b GGUF), model = "summarizer" (the --alias
        # passed to llama-server). Both are overridable via env or config.
        self.assertEqual(request.full_url, "http://127.0.0.1:8086/v1/chat/completions")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "summarizer")
        self.assertIn("reviewed evidence", payload["messages"][1]["content"])

    def test_summary_server_context_uses_existing_server_without_spawning(self):
        """If /health already responds, we use the existing server as-is
        and do NOT terminate it on exit (it belongs to someone else)."""
        settings = {
            "base_url": "http://127.0.0.1:8086",
            "model": "summarizer",
            "model_path": "/dev/null",  # never read because server is "up"
            "log_path": "/tmp/zd-summary-server.log",
            "timeout_seconds": 30,
        }
        with patch.object(zd, "_server_alive", return_value=True) as alive, \
             patch.object(zd, "_spawn_summary_server") as spawn, \
             patch.object(zd, "_shutdown_summary_server") as shutdown:
            with zd._summary_server_context(settings):
                pass
        alive.assert_called()
        spawn.assert_not_called()
        shutdown.assert_not_called()

    def test_summary_server_context_spawns_and_tears_down_when_absent(self):
        """If /health is down on entry, we spawn llama-server, wait for
        readiness, run the block, and terminate the server on exit."""
        settings = {
            "base_url": "http://127.0.0.1:8086",
            "model": "summarizer",
            "model_path": "/dev/null",
            "log_path": "/tmp/zd-summary-server.log",
            "timeout_seconds": 30,
        }
        fake_proc = Mock()
        with patch.object(zd, "_server_alive", return_value=False), \
             patch.object(zd, "_spawn_summary_server", return_value=fake_proc) as spawn, \
             patch.object(zd, "_wait_for_summary_server") as wait, \
             patch.object(zd, "_shutdown_summary_server") as shutdown:
            with zd._summary_server_context(settings):
                pass
        spawn.assert_called_once()
        wait.assert_called_once_with("http://127.0.0.1:8086")
        shutdown.assert_called_once_with(fake_proc)

    def test_summary_server_context_shuts_down_on_readiness_timeout(self):
        """If llama-server never reports /health ready, the spawned
        process is killed before the SummaryServerError propagates."""
        settings = {
            "base_url": "http://127.0.0.1:8086",
            "model": "summarizer",
            "model_path": "/dev/null",
            "log_path": "/tmp/zd-summary-server.log",
            "timeout_seconds": 30,
        }
        fake_proc = Mock()
        with patch.object(zd, "_server_alive", return_value=False), \
             patch.object(zd, "_spawn_summary_server", return_value=fake_proc), \
             patch.object(zd, "_wait_for_summary_server",
                          side_effect=zd.SummaryServerError("timeout")), \
             patch.object(zd, "_shutdown_summary_server") as shutdown:
            with self.assertRaises(zd.SummaryServerError):
                with zd._summary_server_context(settings):
                    self.fail("body must not run when startup fails")
        shutdown.assert_called_once_with(fake_proc)

    def test_local_gemma_summary_requires_notes_and_content(self):
        empty_notes_sessions = [
            {"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "   "},
        ]
        with self.assertRaises(zd.WeekSummaryError):
            zd.summarize_week_with_local_gemma("Week of Apr 6", empty_notes_sessions)

        response = Mock()
        response.read.return_value = json.dumps({"choices": [{}]}).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch.object(zd.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(zd.WeekSummaryError) as ctx:
                zd.summarize_week_with_local_gemma(
                    "Week of Apr 6",
                    [{"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "reviewed evidence"}],
                )

        self.assertIn("missing content", str(ctx.exception))

    def test_invoice_rejects_invalid_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["invoice", "acme", "--month", "2026-4"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Month must be YYYY-MM format.", result.output)

    def test_invoice_help_documents_month_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            patcher = patch.object(zd, "ZD_DB", Path(tmpdir) / "zd.db")
            patcher.start()
            self.addCleanup(patcher.stop)
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["invoice", "--help"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--month YYYY-MM", result.output)
        self.assertIn("Only invoice unbilled items in this calendar month", result.output)
        self.assertIn("zd invoice acme --month 2026-04 --date 2026-04-30", result.output)
        self.assertIn("--summarize-weeks", result.output)
        self.assertNotIn("--summary-provider", result.output)

    def test_invoice_uses_weekly_summary_config_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["zd"]["weekly_summaries"]["enabled"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            with patch.object(
                zd,
                "summarize_week_with_local_gemma",
                return_value="Configured local summary",
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                    input="y\n",
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Summarizing weekly line items with local Gemma model", result.output)

    def test_invoice_summary_flag_overrides_disabled_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            with patch.object(
                zd,
                "summarize_week_with_local_gemma",
                return_value="Forced local summary",
            ):
                result = runner.invoke(
                    zd.cli,
                    [
                        "invoice",
                        "acme",
                        "--month",
                        "2026-04",
                        "--date",
                        "2026-04-30",
                        "--summarize-weeks",
                    ],
                    input="y\n",
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Summarizing weekly line items with local Gemma model", result.output)

    # ------------------------------------------------------------------
    # Task H.5 — 0600 summary-server log + non-loopback endpoint warning
    # ------------------------------------------------------------------

    def test_spawn_summary_server_creates_log_file_with_owner_only_perms(self):
        """The summary-server log can carry client session notes (INV-1), so
        it must be created 0600 regardless of the process umask. Drive the
        real _spawn_summary_server with shutil.which/model-path/Popen
        patched so no real llama-server is required."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "logs" / "summary.log"
            fake_proc = Mock()

            old_umask = os.umask(0o022)
            try:
                with patch.object(zd.shutil, "which", return_value="/usr/local/bin/llama-server"), \
                     patch.object(Path, "exists", return_value=True), \
                     patch("subprocess.Popen", return_value=fake_proc):
                    proc = zd._spawn_summary_server(
                        model_path="/fake/model.gguf",
                        base_url="http://127.0.0.1:8086",
                        alias="summarizer",
                        log_path=str(log_path),
                    )
            finally:
                os.umask(old_umask)

            self.assertIs(proc, fake_proc)
            self.assertTrue(log_path.exists())
            mode = log_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    def test_spawn_summary_server_tightens_perms_on_preexisting_looser_log(self):
        """os.open's mode only applies at CREATION time, so a log file that
        already exists with looser permissions (e.g. from before this
        hardening, or an odd umask) must still end up 0600 via the
        best-effort os.chmod."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "summary.log"
            log_path.write_text("old content\n", encoding="utf-8")
            os.chmod(log_path, 0o644)
            fake_proc = Mock()

            with patch.object(zd.shutil, "which", return_value="/usr/local/bin/llama-server"), \
                 patch.object(Path, "exists", return_value=True), \
                 patch("subprocess.Popen", return_value=fake_proc):
                zd._spawn_summary_server(
                    model_path="/fake/model.gguf",
                    base_url="http://127.0.0.1:8086",
                    alias="summarizer",
                    log_path=str(log_path),
                )

            mode = log_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    def test_is_loopback_host_recognizes_local_spellings(self):
        self.assertTrue(zd._is_loopback_host("127.0.0.1"))
        self.assertTrue(zd._is_loopback_host("127.5.5.5"))
        self.assertTrue(zd._is_loopback_host("localhost"))
        self.assertTrue(zd._is_loopback_host("LOCALHOST"))
        self.assertTrue(zd._is_loopback_host("::1"))
        self.assertFalse(zd._is_loopback_host("192.168.1.50"))
        self.assertFalse(zd._is_loopback_host("example.com"))
        self.assertFalse(zd._is_loopback_host(""))

    def test_invoice_warns_when_summary_endpoint_is_non_loopback(self):
        """Summaries enabled with a LAN base_url must warn exactly once that
        client session notes will leave the machine. _summary_server_context
        is patched to a no-op so no real server is contacted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["zd"]["weekly_summaries"]["enabled"] = True
            config["zd"]["weekly_summaries"]["base_url"] = "http://192.168.1.50:8086"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            @contextlib.contextmanager
            def _noop_server_context(settings):
                yield

            with patch.object(zd, "_summary_server_context", _noop_server_context), \
                 patch.object(
                     zd, "summarize_week_with_local_gemma", return_value="Remote summary"
                 ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("non-local endpoint", result.output)
            self.assertIn("http://192.168.1.50:8086", result.output)
            # Fires exactly once per run, not once per week (two weeks are in scope here).
            self.assertEqual(result.output.count("non-local endpoint"), 1)

    def test_invoice_does_not_warn_for_loopback_summary_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["zd"]["weekly_summaries"]["enabled"] = True
            config["zd"]["weekly_summaries"]["base_url"] = "http://127.0.0.1:8086"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            @contextlib.contextmanager
            def _noop_server_context(settings):
                yield

            with patch.object(zd, "_summary_server_context", _noop_server_context), \
                 patch.object(
                     zd, "summarize_week_with_local_gemma", return_value="Local summary"
                 ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                    input="y\n",
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIn("non-local endpoint", result.output)

    def test_invoice_does_not_warn_when_summaries_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            # Summaries disabled entirely, even though base_url is non-loopback.
            config["zd"]["weekly_summaries"]["enabled"] = False
            config["zd"]["weekly_summaries"]["base_url"] = "http://192.168.1.50:8086"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--month", "2026-04", "--date", "2026-04-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertNotIn("non-local endpoint", result.output)

    def test_invoice_help_does_not_initialize_database_for_process_invocation(self):
        runner = CliRunner()

        with patch.object(zd, "init_db", side_effect=AssertionError("init_db called")), patch.object(
            sys, "argv", ["zd", "invoice", "--help"]
        ):
            result = runner.invoke(zd.cli, ["invoice", "--help"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--month YYYY-MM", result.output)

    # ------------------------------------------------------------------
    # Task E.1 — flat-fee invoice (`--flat AMOUNT --description "..."`)
    # ------------------------------------------------------------------

    def test_flat_invoice_total_is_amount_independent_of_logged_hours(self):
        """A --flat invoice bills exactly AMOUNT regardless of how many
        hours are logged. The seed logs 10.0 unbilled Acme hours @ $100/hr
        ($1000 hourly), but the flat total must be $500.00 in DB and CSV."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])
            runner = CliRunner()

            # Sanity: Acme has 10.0 unbilled hours (1+2+3+4), which at $100/hr
            # would be $1000 on an hourly invoice — clearly != the $500 flat.
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                logged_hours = conn.execute(
                    "SELECT COALESCE(SUM(hours),0) AS h FROM sessions "
                    "WHERE client_id = ? AND invoice_id IS NULL",
                    (acme_id,),
                ).fetchone()["h"]
            self.assertEqual(logged_hours, 10.0)

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "500",
                 "--description", "Fixed-scope engagement"],
                input="y\n",
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Flat fee: Fixed-scope engagement", result.output)
            self.assertIn("Total: $500.00", result.output)
            # The hourly labor echo must NOT appear for a flat invoice.
            self.assertNotIn("@ $100.00/hr =", result.output)

            # DB: total is exactly 500.00, billing_mode='flat', sessions billed,
            # expenses left UNBILLED (CR-9).
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                inv = conn.execute(
                    "SELECT invoice_number, total, billing_mode FROM invoices "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                billed_sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE client_id = ? AND invoice_id IS NOT NULL",
                    (acme_id,),
                ).fetchone()["n"]
                unbilled_sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE client_id = ? AND invoice_id IS NULL",
                    (acme_id,),
                ).fetchone()["n"]
                billed_expenses = conn.execute(
                    "SELECT COUNT(*) AS n FROM expenses "
                    "WHERE client_id = ? AND invoice_id IS NOT NULL",
                    (acme_id,),
                ).fetchone()["n"]
                unbilled_expenses = conn.execute(
                    "SELECT COUNT(*) AS n FROM expenses "
                    "WHERE client_id = ? AND invoice_id IS NULL",
                    (acme_id,),
                ).fetchone()["n"]

            self.assertEqual(round(inv["total"], 2), 500.00)
            self.assertEqual(inv["billing_mode"], "flat")
            self.assertEqual(billed_sessions, 4)   # all 4 Acme sessions billed
            self.assertEqual(unbilled_sessions, 0)
            self.assertEqual(billed_expenses, 0)   # CR-9: expenses untouched
            self.assertEqual(unbilled_expenses, 3)

            # CSV ledger: total 500.00, and the line item is the clean
            # description with NO "(0 hrs @ $0.00/hr)" suffix.
            with open(csv_path, newline="") as f:
                rows = list(_csv.DictReader(f))
            match = next(
                (r for r in rows if r.get("invoice_number") == inv["invoice_number"]),
                None,
            )
            self.assertIsNotNone(match, "expected flat invoice in CSV ledger")
            self.assertEqual(match["total"], "500.00")
            self.assertEqual(match["line_items"], "Fixed-scope engagement")
            self.assertNotIn("hrs @", match["line_items"])

    def test_flat_invoice_regenerates_to_same_total_as_single_flat_line(self):
        """Regenerating a flat invoice reproduces its $500.00 total from a
        single flat line item — never the hourly weekly grouping."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            create = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "500",
                 "--description", "Fixed-scope engagement"],
                input="y\n",
            )
            self.assertEqual(create.exit_code, 0, msg=create.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                inv_num = conn.execute(
                    "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
                ).fetchone()["invoice_number"]

            regen = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--regenerate", inv_num],
                input="y\n",
            )
            self.assertEqual(regen.exit_code, 0, msg=regen.output)
            # Flat regenerate reuses the stored total and reports a single line.
            self.assertIn("Flat invoice — reusing stored total $500.00", regen.output)
            self.assertIn("→ 1 weekly line items", regen.output)
            self.assertIn("Total: $500.00", regen.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                total = conn.execute(
                    "SELECT total FROM invoices WHERE invoice_number = ?",
                    (inv_num,),
                ).fetchone()["total"]
            self.assertEqual(round(total, 2), 500.00)

    def test_flat_requires_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()
            result = runner.invoke(zd.cli, ["invoice", "acme", "--flat", "500"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("--flat requires --description", result.output)

    def test_flat_conflicts_with_summarize_weeks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "500",
                 "--description", "x", "--summarize-weeks"],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("cannot be combined with --summarize-weeks", result.output)

    def test_flat_rejects_zero_negative_and_non_finite_amounts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            runner = CliRunner()

            zero = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "0", "--description", "x"],
            )
            self.assertNotEqual(zero.exit_code, 0)
            self.assertIn("greater than 0", zero.output)

            neg = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "-100", "--description", "x"],
            )
            self.assertNotEqual(neg.exit_code, 0)
            self.assertIn("greater than 0", neg.output)

            for bad in ("inf", "nan", "-inf"):
                res = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--flat", bad, "--description", "x"],
                )
                self.assertNotEqual(res.exit_code, 0, msg=f"{bad!r} should fail")
                self.assertIn("finite", res.output)

            notnum = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--flat", "abc", "--description", "x"],
            )
            self.assertNotEqual(notnum.exit_code, 0)
            self.assertIn("must be a number", notnum.output)

            # Regression: a finite-but-enormous amount (scientific or a
            # fat-fingered long paste) exceeds Decimal's default 28-digit
            # context and used to escape as a raw InvalidOperation traceback
            # because quantize() sat outside the try/except. It must now fail
            # as a clean ClickException, never an unhandled exception.
            for huge in ("1e999", "1e400", "9" * 40):
                res = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--flat", huge, "--description", "x"],
                )
                self.assertNotEqual(res.exit_code, 0, msg=f"{huge!r} should fail")
                self.assertIn("too large", res.output, msg=f"{huge!r} output")
                # No raw decimal error leaked: the only surviving exception is
                # click's own SystemExit, not InvalidOperation.
                self.assertNotIsInstance(res.exception, InvalidOperation)

    def test_flat_invoice_help_documents_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            patcher = patch.object(zd, "ZD_DB", Path(tmpdir) / "zd.db")
            patcher.start()
            self.addCleanup(patcher.stop)
            runner = CliRunner()
            result = runner.invoke(zd.cli, ["invoice", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--flat AMOUNT", result.output)
        self.assertIn("--description", result.output)

    # ------------------------------------------------------------------
    # Task C.9 — invoice numbering: derive year from invoice date +
    # integer comparison (not date.today() / lexical string max)
    # ------------------------------------------------------------------

    def _seed_invoice_row_for_numbering(self, conn, client_id, invoice_number, invoice_date):
        conn.execute(
            """INSERT INTO invoices
               (invoice_number, client_id, invoice_date, total, status)
               VALUES (?, ?, ?, ?, ?)""",
            (invoice_number, client_id, invoice_date, 100.00, "Sent"),
        )

    def test_next_invoice_number_past_9999_uses_integer_not_lexical_max(self):
        """With a DB/CSV containing 2026-9999, the next number for a
        2026-dated invoice must be 2026-10000 (integer max + 1), never a
        lexical miscompare (where the string "2026-10000" sorts below
        "2026-9999") and never a reset back to 2026-0001."""
        import csv as _csv
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_data(tmpdir)
            with open(config_path) as f:
                config = json.load(f)
            csv_path = Path(config["storage"]["ledger_file"])

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-9999", "2026-01-01")

            # Also seed the CSV ledger with the same number so both stores
            # agree on the existing max.
            fieldnames = ["invoice_number", "client", "invoice_date", "total", "status", "pdf_file"]
            with open(csv_path, "w", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow({
                    "invoice_number": "2026-9999",
                    "client": "Acme Corp",
                    "invoice_date": "2026-01-01",
                    "total": "100.00",
                    "status": "Sent",
                    "pdf_file": "",
                })

            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-06-01"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating invoice 2026-10000 for Acme Corp", result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                new_row = conn.execute(
                    "SELECT invoice_number FROM invoices WHERE invoice_date = ?",
                    ("2026-06-01",),
                ).fetchone()
            self.assertEqual(new_row["invoice_number"], "2026-10000")

    def test_backdated_invoice_gets_number_from_its_own_year_not_current_year(self):
        """A --date 2025-06-30 invoice must be numbered from 2025's max
        (or 2025-0001 if none), NOT the current year's series, even
        though the DB already has 2026-000x invoices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-0001", "2026-01-15")
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-0002", "2026-02-15")

            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2025-06-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating invoice 2025-0001 for Acme Corp", result.output)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                new_row = conn.execute(
                    "SELECT invoice_number FROM invoices WHERE invoice_date = ?",
                    ("2025-06-30",),
                ).fetchone()
            self.assertEqual(new_row["invoice_number"], "2025-0001")

    def test_backdated_invoice_continues_its_own_years_series(self):
        """A backdated invoice whose year already has prior invoices gets
        the next suffix within THAT year, not year 2026's series."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_data(tmpdir)

            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                acme_id = conn.execute(
                    "SELECT id FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()["id"]
                self._seed_invoice_row_for_numbering(conn, acme_id, "2025-0005", "2025-03-01")
                self._seed_invoice_row_for_numbering(conn, acme_id, "2026-0001", "2026-01-15")

            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2025-06-30"],
                input="y\n",
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating invoice 2025-0006 for Acme Corp", result.output)

    # ------------------------------------------------------------------
    # Task A.1 — idempotent schema migration (_migrate)
    # ------------------------------------------------------------------

    # The pre-migration ("v0") schema: the CREATE TABLE statements as they
    # existed before Task A.1 added paid_date / billing_mode / billed_rate.
    _OLD_SCHEMA = """
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            rate        REAL NOT NULL,
            created_at  TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER NOT NULL REFERENCES clients(id),
            work_date   TEXT NOT NULL,
            hours       REAL NOT NULL,
            notes       TEXT,
            invoice_id  INTEGER REFERENCES invoices(id),
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER NOT NULL REFERENCES clients(id),
            expense_date TEXT NOT NULL,
            amount      REAL NOT NULL,
            description TEXT,
            invoice_id  INTEGER REFERENCES invoices(id),
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number  TEXT UNIQUE NOT NULL,
            client_id       INTEGER NOT NULL REFERENCES clients(id),
            invoice_date    TEXT NOT NULL,
            total           REAL NOT NULL,
            status          TEXT DEFAULT 'Sent',
            pdf_path        TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """

    def _make_old_db(self, tmpdir, name="old.db", extra_sql=None):
        """Create a v0-schema DB (user_version 0) with one client + session.

        Returns the db path. `extra_sql` runs after the base schema so a
        test can simulate an out-of-band column (e.g. paid_date).
        """
        db_path = Path(tmpdir) / name
        conn = zd.sqlite3.connect(db_path)
        try:
            conn.executescript(self._OLD_SCHEMA)
            if extra_sql:
                conn.executescript(extra_sql)
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("acme", "Acme Corp", 100.00),
            )
            conn.execute(
                "INSERT INTO sessions (client_id, work_date, hours, notes) "
                "VALUES (?,?,?,?)",
                (1, "2026-04-01", 2.5, "kickoff"),
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    @staticmethod
    def _table_info(db_path, table):
        conn = zd.sqlite3.connect(db_path)
        try:
            return conn.execute(f"PRAGMA table_info({table})").fetchall()
        finally:
            conn.close()

    @staticmethod
    def _user_version(db_path):
        conn = zd.sqlite3.connect(db_path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_migrate_adds_all_new_columns_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_old_db(tmpdir)

            # Sanity: the old DB starts without the new columns, at version 0.
            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            sess_cols = {row[1] for row in self._table_info(db_path, "sessions")}
            self.assertNotIn("paid_date", inv_cols)
            self.assertNotIn("billing_mode", inv_cols)
            self.assertNotIn("billed_rate", sess_cols)
            self.assertEqual(self._user_version(db_path), 0)

            with patch.object(zd, "ZD_DB", db_path):
                conn = zd.sqlite3.connect(db_path)
                try:
                    zd._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            sess_cols = {row[1] for row in self._table_info(db_path, "sessions")}
            self.assertIn("paid_date", inv_cols)
            self.assertIn("billing_mode", inv_cols)
            self.assertIn("billed_rate", sess_cols)
            self.assertEqual(self._user_version(db_path), zd._SCHEMA_VERSION)

            # Existing rows survive; the DEFAULT applies to billing_mode.
            conn = zd.sqlite3.connect(db_path)
            conn.row_factory = zd.sqlite3.Row
            try:
                client = conn.execute("SELECT * FROM clients").fetchone()
                session = conn.execute("SELECT * FROM sessions").fetchone()
            finally:
                conn.close()
            self.assertEqual(client["name"], "Acme Corp")
            self.assertEqual(session["hours"], 2.5)
            self.assertEqual(session["notes"], "kickoff")
            self.assertIsNone(session["billed_rate"])

    def test_migrate_is_idempotent_on_repeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_old_db(tmpdir)

            with patch.object(zd, "ZD_DB", db_path):
                conn = zd.sqlite3.connect(db_path)
                try:
                    zd._migrate(conn)
                    conn.commit()
                    first_info = conn.execute(
                        "PRAGMA table_info(invoices)"
                    ).fetchall()
                    # Second run must be a no-op (fast path via user_version)
                    # and must not raise a duplicate-column error.
                    zd._migrate(conn)
                    conn.commit()
                    second_info = conn.execute(
                        "PRAGMA table_info(invoices)"
                    ).fetchall()
                finally:
                    conn.close()

            self.assertEqual(first_info, second_info)
            self.assertEqual(self._user_version(db_path), zd._SCHEMA_VERSION)

    def test_migrated_old_db_matches_fresh_init_db_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Fresh DB straight from init_db.
            fresh_path = Path(tmpdir) / "fresh.db"
            with patch.object(zd, "ZD_DB", fresh_path):
                zd.init_db()

            # Old DB brought forward via _migrate.
            old_path = self._make_old_db(tmpdir, name="migrated.db")
            with patch.object(zd, "ZD_DB", old_path):
                conn = zd.sqlite3.connect(old_path)
                try:
                    zd._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

            for table in ("invoices", "sessions"):
                fresh_info = self._table_info(fresh_path, table)
                migrated_info = self._table_info(old_path, table)
                self.assertEqual(
                    fresh_info,
                    migrated_info,
                    f"{table} schema diverged between fresh and migrated DB",
                )

            self.assertEqual(
                self._user_version(fresh_path),
                self._user_version(old_path),
            )
            self.assertEqual(self._user_version(fresh_path), zd._SCHEMA_VERSION)

    def test_migrate_skips_alter_for_preexisting_out_of_band_column(self):
        # Simulate the live DB, which already has paid_date added out-of-band
        # while user_version is still 0. _migrate must skip the paid_date
        # ALTER (guarded by table_info) and still add the other two columns.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_old_db(
                tmpdir,
                name="oob.db",
                extra_sql="ALTER TABLE invoices ADD COLUMN paid_date TEXT;",
            )
            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            self.assertIn("paid_date", inv_cols)
            self.assertEqual(self._user_version(db_path), 0)

            with patch.object(zd, "ZD_DB", db_path):
                conn = zd.sqlite3.connect(db_path)
                try:
                    # Must not raise "duplicate column name: paid_date".
                    zd._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            sess_cols = {row[1] for row in self._table_info(db_path, "sessions")}
            self.assertIn("paid_date", inv_cols)
            self.assertIn("billing_mode", inv_cols)
            self.assertIn("billed_rate", sess_cols)
            # paid_date must appear exactly once (not duplicated).
            paid_date_count = sum(
                1 for row in self._table_info(db_path, "invoices")
                if row[1] == "paid_date"
            )
            self.assertEqual(paid_date_count, 1)
            self.assertEqual(self._user_version(db_path), zd._SCHEMA_VERSION)

    def _seed_invoice_row(self, tmpdir, invoice_number="2026-0001", status="Sent"):
        """Insert a minimal invoices row for `zd paid` tests."""
        db_path, config_path = self._seed_invoice_data(tmpdir)
        with zd.get_conn() as conn:
            acme_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("acme",)
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO invoices
                   (invoice_number, client_id, invoice_date, total, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (invoice_number, acme_id, "2026-05-01", 500.00, status),
            )
        return db_path, config_path

    def _write_ledger_csv(self, config_path, rows):
        """Write a minimal invoices.csv ledger matching `rows` (dicts)."""
        import csv as _csv
        with open(config_path) as f:
            config = json.load(f)
        csv_path = Path(config["storage"]["ledger_file"])
        fieldnames = ["invoice_number", "client", "invoice_date", "total", "status", "pdf_file"]
        with open(csv_path, "w", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return csv_path

    def test_paid_with_no_date_stores_today_and_sets_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_row(tmpdir, "2026-0001")
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT status, paid_date FROM invoices WHERE invoice_number = ?",
                    ("2026-0001",),
                ).fetchone()
            self.assertEqual(row["status"], "Paid")
            self.assertEqual(row["paid_date"], zd.date.today().isoformat())

    def test_paid_with_explicit_date_stores_that_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_row(tmpdir, "2026-0001")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["paid", "2026-0001", "--date", "2026-05-01"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT status, paid_date FROM invoices WHERE invoice_number = ?",
                    ("2026-0001",),
                ).fetchone()
            self.assertEqual(row["status"], "Paid")
            self.assertEqual(row["paid_date"], "2026-05-01")

    def test_paid_rejects_malformed_date_without_db_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = self._seed_invoice_row(tmpdir, "2026-0001")
            runner = CliRunner()

            for bad_date in ("2026-13-40", "not-a-date"):
                result = runner.invoke(
                    zd.cli, ["paid", "2026-0001", "--date", bad_date]
                )
                self.assertNotEqual(result.exit_code, 0, msg=result.output)
                with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                    row = conn.execute(
                        "SELECT status, paid_date FROM invoices WHERE invoice_number = ?",
                        ("2026-0001",),
                    ).fetchone()
                self.assertEqual(row["status"], "Sent")
                self.assertIsNone(row["paid_date"])

    def test_paid_updates_matching_csv_row_and_reports_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_row(tmpdir, "2026-0001")
            csv_path = self._write_ledger_csv(
                config_path,
                [
                    {
                        "invoice_number": "2026-0001",
                        "client": "Acme Corp",
                        "invoice_date": "2026-05-01",
                        "total": "500.00",
                        "status": "Sent",
                        "pdf_file": "",
                    }
                ],
            )
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("marked Paid in zd DB and CSV ledger", result.output)
            import csv as _csv
            with open(csv_path, newline="") as f:
                csv_rows = list(_csv.DictReader(f))
            self.assertEqual(csv_rows[0]["status"], "Paid")

    def test_paid_with_no_matching_csv_row_does_not_falsely_claim_csv_update(self):
        # This isolates cmd_paid's OWN honest-reporting logic (H.4). A.6 later
        # added an auto-convergence pass (_auto_converge) that would heal this
        # exact DB-ahead drift by appending the missing CSV row before cmd_paid
        # patches it — the correct integrated behavior, covered by
        # ReconcileConvergenceTests. Here we disable that layer so cmd_paid
        # faces a genuinely-absent row (the degraded path where convergence
        # could not heal) and must still report honestly rather than claim a
        # CSV update it did not make.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_row(tmpdir, "2026-0001")
            self._write_ledger_csv(
                config_path,
                [
                    {
                        "invoice_number": "2026-9999",
                        "client": "Acme Corp",
                        "invoice_date": "2026-05-01",
                        "total": "999.00",
                        "status": "Sent",
                        "pdf_file": "",
                    }
                ],
            )
            runner = CliRunner()

            with patch.object(zd, "_auto_converge", lambda conn: None):
                result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            # DB must still be updated to Paid.
            with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM invoices WHERE invoice_number = ?",
                    ("2026-0001",),
                ).fetchone()
            self.assertEqual(row["status"], "Paid")
            # Output must NOT falsely claim the CSV ledger was updated.
            self.assertNotIn("marked Paid in zd DB and CSV ledger", result.output)
            self.assertIn("No matching row for 2026-0001 found in the CSV ledger", result.output)
            self.assertNotIn("zd reconcile", result.output)

    def test_paid_backs_up_csv_before_patching_matched_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed_invoice_row(tmpdir, "2026-0001")
            csv_path = self._write_ledger_csv(
                config_path,
                [
                    {
                        "invoice_number": "2026-0001",
                        "client": "Acme Corp",
                        "invoice_date": "2026-05-01",
                        "total": "500.00",
                        "status": "Sent",
                        "pdf_file": "",
                    }
                ],
            )
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            backups = list(csv_path.parent.glob(f"{csv_path.name}.*.bak"))
            self.assertTrue(backups, "expected a .bak backup of the CSV ledger before the patch")

    # ------------------------------------------------------------------
    # Task H.1 — _sync_client_to_config fail-closed + shared lock
    # ------------------------------------------------------------------

    def test_add_client_fails_closed_on_corrupt_config_and_leaves_it_untouched(self):
        """A CORRUPT ~/.invoice_config.json (exists but doesn't parse) must
        cause `zd add-client` to fail with a clean ClickException — never
        silently overwrite the file with a near-empty {clients: [name]}
        object (the original data-loss bug)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            corrupt_bytes = b'{ not json'
            config_path.write_bytes(corrupt_bytes)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "widget", "Widget Co", "150.00"]
            )

            self.assertNotEqual(result.exit_code, 0)
            # A clean ClickException, not a leaked traceback.
            self.assertNotIsInstance(result.exception, json.JSONDecodeError)
            # The corrupt file must be left BYTE-FOR-BYTE unchanged.
            self.assertEqual(config_path.read_bytes(), corrupt_bytes)

    def test_add_client_persists_new_client_and_preserves_existing_config(self):
        """A new client added to a VALID config round-trips, and the
        pre-existing payee/payment/other-client data must survive (not be
        wiped by the sync)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["payee"]["name"] = "Zero Delta LLC"
            config["payee"]["email"] = "billing@zerodelta.example"
            config["payment"]["bank_name"] = "First National"
            config["payment"]["routing"] = "111000025"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "widget", "Widget Co", "150.00"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            # New client persisted.
            names = [c.get("name") for c in updated["clients"]]
            self.assertIn("Widget Co", names)
            # Pre-existing client(s) preserved.
            self.assertIn("Acme Corp", names)
            # Pre-existing payee/payment data preserved (not wiped).
            self.assertEqual(updated["payee"]["name"], "Zero Delta LLC")
            self.assertEqual(updated["payee"]["email"], "billing@zerodelta.example")
            self.assertEqual(updated["payment"]["bank_name"], "First National")
            self.assertEqual(updated["payment"]["routing"], "111000025")

    def test_add_client_creates_config_when_genuinely_absent(self):
        """A genuinely MISSING config file (not corrupt, just never created)
        is the one case allowed to start from {}."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "zd.db"
            config_path = Path(tmpdir) / ".invoice_config.json"
            patchers = [
                patch.object(zd, "ZD_DB", db_path),
                patch.object(zd, "CONFIG_FILE", config_path),
                patch.dict(os.environ, {"HOME": tmpdir}),
            ]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)
            zd.init_db()
            self.assertFalse(config_path.exists())
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "widget", "Widget Co", "150.00"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual([c["name"] for c in config["clients"]], ["Widget Co"])

    def test_add_client_is_noop_when_already_present_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            config_path = Path(tmpdir) / ".invoice_config.json"
            before = config_path.read_text(encoding="utf-8")
            runner = CliRunner()

            result = runner.invoke(
                zd.cli, ["add-client", "acme", "ACME CORP", "150.00"]
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            after = config_path.read_text(encoding="utf-8")
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
