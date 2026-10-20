"""Shared temporary invoice fixture helpers."""
import tests  # noqa: F401 - HOME/log isolation guard
import json
import os
from pathlib import Path
from unittest.mock import patch
import zd

class ZdInvoiceFixtureMixin:

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
