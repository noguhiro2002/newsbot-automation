import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from newsbot.db import APPROVAL_REASONS, editorial_reasons_for_action
from newsbot.discord_bot import EditorialDecisionModal


class EditorialDecisionModalTests(unittest.TestCase):
    def test_approval_uses_checkboxes_and_only_approval_reasons(self):
        for action in ("weekly", "ready_go", "breaking"):
            with self.subTest(action=action):
                modal = EditorialDecisionModal(None, "article-1", action)
                group = modal.reason_groups[0]
                self.assertIsInstance(group, discord.ui.CheckboxGroup)
                self.assertEqual(group.min_values, 1)
                self.assertEqual(group.max_values, len(APPROVAL_REASONS))
                self.assertEqual([option.value for option in group.options], list(APPROVAL_REASONS))
                self.assertEqual(group.options[0].value, "high_impact")
                self.assertIs(modal.children[0].component, group)
                self.assertEqual(modal.to_dict()["components"][0]["component"]["type"], 22)

    def test_negative_actions_fit_discord_checkbox_limits(self):
        for action in ("hold", "reject", "cancel_weekly", "cancel_publish"):
            modal = EditorialDecisionModal(None, "article-1", action)
            codes = [option.value for group in modal.reason_groups for option in group.options]
            self.assertEqual(codes, list(editorial_reasons_for_action(action)))
            self.assertNotIn("high_impact", codes)
            self.assertLessEqual(len(modal.children), 5)
            for group in modal.reason_groups:
                self.assertLessEqual(len(group.options), 10)


class EditorialSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_reasons_passed_in_display_order(self):
        bot = SimpleNamespace(_require_reviewer=AsyncMock(return_value=True), _handle_weekly=AsyncMock())
        modal = EditorialDecisionModal(bot, "article-1", "weekly")
        modal.reason_groups[0]._values = ["technical_novelty", "high_impact"]
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        bot._handle_weekly.assert_awaited_once_with(
            interaction, "article-1", reason_codes=("high_impact", "technical_novelty"), reason_note="",
        )

    async def test_empty_other_and_inapplicable_reason_do_not_change_draft(self):
        for values in ([], ["other"], ["duplicate"]):
            bot = SimpleNamespace(_require_reviewer=AsyncMock(return_value=True), _handle_weekly=AsyncMock())
            modal = EditorialDecisionModal(bot, "article-1", "weekly")
            modal.reason_groups[0]._values = values
            interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
            await modal.on_submit(interaction)
            bot._handle_weekly.assert_not_awaited()
            interaction.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
