"""INV-2 gate: DB-authoritative write ordering for `zd invoice` (new-invoice path).

These tests lock in the transaction-integrity contract of Task A.3:

  * The SQLite COMMIT is the single point of no return. The ONLY durable
    artifact created before that commit is a TEMP PDF at a non-final path.
  * A failure BEFORE the commit leaves NO final PDF, NO CSV ledger row, and
    the client's sessions still unbilled (invoice_id IS NULL). Any leftover
    temp PDF is cleaned up. Rerunning would therefore not double-bill.
  * A clean run bills exactly once: sessions get invoice_id set AND their
    billed_rate snapshotted, exactly one CSV row is appended, and the final
    PDF exists.
  * A duplicate invoice number aborts before any durable write (INV-5).

The harness mirrors tests/test_zd_invoice.py: TemporaryDirectory, HOME/ZD_DB/
CONFIG_FILE patched to the sandbox, a storage.ledger_file CSV, and CliRunner
with input="y\n" to answer the "Proceed?" confirmation. No real user files are
ever touched.
"""

import csv as _csv
import glob
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import zd


class _LedgerRaceLoader:
    """Wraps the real invoice.py loader to simulate a concurrent ledger writer.

    cmd_invoice loads invoice.py fresh each call via
    importlib.util.spec_from_file_location -> module_from_spec ->
    loader.exec_module. Its invoice-number generator is collision-avoiding by
    construction (the next number is max(existing)+1 across BOTH the CSV ledger
    and the zd DB), so a duplicate can never arise in single-process use. The
    Step-1 INV-5 guard therefore only ever fires on a RACE: another `zd invoice`
    (or a manual ledger edit) appends the next number AFTER cmd_invoice's
    max-scan but BEFORE its dup-check under the ledger lock.

    We reproduce that race by wrapping `_read_csv_with_headers` so the FIRST
    read (the numbering max-scan) sees the ledger as-seeded, but every read
    after it (the Step-1 dup-check read) returns an EXTRA row carrying the very
    number cmd_invoice is about to try. The on-disk CSV is never modified — the
    injection lives only in the returned rows — so a passing test also proves
    the guard aborted before any durable write. Every other invoice.py helper
    is left untouched.
    """

    def __init__(self, inner, raced_number):
        self._inner = inner
        self._raced = raced_number

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        real_read = module._read_csv_with_headers
        raced = self._raced
        state = {"reads": 0}

        def racing_read(csv_path):
            rows, headers = real_read(csv_path)
            state["reads"] += 1
            # Read #1 = numbering max-scan (sees the ledger as-seeded). Read #2+
            # = the dup-check read, where a competing writer has "already"
            # appended `raced`, forcing the collision the guard must catch.
            if state["reads"] >= 2:
                inv_key = (
                    module._csv_field_key(headers, "invoice_number")
                    or "invoice_number"
                )
                extra = {h: "" for h in headers}
                extra[inv_key] = raced
                rows = list(rows) + [extra]
            return rows, headers

        module._read_csv_with_headers = racing_read


class InvoiceTransactionIntegrityTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Sandbox harness (same shape as ZdInvoiceTests)
    # ------------------------------------------------------------------
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
        """Seed a client (Acme) with two unbilled sessions + one expense."""
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
            conn.execute(
                "INSERT INTO expenses (client_id, expense_date, amount, description) VALUES (?,?,?,?)",
                (acme_id, "2026-04-10", 40.00, "filing fee"),
            )
        return db_path, config_path, ledger_file, invoices_dir

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _unbilled_session_count(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE invoice_id IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _invoice_count(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _csv_rows(ledger_file):
        if not Path(ledger_file).exists():
            return []
        with open(ledger_file, newline="", encoding="utf-8-sig") as f:
            return list(_csv.DictReader(f))

    @staticmethod
    def _pdf_files(invoices_dir):
        if not Path(invoices_dir).exists():
            return []
        return sorted(p.name for p in Path(invoices_dir).glob("*.pdf"))

    @staticmethod
    def _temp_pdf_files(invoices_dir):
        if not Path(invoices_dir).exists():
            return []
        return sorted(
            os.path.basename(p)
            for p in glob.glob(str(Path(invoices_dir) / "*.tmp-*"))
        )

    # ------------------------------------------------------------------
    # 1. Failure BEFORE the commit -> nothing durable leaks
    # ------------------------------------------------------------------
    def test_failure_before_commit_leaves_no_durable_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, invoices_dir = self._seed(tmpdir)

            # A connection factory whose commit() always raises. The INSERT and
            # UPDATEs run (in an open, UNCOMMITTED transaction), but the point
            # of no return is never crossed: cmd_invoice's handler must
            # rollback and delete the temp PDF.
            class FailingCommitConn(sqlite3.Connection):
                def commit(self):  # noqa: D401 - injected failure
                    raise RuntimeError("injected commit failure")

            real_connect = zd.sqlite3.connect

            def failing_connect(*args, **kwargs):
                kwargs.setdefault("factory", FailingCommitConn)
                return real_connect(*args, **kwargs)

            runner = CliRunner()
            with patch.object(zd.sqlite3, "connect", side_effect=failing_connect):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--date", "2026-04-30"],
                    input="y\n",
                )

            # The command must fail (non-zero exit), NOT silently succeed.
            self.assertNotEqual(result.exit_code, 0, msg=result.output)

            # The failure must be the INJECTED COMMIT — the point of no return —
            # proving the pre-commit transaction path was actually reached and
            # rolled back. Without this the test would pass VACUOUSLY on a runner
            # where invoice.py can't import (e.g. fpdf2 absent), because the
            # command would exit before ever opening the transaction.
            self.assertIsInstance(result.exception, RuntimeError, msg=result.output)
            self.assertIn("injected commit failure", str(result.exception))

            # No invoice row was committed to the DB.
            self.assertEqual(self._invoice_count(db_path), 0, msg=result.output)

            # Sessions remain unbilled -> a rerun cannot double-bill.
            self.assertEqual(self._unbilled_session_count(db_path), 2, msg=result.output)

            # No CSV ledger row was written.
            self.assertEqual(self._csv_rows(ledger_file), [], msg=result.output)

            # No FINAL PDF at the target path.
            self.assertEqual(self._pdf_files(invoices_dir), [], msg=result.output)

            # The temp PDF was cleaned up (no *.tmp-<pid> left behind).
            self.assertEqual(self._temp_pdf_files(invoices_dir), [], msg=result.output)

    # ------------------------------------------------------------------
    # 2. Clean run -> bills exactly once, snapshots billed_rate
    # ------------------------------------------------------------------
    def test_clean_run_bills_exactly_once_and_snapshots_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, invoices_dir = self._seed(tmpdir, rate=100.00)
            runner = CliRunner()

            result = runner.invoke(
                zd.cli,
                ["invoice", "acme", "--date", "2026-04-30"],
                input="y\n",
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)

            # Exactly one invoice, zero unbilled sessions.
            self.assertEqual(self._invoice_count(db_path), 1)
            self.assertEqual(self._unbilled_session_count(db_path), 0)

            # billed_rate snapshotted onto every billed session.
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                sessions = conn.execute(
                    "SELECT invoice_id, billed_rate FROM sessions ORDER BY work_date"
                ).fetchall()
                inv = conn.execute(
                    "SELECT status, billing_mode FROM invoices"
                ).fetchone()
            finally:
                conn.close()
            self.assertTrue(all(s["invoice_id"] is not None for s in sessions))
            self.assertTrue(all(s["billed_rate"] == 100.00 for s in sessions))
            self.assertEqual(inv["status"], "Sent")
            self.assertEqual(inv["billing_mode"], "hourly")

            # Exactly one CSV row, status Sent, and it exists as a final PDF.
            csv_rows = self._csv_rows(ledger_file)
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(csv_rows[0]["status"], "Sent")

            pdfs = self._pdf_files(invoices_dir)
            self.assertEqual(len(pdfs), 1)
            # No leftover temp PDF from the atomic rename.
            self.assertEqual(self._temp_pdf_files(invoices_dir), [])

    # ------------------------------------------------------------------
    # 3. Duplicate invoice number aborts before any durable write (INV-5)
    # ------------------------------------------------------------------
    def test_duplicate_invoice_number_aborts_before_durable_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, invoices_dir = self._seed(tmpdir)

            # Pre-seed the CSV ledger with 2026-0001 (DB has no invoices), so
            # cmd_invoice's max-scan deterministically computes the next number
            # as 2026-0002. Then simulate a concurrent writer that appends that
            # very 2026-0002 between the max-scan and the Step-1 dup-check (see
            # _LedgerRaceLoader). The guard must catch the collision against the
            # CSV ledger and abort BEFORE any durable write.
            seeded_number = "2026-0001"
            raced_number = "2026-0002"  # = seeded max (1) + 1, the number tried
            with open(ledger_file, "w", newline="", encoding="utf-8") as f:
                writer = _csv.DictWriter(
                    f,
                    fieldnames=[
                        "invoice_number", "date", "payee_name", "payer_name",
                        "line_items", "total", "pdf_file", "status",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "invoice_number": seeded_number,
                    "date": "2026-01-01",
                    "payee_name": "Zero Delta LLC",
                    "payer_name": "Acme Corp",
                    "line_items": "prior work",
                    "total": "10.00",
                    "pdf_file": "old.pdf",
                    "status": "Sent",
                })

            invoices_before = self._invoice_count(db_path)
            unbilled_before = self._unbilled_session_count(db_path)
            csv_rows_before = self._csv_rows(ledger_file)

            real_spec_from_file = importlib.util.spec_from_file_location

            def racing_spec(name, path, *args, **kwargs):
                spec = real_spec_from_file(name, path, *args, **kwargs)
                spec.loader = _LedgerRaceLoader(spec.loader, raced_number)
                return spec

            runner = CliRunner()
            with patch.object(
                importlib.util, "spec_from_file_location", side_effect=racing_spec
            ):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--date", "2026-04-30"],
                    input="y\n",
                )

            # Aborted with a clear error, no new invoice, sessions untouched.
            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("already exists", result.output)
            self.assertEqual(self._invoice_count(db_path), invoices_before)
            self.assertEqual(self._unbilled_session_count(db_path), unbilled_before)

            # No durable projection leaked: the CSV is byte-for-byte unchanged
            # (no appended row), and no PDF (final or temp) was created.
            self.assertEqual(self._csv_rows(ledger_file), csv_rows_before)
            self.assertEqual(self._pdf_files(invoices_dir), [])
            self.assertEqual(self._temp_pdf_files(invoices_dir), [])


    # ------------------------------------------------------------------
    # 4. Post-commit PDF-finalize failure -> DB stays authoritative, and the
    #    CSV row is NOT written (never references a missing PDF).
    # ------------------------------------------------------------------
    def test_pdf_finalize_failure_skips_csv_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _, ledger_file, invoices_dir = self._seed(tmpdir)

            real_replace = os.replace

            def failing_replace(src, dst, *a, **k):
                # Fail ONLY the step-4 temp->final PDF rename; let every other
                # atomic replace proceed normally.
                if ".tmp-" in str(src):
                    raise OSError("injected PDF finalize failure")
                return real_replace(src, dst, *a, **k)

            runner = CliRunner()
            with patch.object(zd.os, "replace", side_effect=failing_replace):
                result = runner.invoke(
                    zd.cli,
                    ["invoice", "acme", "--date", "2026-04-30"],
                    input="y\n",
                )

            # Handled gracefully (no crash): the commit succeeded, only the
            # projection failed.
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("projection is incomplete", result.output)

            # DB is authoritative: invoice committed, sessions billed.
            self.assertEqual(self._invoice_count(db_path), 1, msg=result.output)
            self.assertEqual(self._unbilled_session_count(db_path), 0, msg=result.output)

            # No CSV row (it would have referenced a missing PDF); no final PDF;
            # temp PDF cleaned up.
            self.assertEqual(self._csv_rows(ledger_file), [], msg=result.output)
            self.assertEqual(self._pdf_files(invoices_dir), [], msg=result.output)
            self.assertEqual(self._temp_pdf_files(invoices_dir), [], msg=result.output)


if __name__ == "__main__":
    unittest.main()
