import json
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from newsbot.draft_rewrite import build_rewrite_prompt, generate_draft_rewrites, parse_rewrite_options, rewrite_model


def example_output():
    return {"titles": [f"タイトル案{i}" for i in range(5)], "bodies": [f"本文案{i}" for i in range(5)]}


def draft():
    return SimpleNamespace(
        title_edited="現在のタイトル", title_original="元のタイトル",
        body_edited="現在の本文", body_original="元の本文", source_name="Source",
        source_url="https://example.com/item", source_payload={"evidence": "Verified facts"},
    )


class DraftRewriteTests(unittest.TestCase):
    def test_validates_count_types_lengths_and_distinct_options(self):
        parsed = parse_rewrite_options(example_output(), "rewrite-model")
        self.assertEqual(len(parsed.titles), 5)
        for key, bad in (("titles", []), ("titles", ["x"] * 5), ("titles", [None] * 5),
                         ("titles", ["x" * 201] * 5), ("bodies", ["x" * 1501] * 5), ("bodies", [" "] * 5)):
            with self.subTest(key=key, value=str(bad)[:40]):
                value = example_output()
                value[key] = bad
                with self.assertRaises(ValueError):
                    parse_rewrite_options(value, "rewrite-model")

    def test_prompt_uses_current_text_and_source_evidence(self):
        prompt = build_rewrite_prompt(draft(), "簡潔に")
        self.assertIn("現在のタイトル", prompt)
        self.assertIn("現在の本文", prompt)
        self.assertIn("Verified facts", prompt)
        self.assertIn("簡潔に", prompt)
        self.assertIn("どのタイトルとどの本文", prompt)

    def test_model_is_independent_and_explicit_model_overrides_rewrite_default(self):
        env = {"NEWSBOT_CODEX_MODEL": "collection-model", "NEWSBOT_LAB_AUTOMATION_MASTER_MODEL": "master-model"}
        with patch.dict(os.environ, env, clear=True), patch("newsbot.draft_rewrite.subprocess.run") as run:
            self.assertEqual(rewrite_model(), "")
            with self.assertRaises(ValueError):
                generate_draft_rewrites(draft())
            run.assert_not_called()
        env.update({"NEWSBOT_REWRITE_MODEL": "default-rewrite", "DISCORD_BOT_TOKEN": "secret-token", "PATH": "/usr/bin"})
        output_path = None

        def run(command, **kwargs):
            nonlocal output_path
            self.assertEqual(command[command.index("--model") + 1], "chosen-rewrite")
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertNotIn("DISCORD_BOT_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["timeout"], 300)
            self.assertIn("現在のタイトル", kwargs["input"])
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(example_output()), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, env, clear=True), patch("newsbot.draft_rewrite.subprocess.run", side_effect=run):
            result = generate_draft_rewrites(draft(), model="chosen-rewrite")
        self.assertEqual(result.model, "chosen-rewrite")
        self.assertFalse(output_path.exists())
        self.assertFalse(output_path.parent.exists())

    def test_timeout_failure_and_malformed_output_are_reported(self):
        cases = [
            (subprocess.TimeoutExpired("codex", 1), RuntimeError),
            (SimpleNamespace(returncode=1, stdout="", stderr="sensitive error"), RuntimeError),
            (SimpleNamespace(returncode=0, stdout='{"titles": []}', stderr=""), ValueError),
        ]
        for result, expected in cases:
            with patch.dict(os.environ, {}, clear=True), patch("newsbot.draft_rewrite.subprocess.run") as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                with self.assertRaises(expected) as error:
                    generate_draft_rewrites(draft(), model="rewrite-model")
                self.assertNotIn("sensitive error", str(error.exception))

    def test_invalid_timeout_and_effort_fail_before_starting_codex(self):
        for setting, value in (("NEWSBOT_REWRITE_TIMEOUT_SECONDS", "0"), ("NEWSBOT_REWRITE_TIMEOUT_SECONDS", "601"),
                               ("NEWSBOT_REWRITE_TIMEOUT_SECONDS", "no"), ("NEWSBOT_REWRITE_REASONING_EFFORT", "typo")):
            with patch.dict(os.environ, {setting: value}, clear=True), patch("newsbot.draft_rewrite.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    generate_draft_rewrites(draft(), model="rewrite-model")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
