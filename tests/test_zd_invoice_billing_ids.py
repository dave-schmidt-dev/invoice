"""zd invoice must bill exactly the rows it priced (review finding 1)."""
import tests  # noqa: F401 - HOME/log isolation guard
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import click
from click.testing import CliRunner
import zd
from tests.zd_invoice_fixture import ZdInvoiceFixtureMixin


class BillExactlyPricedRowsTests(ZdInvoiceFixtureMixin, unittest.TestCase):

    def _invoke_with_mid_prompt(self, mutate, args=None):
        """Run `zd invoice acme` and call mutate(conn) while "Proceed?" is open."""
        def confirm(*_a, **_k):
            with zd.get_conn() as conn:
                mutate(conn)
            return True

        with patch.object(click, "confirm", side_effect=confirm):
            return CliRunner().invoke(
                zd.cli, ["invoice", "acme", *(args or ["--date", "2026-04-30"])]
            )

    def test_session_logged_during_prompt_stays_unbilled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)

            def add_session(conn):
                cid = conn.execute("SELECT id FROM clients WHERE slug='acme'").fetchone()["id"]
                conn.execute(
                    "INSERT INTO sessions (client_id, work_date, hours, notes) VALUES (?,?,?,?)",
                    (cid, "2026-05-02", 5.0, "logged mid-prompt"),
                )

            result = self._invoke_with_mid_prompt(add_session)
            self.assertEqual(result.exit_code, 0, msg=result.output)
            with zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT invoice_id, billed_rate FROM sessions WHERE notes='logged mid-prompt'"
                ).fetchone()
                inv = conn.execute("SELECT total FROM invoices").fetchone()
            self.assertIsNone(row["invoice_id"])
            self.assertIsNone(row["billed_rate"])
            self.assertEqual(inv["total"], 1000.0 + 60.0)  # 10h priced + 3 expenses

    def test_priced_session_edited_out_of_month_is_still_billed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)

            def move_session(conn):
                conn.execute(
                    "UPDATE sessions SET work_date='2026-06-01' WHERE notes='april kickoff'"
                )

            result = self._invoke_with_mid_prompt(
                move_session, ["--month", "2026-04", "--date", "2026-04-30"]
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            with zd.get_conn() as conn:
                row = conn.execute(
                    "SELECT invoice_id FROM sessions WHERE notes='april kickoff'"
                ).fetchone()
            self.assertIsNotNone(row["invoice_id"], "priced session left unbilled")

    def test_row_billed_elsewhere_during_prompt_aborts_without_invoice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)

            def steal(conn):
                cid = conn.execute("SELECT id FROM clients WHERE slug='acme'").fetchone()["id"]
                conn.execute(
                    "INSERT INTO invoices (invoice_number, client_id, invoice_date, total, status)"
                    " VALUES ('2026-0099', ?, '2026-04-30', 1, 'Sent')", (cid,),
                )
                inv_id = conn.execute("SELECT id FROM invoices").fetchone()["id"]
                conn.execute(
                    "UPDATE sessions SET invoice_id=? WHERE notes='april closeout'", (inv_id,)
                )

            result = self._invoke_with_mid_prompt(steal)
            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            with zd.get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"]
                billed = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE invoice_id IS NOT NULL"
                ).fetchone()["n"]
            self.assertEqual(count, 1, "only the pre-existing invoice may exist")
            self.assertEqual(billed, 1, "nothing else may be billed")
            leftovers = list((Path(tmpdir) / "invoices").glob("*"))
            self.assertEqual(leftovers, [], "temp/final PDF must not remain")

    def test_existing_pdf_at_final_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._seed_invoice_data(tmpdir)
            pdf = Path(tmpdir) / "invoices" / "Acme_Corp_Invoice_2026-0001.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF other tool original\n")

            result = CliRunner().invoke(
                zd.cli, ["invoice", "acme", "--date", "2026-04-30"], input="y\n"
            )

            self.assertNotEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(pdf.read_bytes(), b"%PDF other tool original\n")
            with zd.get_conn() as conn:
                n = conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"]
            self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
