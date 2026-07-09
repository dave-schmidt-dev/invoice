"""INV-6 gate: CSV ledger writes must be atomic, locked, and backed up.

This is the shared home for write-safety regression tests. `save_to_csv`
rewrites the whole ledger via an atomic os.replace so a crash mid-write can
never leave a torn/partial row. Other write-safety tasks add cases here.
"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import invoice


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


if __name__ == "__main__":
    unittest.main()
