"""INV-6 gate: CSV ledger writes must be atomic, locked, and backed up.

This is the shared home for write-safety regression tests. `save_to_csv`
rewrites the whole ledger via an atomic os.replace so a crash mid-write can
never leave a torn/partial row. Other write-safety tasks add cases here.
"""

import csv
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import invoice
import zd


class WriteSafetyTests(unittest.TestCase):
    def _config(self, tmpdir):
        """Minimal config pointing the ledger at a temp path (never the real one)."""
        ledger_file = Path(tmpdir) / "invoices.csv"
        return {
            "payee": {"name": "Zero Delta LLC"},
            "clients": [{"name": "Acme Corp"}],
            "storage": {
                "ledger_file": str(ledger_file),
                "invoices_dir": str(Path(tmpdir) / "invoices"),
            },
        }

    def _line_items(self):
        return [{"description": "Consulting", "hours": 2, "rate": 100.0}]

    def _read_rows(self, path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def test_happy_path_appends_well_formed_row(self):
        """A normal save writes the header + row and reloads cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])

            invoice.save_to_csv(
                "2026-0001",
                "2026-03-14",
                config,
                self._line_items(),
                200.00,
                "/tmp/invoice-2026-0001.pdf",
                client={"name": "Acme Corp"},
                status="Draft",
            )

            self.assertTrue(ledger.exists())
            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["invoice_number"], "2026-0001")
            self.assertEqual(rows[0]["payer_name"], "Acme Corp")
            self.assertEqual(rows[0]["total"], "200.00")
            self.assertEqual(rows[0]["status"], "Draft")
            self.assertEqual(
                rows[0]["line_items"], "Consulting (2 hrs @ $100.00/hr)"
            )

    def test_second_save_appends_without_losing_first_row(self):
        """Rewriting the whole file must preserve previously saved rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])

            invoice.save_to_csv(
                "2026-0001", "2026-03-14", config, self._line_items(),
                200.00, "/tmp/a.pdf", client={"name": "Acme Corp"},
            )
            invoice.save_to_csv(
                "2026-0002", "2026-03-15", config, self._line_items(),
                300.00, "/tmp/b.pdf", client={"name": "Acme Corp"},
            )

            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [r["invoice_number"] for r in rows],
                ["2026-0001", "2026-0002"],
            )

    def test_mid_write_failure_leaves_original_ledger_intact(self):
        """If os.replace raises mid-write, the atomic swap guarantees the
        original ledger is untouched — no torn or partial row lands on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])

            # Seed one committed invoice via the real code path.
            invoice.save_to_csv(
                "2026-0001", "2026-03-14", config, self._line_items(),
                200.00, "/tmp/a.pdf", client={"name": "Acme Corp"},
            )
            original_bytes = ledger.read_bytes()

            # Simulate a crash at the atomic-swap boundary.
            with patch.object(invoice.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    invoice.save_to_csv(
                        "2026-0002", "2026-03-15", config, self._line_items(),
                        300.00, "/tmp/b.pdf", client={"name": "Acme Corp"},
                    )

            # Original ledger is byte-for-byte intact: all-or-nothing.
            self.assertEqual(ledger.read_bytes(), original_bytes)
            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["invoice_number"], "2026-0001")

            # No stray temp files left behind in the ledger directory.
            leftover = [
                p for p in ledger.parent.iterdir()
                if p.name != ledger.name and not p.name.endswith(".bak")
            ]
            self.assertEqual(leftover, [], f"unexpected leftover files: {leftover}")

    def test_zero_hours_zero_rate_line_item_omits_hours_rate_suffix(self):
        """A flat-rate line item (hours == 0 and rate == 0) is written with a
        clean description and NO '(0 hrs @ $0.00/hr)' suffix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])

            invoice.save_to_csv(
                "2026-0003",
                "2026-03-16",
                config,
                [{"description": "Fixed-price engagement", "hours": 0, "rate": 0}],
                5000.00,
                "/tmp/flat.pdf",
                client={"name": "Acme Corp"},
            )

            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["line_items"], "Fixed-price engagement")
            self.assertNotIn("0 hrs", rows[0]["line_items"])
            self.assertNotIn("$0.00/hr", rows[0]["line_items"])

    def test_normal_line_item_keeps_hours_rate_suffix(self):
        """A normal (non zero/zero) line item still carries its suffix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            ledger = Path(config["storage"]["ledger_file"])

            invoice.save_to_csv(
                "2026-0004",
                "2026-03-17",
                config,
                [{"description": "Advisory", "hours": 3, "rate": 150.0}],
                450.00,
                "/tmp/normal.pdf",
                client={"name": "Acme Corp"},
            )

            rows = self._read_rows(ledger)
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["line_items"], "Advisory (3 hrs @ $150.00/hr)"
            )


# ---------------------------------------------------------------------------
# Task C.10 / H.3 — zd DB backups via the SQLite online-backup API, and only
# on write paths (read-only commands + tab-completion must not churn the
# _MAX_BACKUPS retention window).
# ---------------------------------------------------------------------------


class ZdDbBackupSafetyTests(unittest.TestCase):
    def _seed_zd_db(self, tmpdir):
        """Seed a zd DB (with one client + one unbilled session) and point
        zd's module globals at temp paths, mirroring test_zd_invoice.py's
        _seed_invoice_data pattern."""
        db_path = Path(tmpdir) / "zd.db"
        config_path = Path(tmpdir) / ".invoice_config.json"
        config = {
            "storage": {
                "ledger_file": str(Path(tmpdir) / "invoices.csv"),
                "invoices_dir": str(Path(tmpdir) / "invoices"),
            },
            "clients": [],
            "zd": {"weekly_summaries": {"enabled": False}},
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")

        patchers = [
            patch.object(zd, "ZD_DB", db_path),
            patch.object(zd, "CONFIG_FILE", config_path),
            patch.dict(os.environ, {"HOME": tmpdir}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        zd.init_db()
        # Seed via a readonly connection so this setup step itself never
        # counts as a "write command" backup — it mirrors how a test fixture
        # loads data, not the write path under test.
        with zd.get_conn(readonly=True) as conn:
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("acme", "Acme Corp", 100.00),
            )
            conn.commit()
        # Belt-and-suspenders: clear the run-scoped backup memory and remove
        # any stray .bak file so each test starts from a verifiably clean
        # slate for the specific command under test.
        zd._backed_up_this_run.clear()
        for stray in self._bak_files(db_path):
            stray.unlink()
        return db_path

    def _bak_files(self, db_path):
        return list(db_path.parent.glob(f"{db_path.name}.*.bak"))

    def test_write_command_creates_a_valid_consistent_db_backup(self):
        """After a write command, a *.bak DB backup exists AND is a valid
        SQLite file that opens and contains the same tables/rows as the
        source (a real consistent snapshot, not an empty/corrupt file)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._seed_zd_db(tmpdir)
            from click.testing import CliRunner
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["log", "acme", "1.5", "reviewed contracts"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

            backups = self._bak_files(db_path)
            self.assertTrue(backups, "expected a .bak DB backup after a write command")

            # The backup is a genuine SQLite DB: it opens and its tables/rows
            # match the state of the source at backup time — one client row,
            # the same client, and the source's post-write session count is
            # AT LEAST what was in the backup (backup happens before the
            # session insert, so the backup must have 0 sessions and the
            # source now has 1).
            backup_path = backups[0]
            bak_conn = sqlite3.connect(backup_path)
            try:
                bak_conn.row_factory = sqlite3.Row
                tables = {
                    row[0] for row in bak_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("clients", tables)
                self.assertIn("sessions", tables)
                self.assertIn("invoices", tables)
                client_row = bak_conn.execute(
                    "SELECT slug, name, rate FROM clients WHERE slug = ?", ("acme",)
                ).fetchone()
                self.assertIsNotNone(client_row)
                self.assertEqual(client_row["name"], "Acme Corp")
                # Consistent pre-write snapshot: the backup was taken before
                # `zd log` inserted its session row.
                backup_session_count = bak_conn.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0]
                self.assertEqual(backup_session_count, 0)
            finally:
                bak_conn.close()

            with zd.get_conn(readonly=True) as conn:
                source_session_count = conn.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0]
            self.assertEqual(source_session_count, 1)

    def test_write_command_creates_exactly_one_db_backup_per_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._seed_zd_db(tmpdir)
            from click.testing import CliRunner
            runner = CliRunner()

            result = runner.invoke(zd.cli, ["log", "acme", "1.0", "quick call"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

            self.assertEqual(len(self._bak_files(db_path)), 1)

    def test_read_command_creates_no_db_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._seed_zd_db(tmpdir)
            from click.testing import CliRunner
            runner = CliRunner()

            before = self._bak_files(db_path)
            self.assertEqual(before, [], "no backups should exist before the read command")

            result = runner.invoke(zd.cli, ["status"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

            result = runner.invoke(zd.cli, ["sessions"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

            result = runner.invoke(zd.cli, ["clients"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

            after = self._bak_files(db_path)
            self.assertEqual(after, [], "read-only commands must not create a DB backup")

    def test_init_db_alone_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._seed_zd_db(tmpdir)

            before = self._bak_files(db_path)
            self.assertEqual(before, [])

            zd.init_db()

            after = self._bak_files(db_path)
            self.assertEqual(
                after, [], "init_db() alone on an existing DB must not create a backup"
            )

    def test_backup_db_is_noop_on_fresh_empty_database(self):
        """A brand-new DB with no user tables yet has nothing worth
        snapshotting; _backup_db must no-op rather than back up an empty
        shell."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "empty.db"
            conn = sqlite3.connect(db_path)
            try:
                zd._backup_db(conn, db_path)
            finally:
                conn.close()

            self.assertEqual(self._bak_files(db_path), [])


if __name__ == "__main__":
    unittest.main()
