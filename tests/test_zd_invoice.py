import json
import os
import sys
import tempfile
import unittest
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

    def test_invoice_help_does_not_initialize_database_for_process_invocation(self):
        runner = CliRunner()

        with patch.object(zd, "init_db", side_effect=AssertionError("init_db called")), patch.object(
            sys, "argv", ["zd", "invoice", "--help"]
        ):
            result = runner.invoke(zd.cli, ["invoice", "--help"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--month YYYY-MM", result.output)


if __name__ == "__main__":
    unittest.main()
