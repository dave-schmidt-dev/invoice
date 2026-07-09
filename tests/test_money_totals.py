"""INV-4 gate: the confirmation total the user approves at the "Proceed?"
prompt MUST equal the total persisted to the PDF, the CSV ledger, and the zd
DB — for ALL per-week rounding cases, not just the pre-round one.

The historical bug (INV-4 / F5): the confirmation total was computed as an
aggregate, to_money(total_hours * rate), while the persisted total is the sum
of the per-week to_money(hours * rate) line-item amounts. Sum-of-rounded is
not rounded-of-sum, so the user could approve one figure and have a different
one billed.

Each test seeds a client whose rate + hours split across two DIFFERENT ISO
weeks exercises that skew, drives `zd invoice` through CliRunner, scrapes the
"Total:" line from the confirmation output, and asserts it equals BOTH the CSV
ledger total AND the DB invoices.total.
"""
import csv as _csv
import json
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import zd


# The confirmation-block total, e.g. "  Total: $100.00" — captures the numeric
# amount (thousands separators allowed) the user is asked to approve.
#
# This must NOT match the post-write success line "  ✓  Total: $100.00", which
# echoes the ALREADY-persisted figure. Matching that line would mask the very
# skew this gate exists to catch (the pre-fix confirmation said $99.99 while
# the success line and DB/CSV said $100.00). Anchor on a "Total:" that begins
# its line (after leading whitespace) with no preceding checkmark glyph.
_TOTAL_RE = re.compile(r"^\s*Total:\s*\$([\d,]+\.\d{2})", re.MULTILINE)


class MoneyTotalsTests(unittest.TestCase):
    """Confirmation total == persisted total across per-week rounding skew."""

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
                    "name": "Rounding Co",
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
            # Weekly summaries OFF so grouping stays deterministic and no local
            # model server is contacted.
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

    def _seed(self, tmpdir, rate, sessions):
        """Patch DB/config/HOME, init the schema, and seed one client plus the
        given (work_date, hours) sessions. Returns (db_path, config_path)."""
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
                ("roundco", "Rounding Co", rate),
            )
            client_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("roundco",)
            ).fetchone()["id"]
            conn.executemany(
                "INSERT INTO sessions (client_id, work_date, hours, notes) "
                "VALUES (?,?,?,?)",
                [(client_id, work_date, hours, "work") for work_date, hours in sessions],
            )
        return db_path, config_path

    def _confirmation_total(self, output):
        """Extract the Decimal total shown at the "Proceed?" confirmation."""
        matches = _TOTAL_RE.findall(output)
        # Exactly one confirmation "Total:" line per invoice run; the success
        # line ("  ✓  Total: ...") is excluded by the anchored regex so we
        # never accidentally read back the persisted figure.
        self.assertEqual(
            len(matches), 1,
            msg=f"expected one confirmation Total, got {matches}:\n{output}",
        )
        return Decimal(matches[0].replace(",", ""))

    def _persisted_totals(self, db_path, config_path):
        """Return (db_total, csv_total) as Decimals for the sole invoice."""
        with patch.object(zd, "ZD_DB", db_path), zd.get_conn() as conn:
            db_row = conn.execute(
                "SELECT invoice_number, total FROM invoices ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(db_row, "expected an invoice row in the zd DB")
        # DB stores total as REAL (float); quantize to cents for comparison.
        db_total = zd.to_money(db_row["total"])

        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        csv_path = Path(config["storage"]["ledger_file"])
        with open(csv_path, newline="") as f:
            rows = list(_csv.DictReader(f))
        csv_match = next(
            (r for r in rows if r.get("invoice_number") == db_row["invoice_number"]),
            None,
        )
        self.assertIsNotNone(csv_match, "expected the invoice in the CSV ledger")
        csv_total = zd.to_money(csv_match["total"])
        return db_total, csv_total

    def _run_and_assert_equal(self, rate, sessions, expected_confirmation):
        """Drive `zd invoice`, then assert confirmation == CSV == DB total."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, config_path = self._seed(tmpdir, rate, sessions)
            runner = CliRunner()
            result = runner.invoke(
                zd.cli,
                ["invoice", "roundco", "--date", "2026-04-30"],
                input="y\n",
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)

            confirmation = self._confirmation_total(result.output)
            db_total, csv_total = self._persisted_totals(db_path, config_path)

            # The number the user approved must be the number that was billed —
            # to the PDF-derived DB total and the CSV ledger, by construction.
            self.assertEqual(
                confirmation, db_total,
                msg=(f"confirmation ${confirmation} != DB total ${db_total}\n"
                     f"{result.output}"),
            )
            self.assertEqual(
                confirmation, csv_total,
                msg=(f"confirmation ${confirmation} != CSV total ${csv_total}\n"
                     f"{result.output}"),
            )
            # Guard the reproduction itself: the fixture must actually land on
            # the skew value, else the test would pass vacuously.
            self.assertEqual(confirmation, expected_confirmation)

    def test_per_week_rounding_skew_1_5h_two_weeks(self):
        """rate 33.33, 1.5h in each of two DIFFERENT ISO weeks.

        Per-week: to_money(1.5 * 33.33) = $50.00, summed = $100.00 (persisted).
        Old aggregate confirmation: to_money(3.0 * 33.33) = $99.99 — the skew.
        After the fix the confirmation is derived from the line items, so it
        equals the persisted $100.00.
        """
        self._run_and_assert_equal(
            rate=33.33,
            sessions=[("2026-04-06", 1.5), ("2026-04-13", 1.5)],  # ISO wk 15, 16
            expected_confirmation=Decimal("100.00"),
        )

    def test_pre_round_hours_skew_2_015h_two_weeks(self):
        """rate 33.33, 2.015h in each of two DIFFERENT ISO weeks.

        Exercises the removed round(hours, 2) pre-round: the old code rounded
        2.015 -> 2.02 before pricing, yielding to_money(2.02 * 33.33) = $67.33
        per week ($134.66). With the raw 2.015h the per-week amount is
        to_money(2.015 * 33.33) = $67.16, summed to $134.32 (persisted). The
        confirmation, derived from the same line items, matches $134.32.
        """
        self._run_and_assert_equal(
            rate=33.33,
            sessions=[("2026-04-06", 2.015), ("2026-04-13", 2.015)],
            expected_confirmation=Decimal("134.32"),
        )


if __name__ == "__main__":
    unittest.main()
