from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsbot.cli import build_parser, cmd_research_missed
from newsbot.db import NewsbotStore
from newsbot.missed_item import (
    DEFAULT_MISSED_ITEM_MODEL,
    DEFAULT_MISSED_ITEM_REASONING_EFFORT,
    MissedItemResearchResult,
    build_missed_item_prompt,
    missed_item_model,
    missed_item_reasoning_effort,
    missed_item_timeout_seconds,
    parse_missed_item_result,
    record_researched_missed_item,
    research_missed_item_with_codex,
    validate_public_url,
)


def relevant_result() -> MissedItemResearchResult:
    return MissedItemResearchResult(
        supplied_url="https://wire.example/item",
        canonical_url="https://official.example/news/item",
        title="Autonomous laboratory announcement",
        organization="Example Lab",
        domain="official.example",
        phase="phase_3",
        published_date="2026-08-17",
        content_kind="news",
        relevant=True,
        relevance_summary="Closed-loop experiment control is described.",
        evidence="The official page describes measurement-driven condition selection.",
        confidence=0.92,
    )


class MissedItemSettingsTests(unittest.TestCase):
    def test_defaults_and_env_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(missed_item_model(), DEFAULT_MISSED_ITEM_MODEL)
            self.assertEqual(missed_item_reasoning_effort(), DEFAULT_MISSED_ITEM_REASONING_EFFORT)
            self.assertEqual(missed_item_timeout_seconds(), 1800)
        with patch.dict(
            os.environ,
            {
                "NEWSBOT_MISSED_ITEM_MODEL": "gpt-test",
                "NEWSBOT_MISSED_ITEM_REASONING_EFFORT": "xhigh",
                "NEWSBOT_MISSED_ITEM_TIMEOUT_SECONDS": "42",
            },
            clear=True,
        ):
            self.assertEqual(missed_item_model(), "gpt-test")
            self.assertEqual(missed_item_reasoning_effort(), "xhigh")
            self.assertEqual(missed_item_timeout_seconds(), 42)

    def test_invalid_reasoning_and_timeout_fail_explicitly(self):
        with patch.dict(os.environ, {"NEWSBOT_MISSED_ITEM_REASONING_EFFORT": "extreme"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be one of"):
                missed_item_reasoning_effort()
        for value in ("0", "-1", "1.5", "slow"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"NEWSBOT_MISSED_ITEM_TIMEOUT_SECONDS": value},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "integer of 1 or greater"):
                    missed_item_timeout_seconds()


class MissedItemResearchTests(unittest.TestCase):
    def test_url_validation_rejects_local_or_non_http_urls(self):
        for value in (
            "file:///etc/passwd",
            "http://localhost/test",
            "http://127.0.0.1/test",
            "https://user:pass@example.com/test",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_url(value)
        self.assertEqual(validate_public_url("https://example.com/news"), "https://example.com/news")

    def test_prompt_requests_related_primary_evidence_and_json(self):
        prompt = build_missed_item_prompt("https://example.com/news")
        self.assertIn("https://example.com/news", prompt)
        self.assertIn("related official announcement or primary evidence", prompt)
        self.assertIn('"phase": "phase_1|phase_2|phase_3|phase_4|phase_5"', prompt)
        self.assertIn("Set relevant=false", prompt)

    def test_parse_result_normalizes_and_validates(self):
        result = parse_missed_item_result(
            {
                "canonical_url": "https://Example.com/news/?utm_source=test",
                "title": "An autonomous lab",
                "organization": "Example",
                "phase": "phase_3",
                "published_date": "2026-08-17",
                "content_kind": "news",
                "relevant": True,
                "relevance_summary": "Specific experimental workflow.",
                "evidence": "Official page.",
                "confidence": 0.8,
            },
            "https://wire.example/item",
        )
        self.assertEqual(result.domain, "example.com")
        self.assertTrue(result.relevant)
        with self.assertRaisesRegex(ValueError, "invalid phase"):
            parse_missed_item_result(
                {
                    "title": "Item",
                    "organization": "Org",
                    "phase": "phase_9",
                    "relevant": True,
                    "confidence": 1,
                },
                "https://example.com/item",
            )
        with self.assertRaisesRegex(ValueError, "published_date must be YYYY-MM-DD"):
            parse_missed_item_result(
                {
                    "title": "Item",
                    "organization": "Org",
                    "phase": "phase_3",
                    "published_date": "August 17",
                    "relevant": False,
                    "confidence": 0.5,
                },
                "https://example.com/item",
            )

    def test_codex_command_uses_configured_model_reasoning_search_and_safe_env(self):
        payload = {
            "canonical_url": "https://official.example/news/item",
            "title": "Autonomous laboratory announcement",
            "organization": "Example Lab",
            "phase": "phase_3",
            "published_date": "2026-08-17",
            "content_kind": "news",
            "relevant": True,
            "relevance_summary": "Closed-loop experiments.",
            "evidence": "Official announcement.",
            "confidence": 0.92,
        }

        def fake_run(command, **kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/newsbot",
            "DISCORD_BOT_TOKEN": "secret",
            "NCBI_API_KEY": "secret",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "newsbot.missed_item.subprocess.run", side_effect=fake_run
        ) as run:
            result = research_missed_item_with_codex(
                "https://wire.example/item",
                model="gpt-5.6-luna",
                reasoning_effort="high",
                timeout=9,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["codex", "--search"])
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(run.call_args.kwargs["timeout"], 9)
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin", "HOME": "/home/newsbot"})
        self.assertTrue(result.relevant)


class MissedItemPersistenceTests(unittest.TestCase):
    def test_relevant_result_is_saved_as_search_miss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NewsbotStore(Path(temp_dir) / "newsbot.sqlite")
            store.init()
            judgment_id = record_researched_missed_item(
                store,
                relevant_result(),
                reviewer_user_id="admin-1",
            )
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM editorial_judgments WHERE id = ?", (judgment_id,)
                ).fetchone()
            self.assertEqual(row["decision"], "missed")
            self.assertEqual(row["reason_code"], "search_miss")
            self.assertEqual(row["reviewer_user_id"], "admin-1")
            self.assertEqual(row["canonical_url"], "https://official.example/news/item")
            self.assertIn("Closed-loop experiment control", row["note"])

    def test_irrelevant_result_is_not_saved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NewsbotStore(Path(temp_dir) / "newsbot.sqlite")
            store.init()
            value = relevant_result()
            irrelevant = MissedItemResearchResult(**{**value.to_dict(), "relevant": False})
            with self.assertRaisesRegex(ValueError, "not verified as relevant"):
                record_researched_missed_item(store, irrelevant, reviewer_user_id="admin-1")
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM editorial_judgments").fetchone()[0]
            self.assertEqual(count, 0)

    def test_cli_dry_run_does_not_open_database(self):
        args = argparse.Namespace(
            url="https://wire.example/item",
            reviewer="dry-run",
            topic="lab_automation",
            cadence="weekly",
            db="/must/not/be/opened.sqlite",
            dry_run=True,
        )
        with patch("newsbot.cli.research_missed_item_with_codex", return_value=relevant_result()), patch(
            "newsbot.cli.NewsbotStore"
        ) as store:
            self.assertEqual(cmd_research_missed(args), 0)
        store.assert_not_called()

    def test_parser_exposes_research_missed_dry_run(self):
        args = build_parser().parse_args(
            ["research-missed", "--url", "https://example.com/item", "--reviewer", "admin", "--dry-run"]
        )
        self.assertEqual(args.func, cmd_research_missed)
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
