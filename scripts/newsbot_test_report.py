from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsbot.db import DEFAULT_DB_PATH


TABLES = [
    "article_drafts",
    "review_actions",
    "delivery_events",
    "feedback_events",
    "notification_runs",
    "notification_messages",
]


def row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def print_rows(title: str, rows: list[sqlite3.Row], fields: list[str]) -> None:
    print("")
    print(title)
    print("-" * len(title))
    if not rows:
        print("(none)")
        return
    for row in rows:
        values = [f"{field}={row[field]}" for field in fields]
        print(", ".join(values))


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a compact Newsbot test database report.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--limit", type=int, default=10, help="Rows to show per detail section")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        print(f"Database: {db_path}")
        print("")
        print("Table counts")
        print("------------")
        for table in TABLES:
            print(f"{table}: {row_count(conn, table)}")

        drafts = conn.execute(
            """
            SELECT id, status, publish_type, title_edited
            FROM article_drafts
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print_rows("Latest drafts", drafts, ["id", "status", "publish_type", "title_edited"])

        actions = conn.execute(
            """
            SELECT article_id, reviewer_user_id, action_type, created_at
            FROM review_actions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print_rows("Latest review actions", actions, ["article_id", "reviewer_user_id", "action_type", "created_at"])

        deliveries = conn.execute(
            """
            SELECT article_id, publish_type, status, discord_channel_id, discord_message_id
            FROM delivery_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print_rows(
            "Latest delivery events",
            deliveries,
            ["article_id", "publish_type", "status", "discord_channel_id", "discord_message_id"],
        )

        feedback = conn.execute(
            """
            SELECT article_id, user_id, action_type, created_at
            FROM feedback_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print_rows("Latest feedback events", feedback, ["article_id", "user_id", "action_type", "created_at"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
