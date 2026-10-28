from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from newsbot.db import DEFAULT_DB_PATH


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups" / "sqlite"


def backup_database(
    source: str | Path,
    backup_dir: str | Path,
    *,
    keep: int = 14,
    now: datetime | None = None,
) -> tuple[Path, list[Path]]:
    if keep < 1:
        raise ValueError("keep must be at least 1")

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    destination_dir = Path(backup_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_dir.chmod(0o700)

    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    backup_name = f"newsbot-{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}.sqlite.gz"
    final_path = destination_dir / backup_name

    sqlite_fd, sqlite_temp_name = tempfile.mkstemp(prefix=".newsbot-backup-", suffix=".sqlite", dir=destination_dir)
    os.close(sqlite_fd)
    sqlite_temp = Path(sqlite_temp_name)
    gzip_temp = destination_dir / f".{backup_name}.tmp-{os.getpid()}"

    try:
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_conn:
            with sqlite3.connect(sqlite_temp, timeout=30) as backup_conn:
                source_conn.backup(backup_conn)
                result = backup_conn.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    detail = result[0] if result else "no result"
                    raise RuntimeError(f"Backup integrity check failed: {detail}")

        with sqlite_temp.open("rb") as source_handle:
            with gzip.open(gzip_temp, "wb", compresslevel=6) as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle)
        gzip_temp.chmod(0o600)
        os.replace(gzip_temp, final_path)

        backups = sorted(destination_dir.glob("newsbot-*.sqlite.gz"), reverse=True)
        removed: list[Path] = []
        for expired in backups[keep:]:
            expired.unlink()
            removed.append(expired)
        return final_path, removed
    finally:
        sqlite_temp.unlink(missing_ok=True)
        gzip_temp.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a verified, compressed online backup of the Newsbot SQLite database.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Source SQLite database path.")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="Destination directory.")
    parser.add_argument("--keep", type=int, default=14, help="Number of newest backups to retain (default: 14).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backup_path, removed = backup_database(args.db, args.backup_dir, keep=args.keep)
    print(f"SQLite backup created: {backup_path}")
    print(f"Expired backups removed: {len(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
