"""zd.get_conn() connections close when their ``with`` block ends.

A plain sqlite3 connection's ``with`` block only commits or rolls back. zd's
callers all use ``with zd.get_conn() as conn:``, so connections stayed open
until garbage collection, which raised ResourceWarnings and let a late GC
checkpoint the WAL and rewrite the DB file after a test had snapshotted it.
"""

import tests  # noqa: F401 - HOME/log isolation guard (see tests/__init__.py)

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zd


class GetConnTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "zd.db"
        patcher = patch.object(zd, "ZD_DB", db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        zd.init_db()

    def _client_count(self):
        conn = sqlite3.connect(zd.ZD_DB)
        try:
            return conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        finally:
            conn.close()

    def _assert_closed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_with_block_commits_then_closes(self):
        with zd.get_conn() as conn:
            conn.execute("INSERT INTO clients (slug, name, rate) VALUES ('acme', 'Acme', 100)")

        self._assert_closed(conn)
        self.assertEqual(self._client_count(), 1)

    def test_with_block_rolls_back_and_closes_on_error(self):
        with self.assertRaises(RuntimeError):
            with zd.get_conn() as conn:
                conn.execute("INSERT INTO clients (slug, name, rate) VALUES ('acme', 'Acme', 100)")
                raise RuntimeError("boom")

        self._assert_closed(conn)
        self.assertEqual(self._client_count(), 0)

    def test_readonly_connection_closes_too(self):
        with zd.get_conn(readonly=True) as conn:
            conn.execute("SELECT COUNT(*) FROM clients").fetchone()

        self._assert_closed(conn)

    def test_connection_without_with_block_stays_open_for_the_caller(self):
        conn = zd.get_conn(readonly=True)
        try:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
