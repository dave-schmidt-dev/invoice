"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import json
import os
import unittest
from unittest.mock import Mock, patch
import click
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin

class ZdWeeklySummaryTests(ZdInvoiceFixtureMixin, unittest.TestCase):

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
            "Apr 6-7 - Evidence review and recovery memo drafting",
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

        self.assertEqual(line_items[0]["description"], "Apr 6")

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

        self.assertEqual(line_items[0]["description"], "Apr 6")

    def test_week_key_is_year_inclusive_monday_iso_date(self):
        # 2026-01-06 and 2032-01-06 render the IDENTICAL label "Jan 6"
        # (week_label has no year), but their week Mondays are in different
        # years: 2026-01-05 vs 2032-01-05.
        self.assertEqual(zd.week_label("2026-01-06"), "Jan 6")
        self.assertEqual(zd.week_label("2032-01-06"), "Jan 6")
        self.assertEqual(zd.week_key("2026-01-06"), "2026-01-05")
        self.assertEqual(zd.week_key("2032-01-06"), "2032-01-05")

    def test_week_label_renders_the_dates_actually_worked(self):
        # A single day collapses to that day.
        self.assertEqual(zd.week_label("2026-08-01"), "Aug 1")
        self.assertEqual(zd.week_label(["2026-08-05"]), "Aug 5")
        # A range within one month repeats only the day number.
        self.assertEqual(zd.week_label(["2026-08-03", "2026-08-07"]), "Aug 3-7")
        # Unsorted input is normalized.
        self.assertEqual(zd.week_label(["2026-08-07", "2026-08-03"]), "Aug 3-7")
        # A range crossing a month boundary repeats the month.
        self.assertEqual(
            zd.week_label(["2026-08-31", "2026-09-02"]), "Aug 31-Sep 2"
        )

    def test_week_label_never_shows_a_date_before_the_first_session(self):
        """A weekend-only week must not advertise the previous month's
        Monday. Aug 1/2 2026 are Sat/Sun in the week of Mon Jul 27; an
        August-scoped invoice must read "Aug 1-2", never "Week of Jul 27"."""
        sessions = [
            {"work_date": "2026-08-01", "hours": 3.0, "rate": 100.0, "notes": "sat work"},
            {"work_date": "2026-08-02", "hours": 3.0, "rate": 100.0, "notes": "sun work"},
        ]

        line_items = zd.group_sessions_by_week(sessions)

        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0]["description"], "Aug 1-2")
        self.assertNotIn("Jul", line_items[0]["description"])
        self.assertEqual(line_items[0]["hours"], 6.0)

    def test_week_label_spans_a_month_boundary_within_one_week(self):
        """Mon Aug 31 2026 and Wed Sep 2 2026 share a week, so they stay ONE
        line item, labelled across the month boundary."""
        sessions = [
            {"work_date": "2026-08-31", "hours": 2.0, "rate": 100.0, "notes": "monday"},
            {"work_date": "2026-09-02", "hours": 4.0, "rate": 100.0, "notes": "wednesday"},
        ]

        line_items = zd.group_sessions_by_week(sessions)

        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0]["description"], "Aug 31-Sep 2")
        self.assertEqual(line_items[0]["hours"], 6.0)

    def test_group_sessions_by_week_does_not_merge_same_label_across_years(self):
        """Two sessions that produce the IDENTICAL week_label string
        ("Jan 6") but fall in different years (2026 and 2032) must
        stay as TWO separate line items — grouping must key off the full
        Monday ISO date, not the year-less label string."""
        sessions = [
            {"work_date": "2026-01-06", "hours": 3.0, "rate": 100.0, "notes": "2026 work"},
            {"work_date": "2032-01-06", "hours": 5.0, "rate": 100.0, "notes": "2032 work"},
        ]

        line_items = zd.group_sessions_by_week(sessions)

        self.assertEqual(len(line_items), 2)
        # Both share the same displayed label...
        self.assertEqual(line_items[0]["description"], "Jan 6")
        self.assertEqual(line_items[1]["description"], "Jan 6")
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
            summary = zd.summarize_week_with_local_gemma("Apr 6", sessions)

        self.assertEqual(summary, "Evidence review")
        request = mock_urlopen.call_args.args[0]
        # Defaults: base_url = http://127.0.0.1:8086 (auto-spawned llama-server
        # for the small gemma-e2b GGUF), model = "summarizer" (the --alias
        # passed to llama-server). Both are overridable via env or config.
        self.assertEqual(request.full_url, "http://127.0.0.1:8086/v1/chat/completions")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "summarizer")
        self.assertIn("reviewed evidence", payload["messages"][1]["content"])

    def test_local_gemma_summary_requires_notes_and_content(self):
        empty_notes_sessions = [
            {"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "   "},
        ]
        with self.assertRaises(zd.WeekSummaryError):
            zd.summarize_week_with_local_gemma("Apr 6", empty_notes_sessions)

        response = Mock()
        response.read.return_value = json.dumps({"choices": [{}]}).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch.object(zd.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(zd.WeekSummaryError) as ctx:
                zd.summarize_week_with_local_gemma(
                    "Apr 6",
                    [{"work_date": "2026-04-06", "hours": 1.0, "rate": 100.0, "notes": "reviewed evidence"}],
                )

        self.assertIn("missing content", str(ctx.exception))
