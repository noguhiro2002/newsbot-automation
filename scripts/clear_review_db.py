from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsbot.config import resolve_project_path
from newsbot.db import DEFAULT_DB_PATH, NewsbotStore


REVIEW_TABLES = ("review_actions", "feedback_events", "delivery_events")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clear unpublished Newsbot review drafts and their review-side records. "
            "Published drafts are never deleted."
        )
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to the Newsbot SQLite database.")
    parser.add_argument("--topic", default="", help="Optional topic filter, e.g. lab_automation.")
    parser.add_argument("--cadence", default="", help="Optional cadence filter, e.g. weekly.")
    parser.add_argument("--yes", action="store_true", help="Actually delete records. Without this, only a dry-run summary is printed.")
    return parser


def select_unpublished_draft_ids(conn: sqlite3.Connection, *, topic: str = "", cadence: str = "") -> list[str]:
    clauses = ["status != 'published'"]
    params: list[Any] = []
    if topic:
        clauses.append("topic = ?")
        params.append(topic)
    if cadence:
        clauses.append("cadence = ?")
        params.append(cadence)
    where = " AND ".join(clauses)
    rows = conn.execute(f"SELECT id FROM article_drafts WHERE {where} ORDER BY created_at", params).fetchall()
    return [str(row["id"]) for row in rows]


def count_by_status(conn: sqlite3.Connection, *, topic: str = "", cadence: str = "") -> list[tuple[str, int]]:
    clauses = ["status != 'published'"]
    params: list[Any] = []
    if topic:
        clauses.append("topic = ?")
        params.append(topic)
    if cadence:
        clauses.append("cadence = ?")
        params.append(cadence)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT status, COUNT(*) AS count
        FROM article_drafts
        WHERE {where}
        GROUP BY status
        ORDER BY status
        """,
        params,
    ).fetchall()
    return [(str(row["status"]), int(row["count"])) for row in rows]


def count_related_rows(conn: sqlite3.Connection, table: str, draft_ids: list[str]) -> int:
    if not draft_ids:
        return 0
    placeholders = ", ".join("?" for _ in draft_ids)
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE article_id IN ({placeholders})", draft_ids).fetchone()
    return int(row["count"] if row else 0)


def delete_related_rows(conn: sqlite3.Connection, table: str, draft_ids: list[str]) -> int:
    if not draft_ids:
        return 0
    placeholders = ", ".join("?" for _ in draft_ids)
    cursor = conn.execute(f"DELETE FROM {table} WHERE article_id IN ({placeholders})", draft_ids)
    return int(cursor.rowcount or 0)


def delete_unpublished_drafts(conn: sqlite3.Connection, draft_ids: list[str]) -> int:
    if not draft_ids:
        return 0
    placeholders = ", ".join("?" for _ in draft_ids)
    cursor = conn.execute(f"DELETE FROM article_drafts WHERE id IN ({placeholders}) AND status != 'published'", draft_ids)
    return int(cursor.rowcount or 0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = resolve_project_path(args.db)
    topic = args.topic.strip()
    cadence = args.cadence.strip()

    store = NewsbotStore(db_path)
    store.init()

    with store.connect() as conn:
        draft_ids = select_unpublished_draft_ids(conn, topic=topic, cadence=cadence)
        status_counts = count_by_status(conn, topic=topic, cadence=cadence)
        related_counts = {table: count_related_rows(conn, table, draft_ids) for table in REVIEW_TABLES}
        published_count = int(conn.execute("SELECT COUNT(*) AS count FROM article_drafts WHERE status = 'published'").fetchone()["count"])

        scope = []
        if topic:
            scope.append(f"topic={topic}")
        if cadence:
            scope.append(f"cadence={cadence}")
        scope_text = ", ".join(scope) if scope else "all topics/cadences"

        print(f"Database: {db_path}")
        print(f"Scope: {scope_text}")
        print(f"Published drafts preserved: {published_count}")
        print(f"Unpublished review drafts selected: {len(draft_ids)}")
        if status_counts:
            print("Selected draft statuses:")
            for status, count in status_counts:
                print(f"  {status}: {count}")
        else:
            print("Selected draft statuses: none")
        print("Related rows selected:")
        for table in REVIEW_TABLES:
            print(f"  {table}: {related_counts[table]}")

        if not args.yes:
            print("Dry run only. Re-run with --yes to delete these unpublished review records.")
            return 0

        deleted_related = {table: delete_related_rows(conn, table, draft_ids) for table in REVIEW_TABLES}
        deleted_drafts = delete_unpublished_drafts(conn, draft_ids)

        print("Deleted rows:")
        for table in REVIEW_TABLES:
            print(f"  {table}: {deleted_related[table]}")
        print(f"  article_drafts: {deleted_drafts}")
        print("Published drafts were not deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
