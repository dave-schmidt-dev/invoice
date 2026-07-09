"""INV-3 gate: billed-rate immutability on `zd invoice --regenerate`.

Task B.3 locks in the rule that regenerating an already-billed invoice must
price its line items at the rate that was BILLED (snapshotted onto
``sessions.billed_rate`` at billing time by Task A.3), NOT the client's
current rate. A later rate change on the client must never retroactively
alter a historical invoice's total.

Contract asserted here (regenerate half):

  * Bill an invoice while the client rate is X (A.3 snapshots billed_rate=X).
    Change the client's rate to Y (Y != X). Regenerate the invoice. The
    regenerated total EQUALS the original total (priced at X via billed_rate),
    NOT recomputed at Y — verified against both ``invoices.total`` in the zd DB
    and the ``total`` column in the CSV ledger.
  * A legacy billed session with a NULL ``billed_rate`` (billed before the A.3
    snapshot existed) triggers a prominent warning on regenerate — the current
    rate is never silently substituted.

Task B.4 extends this file with the force-edit half: `zd edit`/`zd edit-expense`
REFUSE billing-relevant edits (hours/date, amount/date) on an already-billed
session/expense REGARDLESS of --force, naming the parent invoice in the
refusal. Only notes/description may be edited on a billed row, and that
requires no --force. There is no auto-regenerate — the refusal simply leaves
the row and the invoice's total untouched.

The harness mirrors tests/test_zd_invoice.py and
tests/test_invoice_transaction_integrity.py: TemporaryDirectory, HOME/ZD_DB/
CONFIG_FILE patched to the sandbox, a storage.ledger_file CSV, and CliRunner
with input="y\n" to answer the "Proceed?" confirmation. No real user files are
ever touched.
"""

import csv as _csv
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import zd


