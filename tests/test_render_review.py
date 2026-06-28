import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from newsbot.models import ArticleDraft
from newsbot.discord_bot import format_schedule, next_weekly_publish_at, parse_review_message_for_modal
from newsbot.render import (
    render_published_message,
    render_review_message,
    render_status_line,
    render_weekly_detail_message,
    render_weekly_digest_overview,
)


def make_draft(**overrides):
    base = {
        "id": "draft-1234",
        "topic": "lab_automation",
        "cadence": "weekly",
        "source_name": "Example Source",
        "source_url": "https://example.com/article",
        "canonical_url": "https://example.com/article",
        "event_hash": "hash",
        "title_original": "Original title",
        "body_original": "Original body",
        "title_edited": "Edited title",
        "body_edited": "Edited body",
        "category_primary": "robotics",
        "tags_json": '["robotics","workflow"]',
        "importance_score": 0.9,
        "status": "pending",
        "publish_type": "",
        "priority": "high",
        "review_channel_id": "",
        "review_message_id": "",
        "published_channel_id": "",
        "published_message_id": "",
        "source_payload_json": "{}",
        "created_at": "2026-05-30T00:00:00+00:00",
        "updated_at": "2026-05-30T00:00:00+00:00",
        "scheduled_at": "",
        "published_at": "",
    }
    base.update(overrides)
    return ArticleDraft(**base)


class RenderReviewTests(unittest.TestCase):
    def test_render_status_line(self):
        draft = make_draft(status="approved_weekly", publish_type="weekly")
        self.assertEqual(render_status_line(draft), "Approved for weekly publish (weekly)")

    def test_render_review_message(self):
        draft = make_draft(scheduled_at="2026-06-01 08:00 JST")
        rendered = render_review_message(draft)

        self.assertIn("Review Draft #draft", rendered)
        self.assertIn("Category: robotics", rendered)
        self.assertIn("Priority: high", rendered)
        self.assertIn("Scheduled: 2026-06-01 08:00 JST", rendered)
        self.assertIn("Title:\nEdited title", rendered)
        self.assertIn("Body:\nEdited body", rendered)
        self.assertIn("Source: https://example.com/article", rendered)

    def test_parse_review_message_for_modal(self):
        draft = make_draft(body_edited="Edited body\nwith second line")
        rendered = render_review_message(draft)

        defaults = parse_review_message_for_modal(draft.id, rendered)

        self.assertEqual(defaults.id, draft.id)
        self.assertEqual(defaults.category, "robotics")
        self.assertEqual(defaults.title, "Edited title")
        self.assertEqual(defaults.body, "Edited body\nwith second line")
        self.assertEqual(defaults.source_url, "https://example.com/article")

    def test_next_weekly_publish_at(self):
        now = datetime(2026, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        scheduled = next_weekly_publish_at(now)

        self.assertEqual(format_schedule(scheduled), "2026-06-08 08:00 JST")

    def test_render_published_message_variants(self):
        draft = make_draft()
        weekly = render_published_message(draft, "weekly")
        breaking = render_published_message(draft, "breaking")
        overview = render_weekly_digest_overview([draft], datetime(2026, 6, 6, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")))

        self.assertIn("1. **Edited title**", weekly)
        self.assertIn("Breaking: Lab Automation News", breaking)
        self.assertIn("**Edited title**", weekly)
        self.assertIn("今週の Lab Automation の注目ニュース！", overview)
        self.assertIn("2026/05/31 ~ 2026/06/06", overview)
        self.assertIn("1. Edited title", overview)

    def test_render_weekly_detail_can_add_leading_gap(self):
        draft = make_draft()
        rendered = render_weekly_detail_message(draft, 1, leading_gap=True)

        self.assertTrue(rendered.startswith("\u200b\n\n1. **Edited title**"))


if __name__ == "__main__":
    unittest.main()
