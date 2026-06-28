import os
import unittest
from unittest.mock import patch

from newsbot.source_check import (
    build_source_check_prompt,
    check_source_with_codex,
    extract_json_object,
    parse_source_check_result,
)


class SourceCheckTests(unittest.TestCase):
    def test_extract_json_object_from_codex_output(self):
        output = 'Done.\n```json\n{"status": "replace", "replacement_url": "https://example.com/new"}\n```'

        parsed = extract_json_object(output)

        self.assertEqual(parsed["status"], "replace")
        self.assertEqual(parsed["replacement_url"], "https://example.com/new")

    def test_parse_source_check_result_normalizes_values(self):
        result = parse_source_check_result(
            {
                "status": "REPLACE",
                "current_url_reachable": False,
                "replacement_url": "https://example.com/new",
                "reason": "Old URL returned 404.",
            }
        )

        self.assertEqual(result.status, "replace")
        self.assertFalse(result.current_url_reachable)
        self.assertEqual(result.replacement_url, "https://example.com/new")
        self.assertEqual(result.reason, "Old URL returned 404.")

    def test_parse_source_check_result_ignores_replacement_when_not_replace(self):
        result = parse_source_check_result(
            {
                "status": "ok",
                "current_url_reachable": True,
                "replacement_url": "https://example.com/unused",
            }
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.replacement_url, "")

    def test_build_source_check_prompt_includes_context(self):
        prompt = build_source_check_prompt(
            title="Example title",
            current_url="https://example.com/old",
            source_name="Example",
        )

        self.assertIn("Example title", prompt)
        self.assertIn("https://example.com/old", prompt)
        self.assertIn('"status": "ok|replace|unknown"', prompt)

    def test_empty_codex_bin_env_falls_back_to_codex(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"status": "unknown"}', "stderr": ""})()
        env = dict(os.environ)
        env["NEWSBOT_CODEX_BIN"] = ""

        with patch.dict(os.environ, env, clear=True), patch("newsbot.source_check.subprocess.run") as run:
            run.return_value = completed
            result = check_source_with_codex(
                title="Example title",
                current_url="https://example.com/old",
                timeout=1,
            )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(run.call_args.args[0][0], "codex")

    def test_codex_model_env_is_passed_to_command(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"status": "ok"}', "stderr": ""})()
        env = dict(os.environ)
        env["NEWSBOT_CODEX_MODEL"] = "gpt-test"

        with patch.dict(os.environ, env, clear=True), patch("newsbot.source_check.subprocess.run") as run:
            run.return_value = completed
            check_source_with_codex(
                title="Example title",
                current_url="https://example.com/old",
                timeout=1,
            )

        command = run.call_args.args[0]
        self.assertIn("--model", command)
        self.assertIn("gpt-test", command)


if __name__ == "__main__":
    unittest.main()
