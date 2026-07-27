import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from newsbot.models import ArticleDraft
from newsbot.discord_bot import (
    ReviewCollectionRequest,
    build_review_collection_command,
    default_review_cadence,
    default_review_topic,
    format_review_collection_confirmation,
    format_schedule,
    next_weekly_publish_at,
    parse_review_message_for_modal,
    resolve_review_collection_period,
    summarize_review_collection_output,
)
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

    def test_review_collection_period_uses_lookback_days(self):
        request = ReviewCollectionRequest(lookback_days=21)
        now = datetime(2026, 7, 7, 1, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

        self.assertEqual(resolve_review_collection_period(request, now), "2026-06-16 to 2026-07-07 (JST)")

    def test_review_collection_period_override_wins(self):
        request = ReviewCollectionRequest(lookback_days=21, period="2026-06-01 to 2026-06-30 (JST)")

        self.assertEqual(resolve_review_collection_period(request), "2026-06-01 to 2026-06-30 (JST)")

    def test_build_review_collection_command_uses_period_or_lookback(self):
        request = ReviewCollectionRequest(lookback_days=14)
        command = build_review_collection_command(
            request,
            python_bin="/env/bin/python",
            codex_bin="/usr/local/bin/codex",
            db_path="/tmp/newsbot.sqlite",
            config_path="/tmp/config.json",
        )

        self.assertIn("--submit-review", command)
        self.assertIn("--lookback-days", command)
        self.assertIn("14", command)
        self.assertIn("--codex-bin", command)
        self.assertIn("/usr/local/bin/codex", command)
        self.assertNotIn("--period", command)

        period_command = build_review_collection_command(
            ReviewCollectionRequest(period="2026-07-01 to 2026-07-07 (JST)"),
            python_bin="/env/bin/python",
            codex_bin="",
        )
        self.assertIn("--period", period_command)
        self.assertNotIn("--lookback-days", period_command)

    def test_review_collection_confirmation_and_summary(self):
        request = ReviewCollectionRequest(lookback_days=7)
        confirmation = format_review_collection_confirmation(request)

        self.assertIn("Manual review candidate collection", confirmation)
        self.assertIn("Topic: `lab_automation`", confirmation)
        self.assertEqual(
            summarize_review_collection_output(
                [
                    "[newsbot-generate] starting Codex payload generation",
                    "[newsbot-generate] running phase_1",
                ]
            ),
            "running phase_1",
        )

    def test_review_defaults_are_configurable(self):
        with patch.dict(
            os.environ,
            {
                "NEWSBOT_REVIEW_TOPIC": "stock_news",
                "NEWSBOT_REVIEW_CADENCE": "daily",
            },
            clear=False,
        ):
            request = ReviewCollectionRequest.default()
            self.assertEqual(default_review_topic(), "stock_news")
            self.assertEqual(default_review_cadence(), "daily")

        self.assertEqual(request.topic, "stock_news")
        self.assertEqual(request.cadence, "daily")

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
