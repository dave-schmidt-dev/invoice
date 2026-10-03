"""Unit tests for invoice_ledger.update_ledger_rows, the one CSV-ledger patch path (INV-6).

Every ledger row patch (invoice.py status, zd paid, zd reconcile, zd invoice
--regenerate) goes through this helper: locked, backed up before the rewrite,
atomic, legacy-header aware. These tests drive it against temp files only.
"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import invoice_ledger
from invoice_ledger import update_ledger_rows

HEADERS = ["invoice_number", "date", "total", "status"]


def _write(path, rows, headers=HEADERS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


class UpdateLedgerRowsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "invoices.csv"
        self.seen_backups = []

    def _backup(self, path):
        # Record what the file looked like when the backup ran: it must still
        # be the pre-edit state.
        self.seen_backups.append(Path(path).read_bytes())

    def test_patches_only_the_matching_row_and_returns_matched_numbers(self):
        _write(self.path, [
            {"invoice_number": "2026-0001", "date": "d1", "total": "1.00", "status": "Sent"},
            {"invoice_number": "2026-0002", "date": "d2", "total": "2.00", "status": "Sent"},
        ])

        matched = update_ledger_rows(self.path, {"2026-0002": {"status": "Paid"}})

        self.assertEqual(matched, {"2026-0002"})
        rows, headers = _read(self.path)
        self.assertEqual([r["status"] for r in rows], ["Sent", "Paid"])
        self.assertEqual(headers, HEADERS)

    def test_no_match_returns_empty_and_leaves_file_and_backups_untouched(self):
        _write(self.path, [
            {"invoice_number": "2026-0001", "date": "d1", "total": "1.00", "status": "Sent"},
        ])
        before = self.path.read_bytes()

        matched = update_ledger_rows(
            self.path, {"2026-9999": {"status": "Paid"}}, backup=self._backup
        )

        self.assertEqual(matched, set())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.seen_backups, [])

    def test_backup_runs_once_before_the_rewrite_on_match(self):
        _write(self.path, [
            {"invoice_number": "2026-0001", "date": "d1", "total": "1.00", "status": "Sent"},
        ])
        before = self.path.read_bytes()

        update_ledger_rows(self.path, {"2026-0001": {"status": "Paid"}}, backup=self._backup)

        self.assertEqual(self.seen_backups, [before])
        self.assertNotEqual(self.path.read_bytes(), before)

    def test_runs_under_the_ledger_lock(self):
        _write(self.path, [
            {"invoice_number": "2026-0001", "date": "d1", "total": "1.00", "status": "Sent"},
        ])
        events = []
        real_lock = invoice_ledger._file_lock

        from contextlib import contextmanager

        @contextmanager
        def spy_lock(target):
            events.append(("lock", Path(target)))
            with real_lock(target):
                yield
            events.append(("unlock", Path(target)))

        def backup(path):
            events.append(("backup", None))

        with patch.object(invoice_ledger, "_file_lock", spy_lock):
            update_ledger_rows(self.path, {"2026-0001": {"status": "Paid"}}, backup=backup)

        self.assertEqual(
            events,
            [("lock", self.path), ("backup", None), ("unlock", self.path)],
        )

    def test_legacy_headers_are_resolved_for_number_and_fields(self):
        legacy = ["Invoice Number", "Date", "Total", "Status"]
        _write(self.path, [
            {"Invoice Number": "2026-0001", "Date": "d1", "Total": "1.00", "Status": "Sent"},
        ], headers=legacy)

        matched = update_ledger_rows(
            self.path, {"2026-0001": {"status": "Paid", "total": "9.00"}}
        )

        self.assertEqual(matched, {"2026-0001"})
        rows, headers = _read(self.path)
        self.assertEqual(headers, legacy)
        self.assertEqual((rows[0]["Status"], rows[0]["Total"]), ("Paid", "9.00"))

    def test_first_match_only_default_patches_one_row_per_number(self):
        dup = {"invoice_number": "2026-0001", "date": "d", "total": "1.00", "status": "Sent"}
        _write(self.path, [dict(dup), dict(dup)])

        update_ledger_rows(self.path, {"2026-0001": {"status": "Paid"}})

        rows, _ = _read(self.path)
        self.assertEqual([r["status"] for r in rows], ["Paid", "Sent"])

    def test_all_matches_when_first_match_only_is_false_and_many_numbers(self):
        _write(self.path, [
            {"invoice_number": "2026-0001", "date": "d", "total": "1.00", "status": "Sent"},
            {"invoice_number": "2026-0002", "date": "d", "total": "2.00", "status": "Sent"},
            {"invoice_number": "2026-0001", "date": "d", "total": "1.00", "status": "Sent"},
            {"invoice_number": "2026-0003", "date": "d", "total": "3.00", "status": "Sent"},
        ])
        updates = {n: {"status": "Paid"} for n in ("2026-0001", "2026-0002")}

        matched = update_ledger_rows(self.path, updates, first_match_only=False)

        self.assertEqual(matched, {"2026-0001", "2026-0002"})
        rows, _ = _read(self.path)
        self.assertEqual([r["status"] for r in rows], ["Paid", "Paid", "Paid", "Sent"])

    def test_missing_column_is_added_only_when_asked(self):
        no_status = ["invoice_number", "date", "total"]
        row = {"invoice_number": "2026-0001", "date": "d", "total": "1.00"}
        _write(self.path, [row], headers=no_status)

        update_ledger_rows(
            self.path, {"2026-0001": {"status": "Paid"}}, add_missing_columns=True
        )

        rows, headers = _read(self.path)
        self.assertEqual(headers, no_status + ["status"])
        self.assertEqual(rows[0]["status"], "Paid")

    def test_missing_column_without_opt_in_fails_and_leaves_ledger_intact(self):
        no_status = ["invoice_number", "date", "total"]
        _write(self.path, [{"invoice_number": "2026-0001", "date": "d", "total": "1.00"}],
               headers=no_status)
        before = self.path.read_bytes()

        with self.assertRaises(ValueError):
            update_ledger_rows(self.path, {"2026-0001": {"status": "Paid"}})

        self.assertEqual(self.path.read_bytes(), before)

    def test_write_unmatched_backs_up_before_read_and_rewrites(self):
        _write(self.path, [
            {"invoice_number": "2026-0001", "date": "d1", "total": "1.00", "status": "Sent"},
        ])
        before = self.path.read_bytes()

        matched = update_ledger_rows(
            self.path, {"2026-9999": {"total": "5.00"}},
            backup=self._backup, write_unmatched=True,
        )

        self.assertEqual(matched, set())
        self.assertEqual(self.seen_backups, [before])
        rows, _ = _read(self.path)
        self.assertEqual(rows[0]["total"], "1.00")


if __name__ == "__main__":
    unittest.main()
