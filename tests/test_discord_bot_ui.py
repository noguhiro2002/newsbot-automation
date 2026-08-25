import unittest

import discord

from newsbot.db import EDITORIAL_REASON_CODES
from newsbot.discord_bot import EditorialDecisionModal


class EditorialDecisionModalTests(unittest.TestCase):
    def test_reason_code_is_a_required_single_select(self):
        modal = EditorialDecisionModal(None, "article-1", "hold")

        self.assertIsInstance(modal.reason_code, discord.ui.Select)
        self.assertEqual(modal.reason_code.min_values, 1)
        self.assertEqual(modal.reason_code.max_values, 1)
        self.assertEqual(
            [option.value for option in modal.reason_code.options],
            sorted(EDITORIAL_REASON_CODES),
        )
        self.assertIsInstance(modal.children[0], discord.ui.Label)
        self.assertIs(modal.children[0].component, modal.reason_code)


if __name__ == "__main__":
    unittest.main()
