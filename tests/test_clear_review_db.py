import sqlite3
import tempfile
import unittest
from pathlib import Path

from newsbot.db import NewsbotStore
from newsbot.models import NewsItem
from scripts.clear_review_db import main


class ClearReviewDbTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "newsbot.sqlite"
        self.store = NewsbotStore(self.db_path)
        self.store.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_draft(self, title: str, url: str):
        draft, _ = self.store.create_or_update_draft_from_item(
            "lab_automation",
            "weekly",
            NewsItem(
                title=title,
                url=url,
                summary=f"{title} summary",
                source="Example Source",
                raw={"title": title, "url": url},
            ),
        )
        return draft

    def _count(self, table: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _status_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM article_drafts GROUP BY status").fetchall()
            return {str(status): int(count) for status, count in rows}

    def test_dry_run_deletes_nothing(self):
        self._create_draft("Pending draft", "https://example.com/pending")

        result = main(["--db", str(self.db_path)])

        self.assertEqual(result, 0)
        self.assertEqual(self._count("article_drafts"), 1)

    def test_yes_deletes_unpublished_review_records_only(self):
        pending = self._create_draft("Pending draft", "https://example.com/pending")
        approved = self._create_draft("Approved draft", "https://example.com/approved")
        approved = self.store.set_draft_status(approved.id, "approved_weekly", "reviewer-1", publish_type="weekly")
        published = self._create_draft("Published draft", "https://example.com/published")
        published = self.store.mark_published(published.id, "publish-channel", "message-1", "weekly")

        self.store.record_review_action(pending.id, "system", "submit_review")
        self.store.record_review_action(published.id, "system", "publish_weekly")
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback_events(
                    id, article_id, user_id, action_type, weight, discord_channel_id, discord_message_id, created_at
                )
                VALUES('feedback-pending', ?, 'user-1', 'interested', 1, 'review', 'review-message', '2026-06-18T00:00:00+00:00')
                """,
                (pending.id,),
            )
            conn.execute(
                """
                INSERT INTO feedback_events(
                    id, article_id, user_id, action_type, weight, discord_channel_id, discord_message_id, created_at
                )
                VALUES('feedback-published', ?, 'user-1', 'interested', 1, 'publish', 'publish-message', '2026-06-18T00:00:00+00:00')
                """,
                (published.id,),
            )
            conn.execute(
                """
                INSERT INTO delivery_events(
                    id, article_id, topic, cadence, publish_type, destination, status,
                    discord_channel_id, discord_message_id, message_text, error_message, created_at
                )
                VALUES(
                    'delivery-pending', ?, 'lab_automation', 'weekly', 'weekly', 'discord', 'dry_run',
                    'review', 'review-message', 'pending text', NULL, '2026-06-18T00:00:00+00:00'
                )
                """,
                (pending.id,),
            )
            conn.execute(
                """
                INSERT INTO delivery_events(
                    id, article_id, topic, cadence, publish_type, destination, status,
                    discord_channel_id, discord_message_id, message_text, error_message, created_at
                )
                VALUES(
                    'delivery-published', ?, 'lab_automation', 'weekly', 'weekly', 'discord', 'sent',
                    'publish', 'publish-message', 'published text', NULL, '2026-06-18T00:00:00+00:00'
                )
                """,
                (published.id,),
            )

        result = main(["--db", str(self.db_path), "--topic", "lab_automation", "--cadence", "weekly", "--yes"])

        self.assertEqual(result, 0)
        self.assertEqual(self._status_counts(), {"published": 1})
        self.assertEqual(self._count("feedback_events"), 1)
        self.assertEqual(self._count("delivery_events"), 1)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            remaining = conn.execute("SELECT * FROM article_drafts").fetchall()
            self.assertEqual(remaining[0]["id"], published.id)
            remaining_feedback = conn.execute("SELECT * FROM feedback_events").fetchall()
            self.assertEqual(remaining_feedback[0]["article_id"], published.id)
            remaining_delivery = conn.execute("SELECT * FROM delivery_events").fetchall()
            self.assertEqual(remaining_delivery[0]["article_id"], published.id)


if __name__ == "__main__":
    unittest.main()
