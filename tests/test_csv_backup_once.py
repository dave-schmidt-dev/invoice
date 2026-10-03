"""INV-6: one `zd` run backs up the same ledger CSV once.

invoice.py is fresh-loaded on every use. Its once-per-run memory used to be
re-created on each load while zd's persisted, so a run that appended a missing
row through ``invoice.save_to_csv`` (convergence) and then patched the same
ledger through ``zd._backup_file`` (``zd paid``) copied it twice. Both now use
the single ``invoice_ledger._backup_file``.
"""

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import zd
from invoice_ledger import CSV_HEADERS


class CsvBackupCountTests(unittest.TestCase):
    def _sandbox(self, tmpdir):
        """DB with one Sent invoice (2026-0001) that the CSV ledger lacks."""
        tmp = Path(tmpdir)
        csv_path = tmp / "invoices.csv"
        config_path = tmp / ".invoice_config.json"
        config_path.write_text(json.dumps({
            "payee": {"name": "Zero Delta LLC"},
            "clients": [{"name": "Acme Corp"}],
            "storage": {"ledger_file": str(csv_path), "invoices_dir": str(tmp / "invoices")},
        }), encoding="utf-8")
        for patcher in (
            patch.object(zd, "ZD_DB", tmp / "zd.db"),
            patch.object(zd, "CONFIG_FILE", config_path),
            patch.dict(os.environ, {"HOME": tmpdir}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        zd.init_db()
        with zd.get_conn(readonly=True) as conn:
            conn.execute("INSERT INTO clients (slug, name, rate) VALUES ('acme', 'Acme Corp', 100)")
            client_id = conn.execute("SELECT id FROM clients").fetchone()["id"]
            conn.execute(
                "INSERT INTO invoices (invoice_number, client_id, invoice_date, total, status)"
                " VALUES ('2026-0001', ?, '2026-05-01', 500.00, 'Sent')",
                (client_id,),
            )
            conn.commit()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            writer.writerow({
                "invoice_number": "2026-9999", "date": "2026-01-01", "payee_name": "Zero Delta LLC",
                "payer_name": "Acme Corp", "line_items": "", "total": "1.00", "pdf_file": "",
                "status": "Sent",
            })
        zd._backed_up_this_run.clear()
        return csv_path

    def test_paid_run_that_also_converges_backs_up_the_csv_once(self):
        """Convergence append + paid patch in one run share one backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = self._sandbox(tmpdir)
            real_copy2 = shutil.copy2
            csv_copies = []

            def counting_copy2(src, dst, *args, **kwargs):
                if Path(src) == csv_path:
                    csv_copies.append(Path(dst))
                return real_copy2(src, dst, *args, **kwargs)

            with patch("shutil.copy2", counting_copy2):
                result = CliRunner().invoke(zd.cli, ["paid", "2026-0001"])

            self.assertEqual(result.exit_code, 0, msg=result.output)
            with open(csv_path, newline="", encoding="utf-8") as f:
                rows = {r["invoice_number"]: r["status"] for r in csv.DictReader(f)}
            self.assertEqual(rows.get("2026-0001"), "Paid", msg=result.output)
            # Convergence appended the row via the fresh-loaded invoice module,
            # then cmd_paid patched it via zd._backup_file: one shared copy.
            self.assertEqual(len(csv_copies), 1, msg=result.output)


if __name__ == "__main__":
    unittest.main()
