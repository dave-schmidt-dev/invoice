"""Focused invoice health tests."""
import tests  # noqa: F401 - HOME/log isolation guard
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zd


class ZdMigrationTests(unittest.TestCase):

    # ------------------------------------------------------------------
    # Task A.1 — idempotent schema migration (_migrate)
    # ------------------------------------------------------------------

    # The pre-migration ("v0") schema: the CREATE TABLE statements as they
    # existed before Task A.1 added paid_date / billing_mode / billed_rate.
    _OLD_SCHEMA = """
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            rate        REAL NOT NULL,
            created_at  TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER NOT NULL REFERENCES clients(id),
            work_date   TEXT NOT NULL,
            hours       REAL NOT NULL,
            notes       TEXT,
            invoice_id  INTEGER REFERENCES invoices(id),
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER NOT NULL REFERENCES clients(id),
            expense_date TEXT NOT NULL,
            amount      REAL NOT NULL,
            description TEXT,
            invoice_id  INTEGER REFERENCES invoices(id),
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number  TEXT UNIQUE NOT NULL,
            client_id       INTEGER NOT NULL REFERENCES clients(id),
            invoice_date    TEXT NOT NULL,
            total           REAL NOT NULL,
            status          TEXT DEFAULT 'Sent',
            pdf_path        TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """

    def _make_old_db(self, tmpdir, name="old.db", extra_sql=None):
        """Create a v0-schema DB (user_version 0) with one client + session.

        Returns the db path. `extra_sql` runs after the base schema so a
        test can simulate an out-of-band column (e.g. paid_date).
        """
        db_path = Path(tmpdir) / name
        conn = zd.sqlite3.connect(db_path)
        try:
            conn.executescript(self._OLD_SCHEMA)
            if extra_sql:
                conn.executescript(extra_sql)
            conn.execute(
                "INSERT INTO clients (slug, name, rate) VALUES (?,?,?)",
                ("acme", "Acme Corp", 100.00),
            )
            conn.execute(
                "INSERT INTO sessions (client_id, work_date, hours, notes) "
                "VALUES (?,?,?,?)",
                (1, "2026-04-01", 2.5, "kickoff"),
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    @staticmethod
    def _table_info(db_path, table):
        conn = zd.sqlite3.connect(db_path)
        try:
            return conn.execute(f"PRAGMA table_info({table})").fetchall()
        finally:
            conn.close()

    @staticmethod
    def _user_version(db_path):
        conn = zd.sqlite3.connect(db_path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_migrate_adds_all_new_columns_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_old_db(tmpdir)

            # Sanity: the old DB starts without the new columns, at version 0.
            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            sess_cols = {row[1] for row in self._table_info(db_path, "sessions")}
            self.assertNotIn("paid_date", inv_cols)
            self.assertNotIn("billing_mode", inv_cols)
            self.assertNotIn("billed_rate", sess_cols)
            self.assertEqual(self._user_version(db_path), 0)

            with patch.object(zd, "ZD_DB", db_path):
                conn = zd.sqlite3.connect(db_path)
                try:
                    zd._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            sess_cols = {row[1] for row in self._table_info(db_path, "sessions")}
            self.assertIn("paid_date", inv_cols)
            self.assertIn("billing_mode", inv_cols)
            self.assertIn("billed_rate", sess_cols)
            self.assertEqual(self._user_version(db_path), zd._SCHEMA_VERSION)

            # Existing rows survive; the DEFAULT applies to billing_mode.
            conn = zd.sqlite3.connect(db_path)
            conn.row_factory = zd.sqlite3.Row
            try:
                client = conn.execute("SELECT * FROM clients").fetchone()
                session = conn.execute("SELECT * FROM sessions").fetchone()
            finally:
                conn.close()
            self.assertEqual(client["name"], "Acme Corp")
            self.assertEqual(session["hours"], 2.5)
            self.assertEqual(session["notes"], "kickoff")
            self.assertIsNone(session["billed_rate"])

    def test_migrate_is_idempotent_on_repeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_old_db(tmpdir)

            with patch.object(zd, "ZD_DB", db_path):
                conn = zd.sqlite3.connect(db_path)
                try:
                    zd._migrate(conn)
                    conn.commit()
                    first_info = conn.execute(
                        "PRAGMA table_info(invoices)"
                    ).fetchall()
                    # Second run must be a no-op (fast path via user_version)
                    # and must not raise a duplicate-column error.
                    zd._migrate(conn)
                    conn.commit()
                    second_info = conn.execute(
                        "PRAGMA table_info(invoices)"
                    ).fetchall()
                finally:
                    conn.close()

            self.assertEqual(first_info, second_info)
            self.assertEqual(self._user_version(db_path), zd._SCHEMA_VERSION)

    def test_migrated_old_db_matches_fresh_init_db_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Fresh DB straight from init_db.
            fresh_path = Path(tmpdir) / "fresh.db"
            with patch.object(zd, "ZD_DB", fresh_path):
                zd.init_db()

            # Old DB brought forward via _migrate.
            old_path = self._make_old_db(tmpdir, name="migrated.db")
            with patch.object(zd, "ZD_DB", old_path):
                conn = zd.sqlite3.connect(old_path)
                try:
                    zd._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

            for table in ("invoices", "sessions"):
                fresh_info = self._table_info(fresh_path, table)
                migrated_info = self._table_info(old_path, table)
                self.assertEqual(
                    fresh_info,
                    migrated_info,
                    f"{table} schema diverged between fresh and migrated DB",
                )

            self.assertEqual(
                self._user_version(fresh_path),
                self._user_version(old_path),
            )
            self.assertEqual(self._user_version(fresh_path), zd._SCHEMA_VERSION)

    def test_migrate_skips_alter_for_preexisting_out_of_band_column(self):
        # Simulate the live DB, which already has paid_date added out-of-band
        # while user_version is still 0. _migrate must skip the paid_date
        # ALTER (guarded by table_info) and still add the other two columns.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_old_db(
                tmpdir,
                name="oob.db",
                extra_sql="ALTER TABLE invoices ADD COLUMN paid_date TEXT;",
            )
            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            self.assertIn("paid_date", inv_cols)
            self.assertEqual(self._user_version(db_path), 0)

            with patch.object(zd, "ZD_DB", db_path):
                conn = zd.sqlite3.connect(db_path)
                try:
                    # Must not raise "duplicate column name: paid_date".
                    zd._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

            inv_cols = {row[1] for row in self._table_info(db_path, "invoices")}
            sess_cols = {row[1] for row in self._table_info(db_path, "sessions")}
            self.assertIn("paid_date", inv_cols)
            self.assertIn("billing_mode", inv_cols)
            self.assertIn("billed_rate", sess_cols)
            # paid_date must appear exactly once (not duplicated).
            paid_date_count = sum(
                1 for row in self._table_info(db_path, "invoices")
                if row[1] == "paid_date"
            )
            self.assertEqual(paid_date_count, 1)
            self.assertEqual(self._user_version(db_path), zd._SCHEMA_VERSION)
