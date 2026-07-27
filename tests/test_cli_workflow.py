import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsbot import cli
from newsbot.db import NewsbotStore
from newsbot.models import NewsItem


class FakeDiscordBotClient:
    instances = []

    def __init__(self, bot_token):
        self.bot_token = bot_token
        self.created_messages = []
        FakeDiscordBotClient.instances.append(self)

    def create_message(self, channel_id, content, *, components=None, suppress_embeds=True):
        message_id = f"message-{len(self.created_messages) + 1}"
        self.created_messages.append(
            {
                "channel_id": channel_id,
                "content": content,
                "components": components or [],
                "suppress_embeds": suppress_embeds,
            }
        )
        return {"id": message_id}


class CliWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "newsbot.sqlite"
        self.payload_path = self.temp_path / "payload.json"
        self.payload_path.write_text(
            json.dumps(
                {
                    "topic": "lab_automation",
                    "cadence": "weekly",
                    "period": "2026-05-25 to 2026-05-31 (JST)",
                    "items": [
                        {
                            "title": "Automated microscopy workflow improves throughput",
                            "summary": "A sample article for end-to-end CLI workflow testing.",
                            "source": "Example Source",
                            "url": "https://example.com/automation-workflow",
                            "category_primary": "robotics",
                            "tags": ["robotics", "microscopy"],
                            "importance_score": 0.87,
                            "priority": "high",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        FakeDiscordBotClient.instances = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_cli(self, args):
        with patch("newsbot.cli.load_dotenv", lambda path=".env": None):
            return cli.main(args)

    def _rows(self, table):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            return [dict(row) for row in rows]

    def test_validate_payload_command(self):
        result = self._run_cli(["validate-payload", "--input", str(self.payload_path)])

        self.assertEqual(result, 0)

    def test_submit_review_dry_run_creates_draft_without_discord(self):
        result = self._run_cli(
            [
                "--db",
                str(self.db_path),
                "submit-review",
                "--input",
                str(self.payload_path),
                "--dry-run",
            ]
        )

        self.assertEqual(result, 0)
        drafts = self._rows("article_drafts")
        actions = self._rows("review_actions")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "pending")
        self.assertIsNone(drafts[0]["review_message_id"])
        self.assertEqual(actions[0]["action_type"], "submit_review")

    def test_submit_review_live_posts_review_message(self):
        env = {
            "DISCORD_BOT_TOKEN": "fake-token",
            "DISCORD_REVIEW_CHANNEL_ID": "review-channel",
            "DISCORD_PUBLISH_CHANNEL_ID": "publish-channel",
            "DISCORD_REVIEWER_USER_IDS": "reviewer-1",
        }

        with patch.dict(os.environ, env, clear=False), patch("newsbot.cli.DiscordBotClient", FakeDiscordBotClient):
            result = self._run_cli(["--db", str(self.db_path), "submit-review", "--input", str(self.payload_path)])

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeDiscordBotClient.instances), 1)
        self.assertEqual(FakeDiscordBotClient.instances[0].created_messages[0]["channel_id"], "review-channel")
        drafts = self._rows("article_drafts")
        self.assertEqual(drafts[0]["review_channel_id"], "review-channel")
        self.assertEqual(drafts[0]["review_message_id"], "message-1")

    def test_submit_review_ranks_by_feedback_and_caps_at_30(self):
        store = NewsbotStore(self.db_path)
        store.init()
        historical, _ = store.create_or_update_draft_from_item(
            "lab_automation",
            "weekly",
            NewsItem(
                title="Historical robotics hit",
                url="https://example.com/historical-robotics",
                source="Example Source",
                category_primary="robotics",
                tags=("workflow",),
                raw={"title": "Historical robotics hit"},
            ),
        )
        for index in range(6):
            store.record_feedback(historical.id, f"user-{index}")

        payload = {
            "topic": "lab_automation",
            "cadence": "weekly",
            "period": "2026-05-25 to 2026-05-31 (JST)",
            "items": [
                {
                    "title": "Audience preferred robotics update",
                    "summary": "Matches prior Interested feedback.",
                    "source": "Example Source",
                    "url": "https://example.com/preferred-robotics",
                    "category_primary": "robotics",
                    "tags": ["workflow"],
                    "importance_score": 0.72,
                    "priority": "normal",
                }
            ]
            + [
                {
                    "title": f"Generic update {index:02d}",
                    "summary": "Generic update.",
                    "source": "Other Source",
                    "url": f"https://example.com/generic-{index}",
                    "category_primary": "other",
                    "tags": ["other"],
                    "importance_score": 0.7,
                    "priority": "normal",
                }
                for index in range(34)
            ],
        }
        self.payload_path.write_text(json.dumps(payload), encoding="utf-8")
        env = {
            "DISCORD_BOT_TOKEN": "fake-token",
            "DISCORD_REVIEW_CHANNEL_ID": "review-channel",
            "DISCORD_PUBLISH_CHANNEL_ID": "publish-channel",
            "DISCORD_REVIEWER_USER_IDS": "reviewer-1",
        }

        with patch.dict(os.environ, env, clear=False), patch("newsbot.cli.DiscordBotClient", FakeDiscordBotClient):
            result = self._run_cli(["--db", str(self.db_path), "submit-review", "--input", str(self.payload_path)])

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeDiscordBotClient.instances[0].created_messages), 30)
        self.assertIn("Audience preferred robotics update", FakeDiscordBotClient.instances[0].created_messages[0]["content"])

    def test_publish_weekly_live_posts_and_records_delivery(self):
        store = NewsbotStore(self.db_path)
        store.init()
        draft, _ = store.create_or_update_draft_from_item(
            "lab_automation",
            "weekly",
            NewsItem(
                title="Automated microscopy workflow improves throughput",
                url="https://example.com/automation-workflow",
                summary="A sample article for weekly publishing.",
                source="Example Source",
                raw={"title": "Automated microscopy workflow improves throughput"},
            ),
        )
        store.set_draft_status(draft.id, "approved_weekly", "reviewer-1", publish_type="weekly")

        env = {
            "DISCORD_BOT_TOKEN": "fake-token",
            "DISCORD_REVIEW_CHANNEL_ID": "review-channel",
            "DISCORD_PUBLISH_CHANNEL_ID": "publish-channel",
            "DISCORD_REVIEWER_USER_IDS": "reviewer-1",
        }

        with patch.dict(os.environ, env, clear=False), patch("newsbot.cli.DiscordBotClient", FakeDiscordBotClient):
            result = self._run_cli(
                [
                    "--db",
                    str(self.db_path),
                    "publish-weekly",
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                ]
            )

        self.assertEqual(result, 0)
        messages = FakeDiscordBotClient.instances[0].created_messages
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["channel_id"], "publish-channel")
        self.assertIn("今週の Lab Automation の注目ニュース！", messages[0]["content"])
        self.assertIn("1. Automated microscopy workflow improves throughput", messages[0]["content"])
        self.assertEqual(messages[1]["channel_id"], "publish-channel")
        self.assertIn("1. **Automated microscopy workflow improves throughput**", messages[1]["content"])
        drafts = self._rows("article_drafts")
        deliveries = self._rows("delivery_events")
        self.assertEqual(drafts[0]["status"], "published")
        self.assertEqual(drafts[0]["published_channel_id"], "publish-channel")
        self.assertEqual(deliveries[0]["status"], "sent")
        self.assertEqual(deliveries[0]["publish_type"], "weekly")

    def test_prepare_weekly_publish_posts_final_review(self):
        store = NewsbotStore(self.db_path)
        store.init()
        draft, _ = store.create_or_update_draft_from_item(
            "lab_automation",
            "weekly",
            NewsItem(
                title="Final review candidate",
                url="https://example.com/final-review",
                summary="A sample article for final weekly review.",
                source="Example Source",
                raw={"title": "Final review candidate"},
            ),
        )
        store.set_draft_status(draft.id, "approved_weekly", "reviewer-1", publish_type="weekly")
        env = {
            "DISCORD_BOT_TOKEN": "fake-token",
            "DISCORD_REVIEW_CHANNEL_ID": "review-channel",
            "DISCORD_PUBLISH_CHANNEL_ID": "publish-channel",
            "DISCORD_REVIEWER_USER_IDS": "reviewer-1",
        }

        with patch.dict(os.environ, env, clear=False), patch("newsbot.cli.DiscordBotClient", FakeDiscordBotClient):
            result = self._run_cli(
                [
                    "--db",
                    str(self.db_path),
                    "prepare-weekly-publish",
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                ]
            )

        self.assertEqual(result, 0)
        message = FakeDiscordBotClient.instances[0].created_messages[0]
        self.assertEqual(message["channel_id"], "review-channel")
        self.assertTrue(message["suppress_embeds"])
        self.assertIn("Final Weekly Review", message["content"])
        self.assertIn("Reorder and Publish Digest", message["content"])
        self.assertEqual(self._rows("article_drafts")[0]["status"], "ready_weekly")

    def test_prepare_weekly_publish_posts_digest_preview_when_all_items_are_final_ready(self):
        store = NewsbotStore(self.db_path)
        store.init()
        draft, _ = store.create_or_update_draft_from_item(
            "lab_automation",
            "weekly",
            NewsItem(
                title="Final digest candidate",
                url="https://example.com/final-digest",
                summary="A sample article selected for final digest.",
                source="Example Source",
                raw={"title": "Final digest candidate"},
            ),
        )
        store.set_draft_status(draft.id, "final_ready", "reviewer-1", publish_type="weekly")
        env = {
            "DISCORD_BOT_TOKEN": "fake-token",
            "DISCORD_REVIEW_CHANNEL_ID": "review-channel",
            "DISCORD_PUBLISH_CHANNEL_ID": "publish-channel",
            "DISCORD_REVIEWER_USER_IDS": "reviewer-1",
        }

        with patch.dict(os.environ, env, clear=False), patch("newsbot.cli.DiscordBotClient", FakeDiscordBotClient):
            result = self._run_cli(
                [
                    "--db",
                    str(self.db_path),
                    "prepare-weekly-publish",
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                ]
            )

        self.assertEqual(result, 0)
        message = FakeDiscordBotClient.instances[0].created_messages[0]
        labels = [button["label"] for row in message["components"] for button in row["components"]]
        self.assertEqual(message["channel_id"], "review-channel")
        self.assertIn("Final digest is ready.", message["content"])
        self.assertEqual(labels, ["Reorder", "Publish Digest"])
        self.assertEqual(self._rows("article_drafts")[0]["status"], "final_ready")

    def test_legacy_notify_dry_run_records_notification_run(self):
        result = self._run_cli(
            [
                "--db",
                str(self.db_path),
                "notify",
                "--input",
                str(self.payload_path),
                "--dry-run",
            ]
        )

        self.assertEqual(result, 0)
        runs = self._rows("notification_runs")
        messages = self._rows("notification_messages")
        self.assertEqual(runs[0]["status"], "dry_run")
        self.assertEqual(messages[0]["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
