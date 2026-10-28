from __future__ import annotations

import gzip
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.backup_sqlite import backup_database


class BackupSqliteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "newsbot.sqlite"
        self.backup_dir = self.root / "backups"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
            conn.execute("INSERT INTO items(title) VALUES('preserved')")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _restore(self, backup_path: Path, target: Path) -> None:
        with gzip.open(backup_path, "rb") as source, target.open("wb") as destination:
            destination.write(source.read())

    def test_backup_is_restorable_and_private(self):
        backup_path, removed = backup_database(
            self.db_path,
            self.backup_dir,
            now=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc),
        )

        restored = self.root / "restored.sqlite"
        self._restore(backup_path, restored)
        with sqlite3.connect(restored) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT title FROM items").fetchone()[0], "preserved")
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backup_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(removed, [])

    def test_retention_keeps_newest_backups(self):
        created = []
        removed = []
        for day in range(1, 5):
            backup_path, removed = backup_database(
                self.db_path,
                self.backup_dir,
                keep=2,
                now=datetime(2026, 9, day, 0, 0, tzinfo=timezone.utc),
            )
            created.append(backup_path)

        remaining = sorted(self.backup_dir.glob("newsbot-*.sqlite.gz"))
        self.assertEqual(remaining, created[-2:])
        self.assertEqual(removed, [created[-3]])

    def test_rejects_invalid_retention(self):
        with self.assertRaisesRegex(ValueError, "at least 1"):
            backup_database(self.db_path, self.backup_dir, keep=0)


if __name__ == "__main__":
    unittest.main()
