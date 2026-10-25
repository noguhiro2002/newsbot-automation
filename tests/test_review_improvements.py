import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from newsbot.config import DiscordSettings, weekly_review_mode
from newsbot.db import NewsbotStore, validate_editorial_reasons
from newsbot.discord_bot import NewsbotDiscordBot
from newsbot.draft_rewrite import DraftRewriteOptions
from newsbot.models import NewsItem
from newsbot.rewrite_ui import RewriteChoiceView, RewriteDraftModal


def add_draft(store, name="example"):
    return store.create_or_update_draft_from_item(
        "lab_automation", "weekly",
        NewsItem(title=name, url=f"https://example.com/{name}", summary="Original body",
                 source="Example", category_primary="robotics", tags=("robotics",), raw={}),
    )[0]


def interaction(user_id=7):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild_id=1,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock(), content=""),
    )


class EditorialStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = NewsbotStore(Path(self.temp.name) / "newsbot.sqlite")
        self.store.init()
        self.draft = add_draft(self.store)

    def test_multiple_reasons_count_as_one_decision(self):
        self.store.record_editorial_judgment(
            article_id=self.draft.id, decision="accepted", reviewer_user_id="7",
            reason_codes=["high_impact", "technical_novelty", "high_impact"],
        )
        profile = self.store.editorial_feedback_profile(topic="lab_automation", cadence="weekly", lookback_weeks=12)
        self.assertEqual(profile["judgment_count"], 1)
        self.assertEqual(profile["decisions"], {"accepted": 1})
        self.assertEqual(profile["reason_codes"], {"high_impact": 1, "technical_novelty": 1})
        self.assertEqual(profile["reason_codes_by_decision"]["accepted"], profile["reason_codes"])
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM editorial_judgments").fetchone()
        self.assertEqual(row["reason_code"], "high_impact")
        self.assertEqual(json.loads(row["reason_codes_json"]), ["high_impact", "technical_novelty"])

    def test_upgrade_preserves_legacy_reason_and_is_idempotent(self):
        self.store.record_editorial_judgment(
            article_id=self.draft.id, decision="accepted", reviewer_user_id="7", reason_code="high_impact",
        )
        with self.store.connect() as conn:
            conn.execute("ALTER TABLE editorial_judgments DROP COLUMN reason_codes_json")
            conn.execute("DELETE FROM schema_migrations WHERE version = 4")
        self.store.init()
        self.store.init()
        profile = self.store.editorial_feedback_profile(topic="lab_automation", cadence="weekly", lookback_weeks=12)
        self.assertEqual(profile["reason_codes"], {"high_impact": 1})
        self.assertEqual(profile["judgment_count"], 1)

    def test_latest_decision_replaces_all_old_reasons_even_in_same_second(self):
        for decision, reasons in (("accepted", ["high_impact", "technical_novelty"]), ("excluded", ["duplicate"])):
            self.store.record_editorial_judgment(
                article_id=self.draft.id, decision=decision, reviewer_user_id="7", reason_codes=reasons,
            )
        profile = self.store.editorial_feedback_profile(topic="lab_automation", cadence="weekly", lookback_weeks=12)
        self.assertEqual(profile["reason_codes"], {"duplicate": 1})

    def test_other_requires_note_alongside_another_reason(self):
        with self.assertRaises(ValueError):
            validate_editorial_reasons(["high_impact", "other"], action="weekly")
        self.assertEqual(validate_editorial_reasons(["high_impact", "other"], "Why", action="weekly"), ("high_impact", "other"))

    def test_mode_switch_includes_only_previously_approved_items_and_keeps_batches(self):
        statuses = ["approved_weekly", "ready_weekly", "final_ready", "pending", "held", "rejected", "published"]
        for status in statuses:
            draft = add_draft(self.store, status)
            self.store.set_draft_status(draft.id, status, "7", scheduled_at=status)
        result = self.store.prepare_single_stage_weekly("lab_automation", "weekly")
        self.assertEqual({d.title_edited for d in result}, set(statuses[:3]))
        self.assertEqual({d.scheduled_at for d in result}, set(statuses[:3]))
        self.assertEqual(len(self.store.prepare_single_stage_weekly("lab_automation", "weekly")), 3)
        self.assertEqual({d.id for d in self.store.list_weekly_queue("lab_automation", "weekly")}, {d.id for d in result})

    def test_stale_rewrite_cannot_overwrite_an_edit_or_publication(self):
        self.store.update_draft_text(self.draft.id, "New title", "New body", "8")
        with self.assertRaises(ValueError):
            self.store.update_draft_text(self.draft.id, "AI title", "AI body", "7", expected_draft=self.draft)
        latest = self.store.get_draft(self.draft.id)
        self.assertEqual(latest.title_edited, "New title")
        self.store.set_draft_status(self.draft.id, "published", "8")
        with self.assertRaises(ValueError):
            self.store.update_draft_text(self.draft.id, "AI title", "AI body", "7", expected_draft=latest)