class _RegenerateHarness(unittest.TestCase):
    """Shared sandbox: config, seeded client + sessions, bill/regenerate."""

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
                    "base_url": "http://127.0.0.1:8086",
                    "model": "summarizer",
                    "timeout_seconds": 30,
                }
            },
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path, Path(ledger_file), Path(invoices_dir)

    def _seed(self, tmpdir, rate=100.00):
        """Seed Acme with two unbilled sessions at ``rate``."""
        db_path = Path(tmpdir) / "zd.db"
        config_path, ledger_file, invoices_dir = self._write_config(tmpdir)
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
                ("acme", "Acme Corp", rate),
            )
            acme_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("acme",)
            ).fetchone()["id"]
            conn.executemany(
                "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                [
                    (acme_id, "2026-04-01", 2.0, "kickoff"),
                    (acme_id, "2026-04-15", 3.0, "midpoint"),
                ],
            )
        return db_path, config_path, ledger_file, invoices_dir

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _set_client_rate(db_path, slug, new_rate):
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE clients SET rate = ? WHERE slug = ?", (new_rate, slug)
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _invoice_row(db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM invoices ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

    @staticmethod
    def _csv_total(ledger_file, invoice_number):
        with open(ledger_file, newline="", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                if row.get("invoice_number") == invoice_number:
                    return row.get("total")
        return None

    @staticmethod
    def _null_billed_rate(db_path):
        """Blank out billed_rate on all sessions, simulating a legacy invoice
        billed before the A.3 snapshot column existed."""
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE sessions SET billed_rate = NULL")
            conn.commit()
        finally:
            conn.close()

    def _bill(self, runner):
        result = runner.invoke(
            zd.cli,
            ["invoice", "acme", "--date", "2026-04-30"],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        return result

    def _regenerate(self, runner, invoice_number):
        result = runner.invoke(
            zd.cli,
            ["invoice", "acme", "--regenerate", invoice_number],
            input="y\n",
        )
        return result


class RegenerateRateImmutabilityTests(_RegenerateHarness):
    """INV-3: a client rate change must NOT alter an already-billed total."""

    def test_regenerate_prices_at_billed_rate_not_current_client_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            # Bill at rate X = 100. 5 hours -> $500.00, snapshotted billed_rate.
            bill = self._bill(runner)
            self.assertIn("Total: $500.00", bill.output)

            billed = self._invoice_row(db_path)
            invoice_number = billed["invoice_number"]
            original_db_total = billed["total"]
            original_csv_total = self._csv_total(ledger_file, invoice_number)
            self.assertEqual(original_db_total, 500.00)
            self.assertEqual(original_csv_total, "500.00")

            # Every billed session carries billed_rate = X.
            conn = sqlite3.connect(db_path)
            try:
                rates = [
                    r[0]
                    for r in conn.execute(
                        "SELECT billed_rate FROM sessions WHERE invoice_id IS NOT NULL"
                    ).fetchall()
                ]
            finally:
                conn.close()
            self.assertTrue(rates and all(r == 100.00 for r in rates))

            # Client rate changes to Y = 250 AFTER billing.
            self._set_client_rate(db_path, "acme", 250.00)

            # Regenerate. Total MUST stay at X-priced $500.00, NOT the
            # Y-recomputed 5h * $250 = $1250.00.
            regen = self._regenerate(runner, invoice_number)
            self.assertEqual(regen.exit_code, 0, msg=regen.output)
            self.assertIn("Total: $500.00", regen.output)
            self.assertNotIn("1,250.00", regen.output)
            self.assertNotIn("$1250", regen.output)

            # Persisted total unchanged in BOTH stores.
            after = self._invoice_row(db_path)
            self.assertEqual(
                after["total"], original_db_total,
                "regenerate must not repriced the DB total at the new client rate",
            )
            self.assertEqual(
                self._csv_total(ledger_file, invoice_number),
                original_csv_total,
                "regenerate must not reprice the CSV ledger total",
            )

    def test_regenerate_confirmation_total_equals_persisted_total(self):
        """The number the user approves at the confirmation prompt is exactly
        the number written to the DB/CSV (INV-4), even after a rate change."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed(tmpdir, rate=137.50)
            runner = CliRunner()

            bill = self._bill(runner)
            billed = self._invoice_row(db_path)
            invoice_number = billed["invoice_number"]

            # 5 hours * 137.50 = 687.50 across the weekly line items.
            self.assertIn("Total: $687.50", bill.output)
            self.assertEqual(billed["total"], 687.50)

            self._set_client_rate(db_path, "acme", 999.99)

            regen = self._regenerate(runner, invoice_number)
            self.assertEqual(regen.exit_code, 0, msg=regen.output)
            # Confirmation echo == persisted total.
            self.assertIn("Total: $687.50", regen.output)

            after = self._invoice_row(db_path)
            self.assertEqual(after["total"], 687.50)
            self.assertEqual(
                self._csv_total(ledger_file, invoice_number), "687.50"
            )

    def test_regenerate_warns_when_session_has_null_billed_rate(self):
        """A legacy billed session (NULL billed_rate) must trigger a prominent
        warning on regenerate — the current rate is never silently used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, _, _ = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            self._bill(runner)
            invoice_number = self._invoice_row(db_path)["invoice_number"]

            # Simulate a pre-A.3 legacy invoice: no snapshotted rate.
            self._null_billed_rate(db_path)

            regen = self._regenerate(runner, invoice_number)
            self.assertEqual(regen.exit_code, 0, msg=regen.output)
            # CliRunner merges stderr into .output by default; the warning must
            # be present and unmistakable.
            self.assertIn("WARNING", regen.output)
            self.assertIn("billed_rate", regen.output)
            self.assertIn("UNVERIFIED", regen.output)


class RegenerateForceEditTests(_RegenerateHarness):
    """Task B.4 (INV-3): billed rows lock their billing-relevant fields.

    DECISION (maintainer): a billed session/expense REFUSES edits to the
    fields that feed its parent invoice's total — hours/work_date for a
    session, amount/expense_date for an expense — REGARDLESS of --force.
    Only --notes (session) / --description (expense) may be edited on a
    billed row, and that edit requires no --force at all. There is no
    auto-regenerate; the refusal simply leaves everything untouched.
    """

    @staticmethod
    def _session_row(db_path, session_id):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()

    @staticmethod
    def _expense_row(db_path, expense_id):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM expenses WHERE id = ?", (expense_id,)
            ).fetchone()
        finally:
            conn.close()

    @staticmethod
    def _billed_session_ids(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM sessions WHERE invoice_id IS NOT NULL ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()

    @staticmethod
    def _billed_expense_ids(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM expenses WHERE invoice_id IS NOT NULL ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()

    def test_edit_hours_on_billed_session_is_refused(self):
        """--hours on a billed session is refused even with --force; nothing
        changes in the session row or the parent invoice's total."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            self._bill(runner)
            invoice_number = self._invoice_row(db_path)["invoice_number"]
            original_total = self._invoice_row(db_path)["total"]
            original_csv_total = self._csv_total(ledger_file, invoice_number)

            session_ids = self._billed_session_ids(db_path)
            self.assertTrue(session_ids, "expected at least one billed session")
            session_id = session_ids[0]
            before = self._session_row(db_path, session_id)

            result = runner.invoke(
                zd.cli,
                ["edit", str(session_id), "--hours", "6", "--force"],
            )

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(str(session_id), result.output)
            self.assertIn(invoice_number, result.output)
            self.assertIn("locked", result.output)

            after = self._session_row(db_path, session_id)
            self.assertEqual(after["hours"], before["hours"])
            self.assertEqual(
                self._invoice_row(db_path)["total"], original_total,
                "invoices.total must not go stale",
            )
            self.assertEqual(
                self._csv_total(ledger_file, invoice_number),
                original_csv_total,
                "CSV ledger total must not go stale",
            )

    def test_edit_date_on_billed_session_is_refused(self):
        """--date on a billed session is refused even with --force."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            self._bill(runner)
            invoice_number = self._invoice_row(db_path)["invoice_number"]
            original_total = self._invoice_row(db_path)["total"]
            original_csv_total = self._csv_total(ledger_file, invoice_number)

            session_id = self._billed_session_ids(db_path)[0]
            before = self._session_row(db_path, session_id)

            result = runner.invoke(
                zd.cli,
                ["edit", str(session_id), "--date", "2026-05-05", "--force"],
            )

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(invoice_number, result.output)
            self.assertIn("locked", result.output)

            after = self._session_row(db_path, session_id)
            self.assertEqual(after["work_date"], before["work_date"])
            self.assertEqual(self._invoice_row(db_path)["total"], original_total)
            self.assertEqual(
                self._csv_total(ledger_file, invoice_number), original_csv_total
            )

    def test_edit_notes_on_billed_session_succeeds_without_force(self):
        """--notes alone on a billed session succeeds with NO --force; the
        invoice total is untouched since notes are not billing-relevant."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            self._bill(runner)
            invoice_number = self._invoice_row(db_path)["invoice_number"]
            original_total = self._invoice_row(db_path)["total"]

            session_id = self._billed_session_ids(db_path)[0]

            result = runner.invoke(
                zd.cli,
                ["edit", str(session_id), "--notes", "corrected note"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            after = self._session_row(db_path, session_id)
            self.assertEqual(after["notes"], "corrected note")
            self.assertEqual(
                self._invoice_row(db_path)["total"], original_total,
                "a notes-only edit must never alter the billed total",
            )

    def _seed_with_expense(self, tmpdir, rate=100.00, expense_amount=75.00):
        """Seed Acme (two unbilled sessions, per _seed) plus one unbilled
        expense, so billing produces both a billed session and a billed
        expense to exercise the lock on."""
        db_path, config_path, ledger_file, invoices_dir = self._seed(tmpdir, rate=rate)
        with zd.get_conn() as conn:
            acme_id = conn.execute(
                "SELECT id FROM clients WHERE slug = ?", ("acme",)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO expenses (client_id, expense_date, amount, description) "
                "VALUES (?,?,?,?)",
                (acme_id, "2026-04-10", expense_amount, "pre-existing expense"),
            )
        return db_path, config_path, ledger_file, invoices_dir

    def test_edit_expense_amount_on_billed_expense_is_refused(self):
        """--amount on a billed expense is refused even with --force."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed_with_expense(tmpdir, rate=100.00)
            runner = CliRunner()

            self._bill(runner)
            invoice_number = self._invoice_row(db_path)["invoice_number"]
            original_total = self._invoice_row(db_path)["total"]
            original_csv_total = self._csv_total(ledger_file, invoice_number)

            expense_ids = self._billed_expense_ids(db_path)
            self.assertTrue(expense_ids, "expected at least one billed expense")
            expense_id = expense_ids[0]
            before = self._expense_row(db_path, expense_id)

            result = runner.invoke(
                zd.cli,
                ["edit-expense", str(expense_id), "--amount", "999", "--force"],
            )

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn(str(expense_id), result.output)
            self.assertIn(invoice_number, result.output)
            self.assertIn("locked", result.output)

            after = self._expense_row(db_path, expense_id)
            self.assertEqual(after["amount"], before["amount"])
            self.assertEqual(self._invoice_row(db_path)["total"], original_total)
            self.assertEqual(
                self._csv_total(ledger_file, invoice_number), original_csv_total
            )

    def test_edit_expense_description_on_billed_expense_succeeds_without_force(self):
        """--description alone on a billed expense succeeds with NO --force;
        the invoice total is untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, _ = self._seed_with_expense(tmpdir, rate=100.00)
            runner = CliRunner()

            self._bill(runner)
            invoice_number = self._invoice_row(db_path)["invoice_number"]
            original_total = self._invoice_row(db_path)["total"]

            expense_id = self._billed_expense_ids(db_path)[0]

            result = runner.invoke(
                zd.cli,
                ["edit-expense", str(expense_id), "--description", "fixed"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            after = self._expense_row(db_path, expense_id)
            self.assertEqual(after["description"], "fixed")
            self.assertEqual(
                self._invoice_row(db_path)["total"], original_total,
                "a description-only edit must never alter the billed total",
            )

    def test_edit_unbilled_session_still_works(self):
        """Regression: the billed-row lock must not spill onto unbilled rows;
        `zd edit <unbilled-id> --hours ...` keeps working exactly as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, _, _ = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            # Nothing has been billed yet — both seeded sessions are unbilled.
            conn = sqlite3.connect(db_path)
            try:
                session_id = conn.execute(
                    "SELECT id FROM sessions WHERE invoice_id IS NULL ORDER BY id LIMIT 1"
                ).fetchone()[0]
            finally:
                conn.close()

            result = runner.invoke(
                zd.cli, ["edit", str(session_id), "--hours", "4"],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            after = self._session_row(db_path, session_id)
            self.assertEqual(after["hours"], 4.0)


if __name__ == "__main__":
    unittest.main()
