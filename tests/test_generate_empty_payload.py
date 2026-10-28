import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsbot.models import NewsPayload
from scripts.generate_payload_openai import main, select_review_items, validate_payload_dict


class EmptyGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.output = self.path / "payload.json"
        self.payload = {"topic": "lab_automation", "cadence": "weekly", "period": "test period", "items": []}
        self.args = [
            "--topic", "lab_automation", "--period", "test period",
            "--db", str(self.path / "db.sqlite"), "--output", str(self.output),
        ]
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("scripts.generate_payload_openai.load_env_file"),
            patch("scripts.generate_payload_openai.log_step"),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_zero_items_finish_without_discord_in_both_generation_modes(self):
        for mode in ([], ["--single-agent"]):
            with self.subTest(mode=mode), patch(
                "scripts.generate_payload_openai.run_lab_automation_multi_agent", return_value=self.payload,
            ), patch(
                "scripts.generate_payload_openai.run_codex", return_value=json.dumps(self.payload),
            ), patch("scripts.generate_payload_openai.run_newsbot_command") as command, patch(
                "scripts.generate_payload_openai.write_last_run",
            ) as state:
                result = main(self.args + mode + ["--submit-review"])
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(self.output.read_text()), self.payload)
                command.assert_not_called()
                state.assert_called_once()

    def test_empty_isolated_run_does_not_advance_production_state(self):
        with patch("scripts.generate_payload_openai.run_lab_automation_multi_agent", return_value=self.payload), patch(
            "scripts.generate_payload_openai.run_newsbot_command",
        ) as command, patch("scripts.generate_payload_openai.write_last_run") as state:
            self.assertEqual(main(self.args + ["--isolated-dry-run"]), 0)
            self.assertEqual(json.loads(self.output.read_text())["items"], [])
            self.assertFalse((self.path / "db.sqlite").exists())
            command.assert_not_called()
            state.assert_not_called()

    def test_scheduled_generation_defers_state_for_empty_and_nonempty_results(self):
        for items in ([], [{"title": "News", "summary": "Summary", "url": "https://example.com/news"}]):
            with self.subTest(empty=not items), patch(
                "scripts.generate_payload_openai.run_codex", return_value=json.dumps({**self.payload, "items": items}),
            ), patch("scripts.generate_payload_openai.run_newsbot_command", return_value=0) as command, patch(
                "scripts.generate_payload_openai.write_last_run",
            ) as state:
                self.assertEqual(main(self.args + ["--single-agent", "--validate-only", "--defer-run-state"]), 0)
                state.assert_not_called()
                self.assertFalse(any(c.args[0][0] == "submit-review" for c in command.call_args_list))

    def test_malformed_outputs_are_not_treated_as_successful_empty_searches(self):
        invalid_payloads = [
            {key: value for key, value in self.payload.items() if key != "items"},
            {**self.payload, "items": None},
            {**self.payload, "items": {}},
            {**self.payload, "items": ""},
            {**self.payload, "topic": ""},
            {**self.payload, "topic": "unknown-topic"},
            {**self.payload, "cadence": "unknown-cadence"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch(
                "scripts.generate_payload_openai.run_codex", return_value=json.dumps(payload),
            ), patch("scripts.generate_payload_openai.run_newsbot_command") as command, patch(
                "scripts.generate_payload_openai.write_last_run",
            ) as state:
                with self.assertRaises(ValueError):
                    main(self.args + ["--single-agent", "--submit-review"])
                command.assert_not_called()
                state.assert_not_called()

    def test_empty_search_can_be_ranked_but_publication_validation_stays_strict(self):
        ranked = select_review_items(self.payload, preference_profile={}, limit=30)
        self.assertEqual(ranked["items"], [])
        with self.assertRaisesRegex(ValueError, "at least one item"):
            NewsPayload.from_dict(ranked)
        with self.assertRaisesRegex(ValueError, "at least one item"):
            validate_payload_dict(ranked, config_path=Path("config/newsbot.config.json"))

    def test_nonempty_result_still_validates_and_submits(self):
        payload = {**self.payload, "items": [{"title": "News", "summary": "Summary", "url": "https://example.com/news"}]}
        with patch("scripts.generate_payload_openai.run_codex", return_value=json.dumps(payload)), patch(
            "scripts.generate_payload_openai.run_newsbot_command", return_value=0,
        ) as command, patch("scripts.generate_payload_openai.write_last_run") as state:
            self.assertEqual(main(self.args + ["--single-agent", "--submit-review"]), 0)
            self.assertEqual(command.call_args_list[0].args[0], ["validate-payload", "--input", str(self.output)])
            self.assertEqual(command.call_args_list[1].args[0], ["submit-review", "--input", str(self.output)])
            state.assert_called_once()


if __name__ == "__main__":
    unittest.main()