class ReviewModeConfigTests(unittest.TestCase):
    def test_default_explicit_and_invalid_modes(self):
        for value, expected in (("", "two_stage"), ("two_stage", "two_stage"), ("single_stage", "single_stage")):
            with patch.dict(os.environ, {"NEWSBOT_WEEKLY_REVIEW_MODE": value}):
                self.assertEqual(weekly_review_mode(), expected)
        with patch.dict(os.environ, {"NEWSBOT_WEEKLY_REVIEW_MODE": "typo"}), self.assertRaises(ValueError):
            weekly_review_mode()


class ReviewBotWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = NewsbotStore(Path(self.temp.name) / "newsbot.sqlite")
        self.store.init()
        self.draft = add_draft(self.store)
        with patch.dict(os.environ, {"NEWSBOT_WEEKLY_REVIEW_MODE": "single_stage"}):
            self.bot = NewsbotDiscordBot(
                store=self.store,
                settings=DiscordSettings("fake", "10", "20", frozenset({"7"}), "1"),
            )
        self.bot.schedule_refresh_review_message = Mock()
        self.bot.get_channel = Mock(return_value=SimpleNamespace(id=10))
        self.bot.send_channel_message = AsyncMock(return_value=SimpleNamespace(id=123))

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    async def test_single_review_approval_then_preview_then_publish(self):
        event = interaction()
        await self.bot._handle_weekly(event, self.draft.id, reason_codes=("high_impact", "technical_novelty"))
        selected = self.store.get_draft(self.draft.id)
        self.assertEqual(selected.status, "final_ready")
        self.bot.send_channel_message.assert_not_awaited()
        self.assertIn("Preview Digest", [child.label for child in self.bot.review_view_for_draft(selected).children])
        await self.bot._handle_preview_digest(interaction(), self.draft.id)
        self.assertEqual(self.bot.send_channel_message.await_count, 1)
        self.assertIn("Final digest is ready", self.bot.send_channel_message.call_args.args[1])
        self.assertEqual(self.store.get_draft(self.draft.id).status, "final_ready")
        await self.bot._handle_publish_digest(interaction(), self.draft.id)
        self.assertEqual(self.store.get_draft(self.draft.id).status, "published")
        self.assertEqual(self.bot.send_channel_message.await_count, 3)
        await self.bot._handle_publish_digest(interaction(), self.draft.id)
        self.assertEqual(self.bot.send_channel_message.await_count, 3)

    async def test_two_stage_still_requires_final_item_approval(self):
        self.bot.weekly_review_mode = "two_stage"
        await self.bot._handle_weekly(interaction(), self.draft.id, reason_codes=("high_impact",))
        self.assertEqual(self.store.get_draft(self.draft.id).status, "approved_weekly")
        self.assertEqual(await self.bot.prepare_weekly_publish("lab_automation", "weekly"), (1, 0, 0))
        self.assertEqual(self.store.get_draft(self.draft.id).status, "ready_weekly")
        await self.bot._handle_ready_go(interaction(), self.draft.id, reason_codes=("technical_novelty",))
        self.assertEqual(self.store.get_draft(self.draft.id).status, "final_ready")

    async def test_cancellation_and_stale_preview_do_not_publish(self):
        await self.bot._handle_weekly(interaction(), self.draft.id, reason_codes=("high_impact",))
        await self.bot._handle_cancel_publish(interaction(), self.draft.id, reason_codes=("duplicate",))
        self.assertEqual(self.store.get_draft(self.draft.id).status, "pending")
        self.bot.schedule_refresh_review_message.assert_called_with(self.draft.id)
        self.assertEqual([d.id for d in self.store.list_recent_cancelled_weekly()], [self.draft.id])
        await self.bot._handle_publish_digest(interaction(), self.draft.id)
        self.bot.send_channel_message.assert_not_awaited()

    async def test_weekly_queue_shows_single_stage_selections(self):
        await self.bot._handle_weekly(interaction(), self.draft.id, reason_codes=("high_impact",))
        event = interaction()
        await self.bot._cmd_weekly_queue(event)
        self.assertIn(self.draft.title_edited, event.followup.send.call_args.args[0])
        self.assertNotIn("No drafts found", event.followup.send.call_args.args[0])

    async def test_unauthorized_user_cannot_approve_or_generate(self):
        event = interaction(user_id=8)
        await self.bot._handle_weekly(event, self.draft.id, reason_codes=("high_impact",))
        await self.bot._handle_rewrite(event, self.draft.id)
        self.assertEqual(self.store.get_draft(self.draft.id).status, "pending")
        event.response.send_modal.assert_not_awaited()

    def options(self):
        return DraftRewriteOptions("rewrite-model", tuple(f"Title {i}" for i in range(5)), tuple(f"Body {i}" for i in range(5)))

    async def test_independent_title_and_body_selection_preserves_metadata_and_approval(self):
        draft = self.store.set_draft_status(self.draft.id, "final_ready", "7", publish_type="weekly", scheduled_at="batch")
        view = RewriteChoiceView(self.bot, draft, self.options(), 7)
        self.assertTrue(view.apply.disabled)
        view.title_select._values = ["1"]
        await view.choose_title(interaction())
        self.assertTrue(view.apply.disabled)
        view.body_select._values = ["4"]
        await view.choose_body(interaction())
        self.assertFalse(view.apply.disabled)
        self.assertIn("Title 1", view.preview())
        self.assertIn("Body 4", view.preview())
        await view.apply.callback(interaction())
        updated = self.store.get_draft(draft.id)
        self.assertEqual((updated.title_edited, updated.body_edited), ("Title 1", "Body 4"))
        for field in ("source_url", "category_primary", "tags_json", "status", "scheduled_at", "source_payload_json"):
            self.assertEqual(getattr(updated, field), getattr(draft, field))
        with self.store.connect() as conn:
            after = json.loads(conn.execute("SELECT after_json FROM review_actions WHERE action_type = 'edit'").fetchone()[0])
        self.assertIn("title=2; body=5", after["reviewer_note"])

    async def test_cancel_expiry_and_wrong_user_leave_draft_untouched(self):
        view = RewriteChoiceView(self.bot, self.draft, self.options(), 7)
        self.assertFalse(await view.interaction_check(interaction(8)))
        await view.cancel.callback(interaction())
        self.assertFalse(await view.interaction_check(interaction()))
        self.assertEqual(self.store.get_draft(self.draft.id).to_dict(), self.draft.to_dict())
        view = RewriteChoiceView(self.bot, self.draft, self.options(), 7)
        await view.on_timeout()
        self.assertFalse(await view.interaction_check(interaction()))

    async def test_generation_shows_five_options_without_saving(self):
        modal = RewriteDraftModal(self.bot, self.draft.id)
        modal.model_input._value = "rewrite-model"
        event = interaction()
        with patch("newsbot.rewrite_ui.generate_draft_rewrites", return_value=self.options()) as generate:
            await modal.on_submit(event)
        self.assertEqual(generate.call_args.kwargs["model"], "rewrite-model")
        self.assertEqual(event.followup.send.await_count, 6)
        self.assertIsInstance(event.followup.send.call_args.kwargs["view"], RewriteChoiceView)
        self.assertEqual(self.store.get_draft(self.draft.id).to_dict(), self.draft.to_dict())
        self.assertFalse(self.bot._rewrites_in_progress)

    async def test_generation_failure_does_not_save_and_allows_retry(self):
        modal = RewriteDraftModal(self.bot, self.draft.id)
        with patch("newsbot.rewrite_ui.generate_draft_rewrites", side_effect=RuntimeError("timeout")):
            await modal.on_submit(interaction())
        self.assertFalse(self.bot._rewrites_in_progress)
        self.assertEqual(self.store.get_draft(self.draft.id).to_dict(), self.draft.to_dict())


if __name__ == "__main__":
    unittest.main()
