import tempfile
import unittest
from pathlib import Path

from newsbot.db import NewsbotStore
from newsbot.feedback import feedback_components, record_interested_feedback
from newsbot.models import NewsItem


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "newsbot.sqlite"
        self.store = NewsbotStore(self.db_path)
        self.store.init()
        draft, _ = self.store.create_or_update_draft_from_item(
            "lab_automation",
            "weekly",
            NewsItem(
                title="Robotics workflow improves throughput",
                url="https://example.com/robotics",
                summary="A workflow improvement.",
                source="Example",
                raw={"title": "Robotics workflow improves throughput"},
            ),
        )
        self.article_id = draft.id

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_feedback_is_inserted_once_per_user(self):
        first = record_interested_feedback(
            self.store,
            article_id=self.article_id,
            user_id="user-1",
            channel_id="channel-1",
            message_id="message-1",
        )
        second = record_interested_feedback(
            self.store,
            article_id=self.article_id,
            user_id="user-1",
            channel_id="channel-1",
            message_id="message-1",
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.store.feedback_count(self.article_id), 1)

    def test_feedback_component_shows_count_in_english(self):
        components = feedback_components(self.article_id, interested_count=3)

        button = components[0]["components"][0]
        self.assertEqual(button["label"], "Interested (3)")
        self.assertEqual(button["emoji"], {"name": "\U0001f44d"})


if __name__ == "__main__":
    unittest.main()
