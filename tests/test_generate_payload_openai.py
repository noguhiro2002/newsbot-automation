import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts.generate_payload_openai import (
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    add_paper_api_candidate_ids,
    build_prompt,
    build_generation_prompt,
    build_master_prompt,
    build_parser,
    codex_web_search_queries,
    create_isolated_dry_run_db,
    default_output_path,
    default_period,
    extract_json_object,
    lab_automation_phase_definitions,
    main,
    merge_discovery_lane_outputs,
    merge_phase_4_batch_outputs,
    merge_phase_4_lane_outputs,
    normalize_discovery_lane_output,
    parse_phase_models,
    parse_phase_reasoning,
    read_last_run,
    resolve_period,
    run_codex,
    select_review_items,
    split_paper_api_result_for_llm,
    validate_phase_output,
    validate_payload_dict,
    write_phase_4_rejections,
    write_last_run,
)
from newsbot.db import NewsbotStore


class GeneratePayloadOpenAITests(unittest.TestCase):
    def test_extract_plain_json(self):
        value = extract_json_object('{"topic":"lab_automation","items":[]}')

        self.assertEqual(value["topic"], "lab_automation")

    def test_extract_fenced_json(self):
        value = extract_json_object(
            """
            Here is the payload:

            ```json
            {"topic":"lab_automation","cadence":"weekly","period":"x","items":[]}
            ```
            """
        )

        self.assertEqual(value["cadence"], "weekly")

    def test_extract_embedded_json(self):
        value = extract_json_object(
            'payload={"topic":"lab_automation","cadence":"weekly","period":"x","items":[{"title":"A","url":"https://example.com"}]} done'
        )

        self.assertEqual(value["items"][0]["title"], "A")

    def test_default_paths_and_period(self):
        now = datetime(2026, 6, 6, 10, 0)

        self.assertEqual(default_period("weekly", now), "2026-05-30 to 2026-06-06 (JST)")
        self.assertTrue(str(default_output_path("lab_automation", "weekly", now)).endswith("lab_automation_weekly_20260606_100000_000000.json"))

    def test_resolve_period_with_lookback_days(self):
        now = datetime(2026, 6, 6, 10, 0)

        period = resolve_period(topic="lab_automation", cadence="weekly", lookback_days=3, now=now)

        self.assertEqual(period, "2026-06-03 to 2026-06-06 (JST)")

    def test_run_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "last.txt"
            value = datetime(2026, 6, 6, 10, 0)

            write_last_run(path, value)

            self.assertEqual(read_last_run(path), value)

    def test_isolated_dry_run_snapshots_database_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "production.sqlite"
            output_path = temp_path / "payload.json"
            source_store = NewsbotStore(source_path)
            source_store.init()
            source_store.record_editorial_judgment(
                decision="missed",
                reason_code="search_miss",
                reviewer_user_id="admin",
                topic="lab_automation",
                cadence="weekly",
                canonical_url="https://example.com/missed",
                title="Missed item",
                phase="phase_3",
            )

            snapshot_path = create_isolated_dry_run_db(source_path, output_path)
            snapshot_store = NewsbotStore(snapshot_path)
            snapshot_store.record_editorial_judgment(
                decision="missed",
                reason_code="search_miss",
                reviewer_user_id="dry-run",
                topic="lab_automation",
                cadence="weekly",
                canonical_url="https://example.com/dry-only",
                title="Dry-run only item",
                phase="phase_3",
            )

            with source_store.connect() as conn:
                source_count = conn.execute("SELECT COUNT(*) FROM editorial_judgments").fetchone()[0]
            with snapshot_store.connect() as conn:
                snapshot_count = conn.execute("SELECT COUNT(*) FROM editorial_judgments").fetchone()[0]
            self.assertEqual(source_count, 1)
            self.assertEqual(snapshot_count, 2)
            self.assertTrue(str(snapshot_path).endswith(".isolated-dry-run.sqlite"))

    def test_isolated_dry_run_parser_and_submission_conflict(self):
        args = build_parser().parse_args(["--isolated-dry-run"])
        self.assertTrue(args.isolated_dry_run)
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            main(["--isolated-dry-run", "--submit-review"])

    def test_validate_payload_dict(self):
        payload = {
            "topic": "lab_automation",
            "cadence": "weekly",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "items": [
                {
                    "title": "Automation update",
                    "summary": "A useful update.",
                    "source": "Example",
                    "url": "https://example.com/news",
                    "category_primary": "robotics",
                    "tags": ["robotics"],
                    "importance_score": 0.8,
                    "priority": "normal",
                }
            ],
        }

        parsed = validate_payload_dict(payload, config_path=Path("config/newsbot.config.json"))

        self.assertEqual(parsed.topic, "lab_automation")
        self.assertEqual(parsed.items[0].canonical_url, "https://example.com/news")

    def test_build_prompt_mentions_output_and_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "payload.json"
            prompt = build_prompt(
                topic="lab_automation",
                cadence="weekly",
                period="2026-05-30 to 2026-06-06 (JST)",
                max_items=10,
                output_path=output,
                config_path=Path("config/newsbot.config.json"),
                preference_profile={"total_feedback": 2, "categories": {"robotics": 2}},
            )

        self.assertIn('"topic": "lab_automation"', prompt)
        self.assertIn('"items"', prompt)
        self.assertIn(output.as_posix(), prompt)
        self.assertIn("preferred categories: robotics (2)", prompt)

    def test_build_generation_prompt_uses_topic_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)
            output = template_dir / "payload.json"
            (template_dir / "lab_automation.md").write_text(
                "Template {{TOPIC}} {{CADENCE}} {{PERIOD}} {{MAX_ITEMS}} {{OUTPUT_PATH}} {{CONFIG_PATH}}\n"
                "{{PREFERENCE_PROFILE}}",
                encoding="utf-8",
            )

            prompt, template_path = build_generation_prompt(
                topic="lab_automation",
                cadence="weekly",
                period="2026-05-30 to 2026-06-06 (JST)",
                max_items=10,
                output_path=output,
                config_path=Path("config/newsbot.config.json"),
                preference_profile={"total_feedback": 2, "categories": {"robotics": 2}},
                template_dir=template_dir,
            )

        self.assertEqual(template_path, Path(temp_dir) / "lab_automation.md")
        self.assertIn("Template lab_automation weekly 2026-05-30 to 2026-06-06 (JST) 10", prompt)
        self.assertIn(output.as_posix(), prompt)
        self.assertIn("config/newsbot.config.json", prompt)
        self.assertIn("preferred categories: robotics (2)", prompt)

    def test_build_generation_prompt_falls_back_without_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "payload.json"
            prompt, template_path = build_generation_prompt(
                topic="missing_topic",
                cadence="weekly",
                period="2026-05-30 to 2026-06-06 (JST)",
                max_items=10,
                output_path=output,
                config_path=Path("config/newsbot.config.json"),
                preference_profile=None,
                template_dir=Path(temp_dir),
            )

        self.assertIsNone(template_path)
        self.assertIn("You are generating a Newsbot payload JSON", prompt)
        self.assertIn('"topic": "missing_topic"', prompt)

    @patch("scripts.generate_payload_openai.subprocess.run")
    def test_run_codex_uses_stdin_and_filtered_environment(self, run):
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
                "stderr": "",
            },
        )()
        run.return_value = completed
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/newsbot",
            "DISCORD_BOT_TOKEN": "discord-secret",
            "NCBI_API_KEY": "ncbi-secret",
            "X_API_KEY": "x-secret",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, env, clear=True):
            log_path = Path(temp_dir) / "phase.codex.log"
            result = run_codex(
                codex_bin="codex",
                codex_model="",
                reasoning_effort="",
                codex_args=[],
                prompt="private prompt",
                timeout=1,
                log_path=log_path,
            )

        self.assertTrue(result)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "-")
        self.assertNotIn("private prompt", command)
        self.assertEqual(run.call_args.kwargs["input"], "private prompt")
        self.assertEqual(
            run.call_args.kwargs["env"],
            {"PATH": "/usr/bin", "HOME": "/home/newsbot"},
        )

    def test_main_skip_codex_writes_multi_agent_prompts_for_lab_automation(self):
        paper_api_result = {
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "queries": ["self-driving laboratory"],
            "candidates": [
                {
                    "title": "API candidate paper",
                    "abstract": "A lab automation paper.",
                    "authors": ["Ada Example"],
                    "source": "arXiv",
                    "url": "https://arxiv.org/abs/2606.12345",
                    "doi": "",
                    "published_date": "2026-06-01",
                    "source_type": "preprint",
                    "api_source": "arxiv",
                    "matched_query": "self-driving laboratory",
                    "raw_categories": ["cs.RO"],
                }
            ],
            "coverage": {"api_queries_run": [], "api_failures": [], "limitations": ""},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.collect_paper_api_candidates", return_value=paper_api_result
        ):
            temp_path = Path(temp_dir)
            prompt_output = temp_path / "rendered.prompt.txt"
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"

            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--prompt-output",
                    str(prompt_output),
                    "--db",
                    str(db_path),
                    "--skip-codex",
                ]
            )

            prompt = prompt_output.read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertIn("Lab Automation Master", prompt)
            self.assertIn("2026-05-30 to 2026-06-06 (JST)", prompt)
            self.assertIn(output.as_posix(), prompt)
            self.assertIn("No historical Interested feedback", prompt)
            for phase in lab_automation_phase_definitions():
                suffix = (
                    ".api.prompt.txt"
                    if phase.key == "phase_4"
                    else ".structured.prompt.txt"
                    if phase.broad_prompt_file
                    else ".prompt.txt"
                )
                self.assertTrue((output.with_suffix(f".{phase.key}{suffix}")).exists())
            self.assertTrue(output.with_suffix(".phase_4.paper_api.json").exists())
            phase_4_prompt = output.with_suffix(".phase_4.api.prompt.txt").read_text(encoding="utf-8")
            self.assertIn("API candidate paper", phase_4_prompt)
            self.assertIn("paper-api-", phase_4_prompt)
            self.assertIn("ChemWorld", phase_4_prompt)
            self.assertIn("一般的なLLMベンチマーク", phase_4_prompt)
            phase_4_broad_prompt = output.with_suffix(
                ".phase_4.broad.prompt.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("Independence Rule", phase_4_broad_prompt)
            self.assertNotIn("API candidate paper", phase_4_broad_prompt)
            for phase_key in ("phase_1", "phase_2", "phase_3"):
                phase_prompt = output.with_suffix(f".{phase_key}.structured.prompt.txt").read_text(
                    encoding="utf-8"
                )
                self.assertIn("structuredレーン専用", phase_prompt)
                self.assertIn('"broad_queries_run"', phase_prompt)
                self.assertIn('"discovery_mode"', phase_prompt)
                broad_prompt = output.with_suffix(f".{phase_key}.broad.prompt.txt").read_text(encoding="utf-8")
                self.assertIn("独立", broad_prompt)
                self.assertNotIn("{{FEED_CANDIDATES_JSON}}", broad_prompt)
            phase_5_prompt = output.with_suffix(".phase_5.prompt.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("Independence Rule", phase_5_prompt)
            self.assertIn("Phase 1〜4の候補や検索結果は参照できません", phase_5_prompt)
            self.assertIn("fixed upper limitなし", phase_5_prompt)
            self.assertNotIn("maximum candidates: `30`", phase_5_prompt)
            self.assertIn("Phase 5由来であること自体を減点せず", prompt)

    def test_main_skip_codex_disable_paper_api_does_not_collect(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch("scripts.generate_payload_openai.collect_paper_api_candidates") as collect:
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"

            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--db",
                    str(db_path),
                    "--skip-codex",
                    "--disable-paper-api",
                ]
            )

            self.assertEqual(exit_code, 0)
            collect.assert_not_called()
            self.assertFalse(output.with_suffix(".phase_4.paper_api.json").exists())

    def test_main_skip_codex_writes_phase_4_prompt_batches(self):
        paper_api_result = {
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "queries": [],
            "candidates": [
                {
                    "title": f"API candidate {index}",
                    "abstract": "Robotic chemistry experiment.",
                    "authors": [],
                    "source": "arXiv",
                    "url": f"https://arxiv.org/abs/{index}",
                    "doi": "",
                    "published_date": "2026-06-01",
                    "source_type": "preprint",
                    "api_source": "arxiv",
                    "matched_query": "date-category-semantic-filter",
                    "raw_categories": ["cs.RO"],
                }
                for index in range(3)
            ],
            "coverage": {"api_queries_run": [], "api_failures": []},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.collect_paper_api_candidates",
            return_value=paper_api_result,
        ):
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--db",
                    str(temp_path / "newsbot.sqlite3"),
                    "--skip-codex",
                    "--phase-4-llm-batch-size",
                    "2",
                ]
            )

            self.assertEqual(exit_code, 0)
            first = output.with_suffix(".phase_4.api.batch_001.prompt.txt")
            second = output.with_suffix(".phase_4.api.batch_002.prompt.txt")
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertIn('"batch_count": 2', first.read_text(encoding="utf-8"))
            self.assertIn("API candidate 2", second.read_text(encoding="utf-8"))

    def test_only_phase_4_runs_phase_and_skips_master(self):
        phase_payload = {
            "phase": "phase_4_papers",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "candidates": [],
            "search_coverage": {
                "queries_run": ["broad q1", "broad q2", "broad q3"],
                "broad_queries_run": ["broad q1", "broad q2", "broad q3"],
                "broad_candidate_count": 0,
                "merged_candidate_count": 0,
                "notable_zero_result_queries": [],
                "limitations": "",
            },
        }

        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            return json.dumps(phase_payload)

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.run_codex", side_effect=fake_run_codex
        ) as run:
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"
            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--db",
                    str(db_path),
                    "--only-phase",
                    "phase_4",
                    "--disable-paper-api",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(output.with_suffix(".phase_4.broad.prompt.txt").exists())
            self.assertTrue(output.with_suffix(".phase_4.json").exists())
            self.assertFalse(output.with_suffix(".phase_1.structured.prompt.txt").exists())
            self.assertFalse(output.with_suffix(".phase_1.broad.prompt.txt").exists())
            self.assertFalse(output.with_suffix(".master.prompt.txt").exists())
            self.assertFalse(output.exists())

    def test_phase_4_api_and_broad_lanes_run_in_parallel_and_merge_provenance(self):
        paper_api_result = {
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "queries": [],
            "candidates": [
                {
                    "title": "Autonomous chemistry laboratory",
                    "abstract": "A robotic chemist executes closed-loop experiments.",
                    "authors": [],
                    "source": "ChemRxiv",
                    "url": "https://doi.org/10.26434/chemrxiv.1/v1",
                    "doi": "10.26434/chemrxiv.1/v1",
                    "published_date": "2026-06-01",
                    "source_type": "preprint",
                    "api_source": "chemrxiv_crossref",
                    "matched_query": "chemrxiv-posted-content",
                    "raw_categories": [],
                }
            ],
            "coverage": {"api_queries_run": [], "api_failures": []},
            "prefilter_excluded_candidates": [],
        }
        barrier = threading.Barrier(2, timeout=2)
        lane_threads: dict[str, int] = {}

        def fake_run_codex(**kwargs):
            path = kwargs["log_path"]
            path.write_text("fake log", encoding="utf-8")
            lane = "api" if ".api." in path.name else "broad"
            lane_threads[lane] = threading.get_ident()
            barrier.wait()
            common_candidate = {
                "title": "Autonomous chemistry laboratory",
                "source": "ChemRxiv",
                "url": "https://doi.org/10.26434/chemrxiv.1/v1",
                "published_date": "2026-06-01",
                "source_type": "preprint",
                "geography": "Global",
                "lab_automation_relevance": "Closed-loop robotic chemistry.",
                "evidence": "Verified DOI and abstract.",
                "confidence": 0.9,
                "canonical_source_checked": True,
                "duplicate_key": "autonomous-chemistry-laboratory",
            }
            if lane == "api":
                candidate_id = re.search(
                    r'"candidate_id":\s*"(paper-api-[a-f0-9]+)"',
                    kwargs["prompt"],
                ).group(1)
                common_candidate["paper_api_candidate_ids"] = [candidate_id]
                value = {
                    "phase": "phase_4_papers",
                    "period": "2026-05-30 to 2026-06-06 (JST)",
                    "candidates": [common_candidate],
                    "excluded_api_candidates": [],
                    "search_coverage": {
                        "queries_run": ["DOI verification"],
                        "notable_zero_result_queries": [],
                        "limitations": "",
                    },
                }
            else:
                common_candidate.update(
                    {
                        "paper_api_candidate_ids": [],
                        "discovery_mode": "broad",
                        "discovery_modes": ["broad"],
                    }
                )
                value = {
                    "phase": "phase_4_papers",
                    "period": "2026-05-30 to 2026-06-06 (JST)",
                    "candidates": [common_candidate],
                    "search_coverage": {
                        "queries_run": ["q1", "q2", "q3"],
                        "broad_queries_run": ["q1", "q2", "q3"],
                        "broad_candidate_count": 1,
                        "merged_candidate_count": 1,
                        "notable_zero_result_queries": [],
                        "limitations": "",
                    },
                }
            return json.dumps(value)

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.collect_paper_api_candidates",
            return_value=paper_api_result,
        ), patch(
            "scripts.generate_payload_openai.run_codex", side_effect=fake_run_codex
        ):
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--db",
                    str(temp_path / "newsbot.sqlite3"),
                    "--only-phase",
                    "phase_4",
                ]
            )

            merged = json.loads(
                output.with_suffix(".phase_4.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertNotEqual(lane_threads["api"], lane_threads["broad"])
        self.assertEqual(len(merged["candidates"]), 1)
        self.assertEqual(
            set(merged["candidates"][0]["discovery_modes"]), {"api", "broad"}
        )
        self.assertEqual(merged["search_coverage"]["api_candidate_count"], 1)
        self.assertEqual(merged["search_coverage"]["broad_candidate_count"], 1)
        self.assertEqual(merged["search_coverage"]["merged_candidate_count"], 1)

    def test_only_phase_5_runs_independent_broad_phase_and_skips_master(self):
        phase_payload = {
            "phase": "phase_5_cross_phase_broad_sweep",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "candidates": [],
            "search_coverage": {
                "queries_run": ["broad q1", "broad q2", "broad q3"],
                "broad_queries_run": ["broad q1", "broad q2", "broad q3"],
                "broad_candidate_count": 0,
                "merged_candidate_count": 0,
                "notable_zero_result_queries": [],
                "limitations": "",
            },
        }

        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            return json.dumps(phase_payload)

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.run_codex", side_effect=fake_run_codex
        ) as run:
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--db",
                    str(temp_path / "newsbot.sqlite3"),
                    "--only-phase",
                    "phase_5",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(output.with_suffix(".phase_5.prompt.txt").exists())
            self.assertTrue(output.with_suffix(".phase_5.json").exists())
            self.assertFalse(output.with_suffix(".phase_1.structured.prompt.txt").exists())
            self.assertFalse(output.with_suffix(".phase_1.broad.prompt.txt").exists())
            self.assertFalse(output.with_suffix(".master.prompt.txt").exists())
            self.assertFalse(output.exists())

    def test_main_single_agent_skip_codex_uses_legacy_topic_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            prompt_output = temp_path / "rendered.prompt.txt"
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"

            exit_code = main(
                [
                    "--topic",
                    "lab_automation",
                    "--cadence",
                    "weekly",
                    "--period",
                    "2026-05-30 to 2026-06-06 (JST)",
                    "--output",
                    str(output),
                    "--prompt-output",
                    str(prompt_output),
                    "--db",
                    str(db_path),
                    "--skip-codex",
                    "--single-agent",
                ]
            )

            prompt = prompt_output.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("Lab Automation Newsbot Payload Prompt", prompt)
        self.assertIn("2026-05-30 to 2026-06-06 (JST)", prompt)

    def test_parse_phase_models(self):
        parsed = parse_phase_models(["phase_1=gpt-a", "phase_4=gpt-b", "phase_5=gpt-c"])

        self.assertEqual(parsed, {"phase_1": "gpt-a", "phase_4": "gpt-b", "phase_5": "gpt-c"})

    def test_parse_phase_models_rejects_unknown_phase(self):
        with self.assertRaises(ValueError):
            parse_phase_models(["phase_9=gpt-x"])

    def test_parse_phase_reasoning(self):
        parsed = parse_phase_reasoning(["phase_1=max", "phase_4=xhigh", "phase_5=xhigh"])

        self.assertEqual(parsed, {"phase_1": "max", "phase_4": "xhigh", "phase_5": "xhigh"})

    def test_default_codex_timeout_is_ninety_minutes(self):
        self.assertEqual(DEFAULT_CODEX_TIMEOUT_SECONDS, 5400)

    def test_parse_phase_reasoning_rejects_unsupported_effort(self):
        with self.assertRaises(ValueError):
            parse_phase_reasoning(["phase_1=extreme"])

    def test_validate_phase_output_requires_candidate_fields(self):
        with self.assertRaises(ValueError):
            validate_phase_output(
                {
                    "phase": "phase_1_domestic_official",
                    "period": "2026-05-30 to 2026-06-06 (JST)",
                    "candidates": [{"title": "Only title"}],
                    "search_coverage": {},
                },
                expected_phase="phase_1_domestic_official",
            )

    def test_validate_phase_5_requires_three_broad_queries(self):
        with self.assertRaisesRegex(ValueError, "at least three queries"):
            validate_phase_output(
                {
                    "phase": "phase_5_cross_phase_broad_sweep",
                    "period": "2026-05-30 to 2026-06-06 (JST)",
                    "candidates": [],
                    "search_coverage": {
                        "queries_run": ["q1", "q2"],
                        "broad_queries_run": ["q1", "q2"],
                        "broad_candidate_count": 0,
                        "merged_candidate_count": 0,
                    },
                },
                expected_phase="phase_5_cross_phase_broad_sweep",
            )

    def test_normalize_broad_lane_repairs_unambiguous_llm_schema_slips(self):
        value = {
            "phase": "phase_1_domestic_official",
            "period": "2026-08-11 to 2026-08-18 (JST)",
            "discovery_mode": "broad",
            "candidates": [
                {
                    "title": "Autonomous experiment event",
                    "source": "Example University",
                    "url": "https://example.ac.jp/event",
                    "published_date": "2026-06-24",
                    "source_type": "official",
                    "geography": "Japan",
                    "lab_automation_relevance": "Direct relevance.",
                    "evidence": ["Autonomous experimentation platform."],
                    "confidence": "high",
                    "canonical_source_checked": True,
                    "duplicate_key": "autonomous-experiment-event",
                    "content_kind": "event_occurrence",
                    "event_date_start": "2026-08-17",
                    "event_date_end": "2026-08-17",
                    "date_basis": "event",
                    "original_publication_date": "2026-06-24",
                }
            ],
            "search_coverage": {
                "broad_queries_run": ["q1", "q2", "q3"],
                "broad_candidate_count": 1,
                "merged_candidate_count": 1,
                "limitations": "",
            },
        }

        normalized = normalize_discovery_lane_output(value, "broad")
        validated = validate_phase_output(
            normalized,
            expected_phase="phase_1_domestic_official",
            discovery_lane="broad",
        )

        candidate = validated["candidates"][0]
        self.assertEqual(candidate["discovery_mode"], "broad")
        self.assertEqual(candidate["discovery_modes"], ["broad"])
        self.assertEqual(candidate["confidence"], 0.85)
        self.assertEqual(len(validated["search_coverage"]["output_normalizations"]), 3)

    def test_normalize_broad_lane_recovers_numeric_query_count_from_codex_events(self):
        value = {
            "phase": "phase_2_pr_resolution",
            "period": "2026-08-11 to 2026-08-18 (JST)",
            "candidates": [],
            "search_coverage": {
                "broad_queries_run": 19,
                "broad_candidate_count": 0,
                "merged_candidate_count": 0,
                "limitations": "",
            },
        }

        normalized = normalize_discovery_lane_output(
            value,
            "broad",
            web_search_queries=["query one", "query two", "query three", "query one"],
        )
        validated = validate_phase_output(
            normalized,
            expected_phase="phase_2_pr_resolution",
            discovery_lane="broad",
        )

        coverage = validated["search_coverage"]
        self.assertEqual(
            coverage["broad_queries_run"],
            ["query one", "query two", "query three"],
        )
        self.assertEqual(coverage["broad_query_count_reported"], 19)
        self.assertEqual(
            coverage["output_normalizations"][0]["source"],
            "codex_web_search_events",
        )

    def test_normalize_broad_lane_falls_back_to_query_examples(self):
        value = {
            "phase": "phase_3_global_official",
            "period": "2026-08-11 to 2026-08-18 (JST)",
            "candidates": [],
            "search_coverage": {
                "broad_queries_run": 50,
                "broad_query_examples": ["example one", "example two", "example three"],
                "broad_candidate_count": 0,
                "merged_candidate_count": 0,
                "limitations": "",
            },
        }

        normalized = normalize_discovery_lane_output(value, "broad")

        self.assertEqual(
            normalized["search_coverage"]["broad_queries_run"],
            ["example one", "example two", "example three"],
        )
        self.assertEqual(
            normalized["search_coverage"]["output_normalizations"][0]["source"],
            "broad_query_examples",
        )

    def test_codex_web_search_queries_reads_completed_queries_and_dedupes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = Path(temp_dir) / "events.jsonl"
            events_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "web_search", "query": "query one"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "web_search", "query": "query two"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "web_search", "query": "query one"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                codex_web_search_queries(events_path), ["query one", "query two"]
            )

    def test_merge_continues_when_broad_lane_failed(self):
        structured = {
            "phase": "phase_1_domestic_official",
            "period": "2026-08-11 to 2026-08-18 (JST)",
            "candidates": [
                {
                    "title": "Structured candidate",
                    "source": "Example",
                    "url": "https://example.com/item",
                    "published_date": "2026-08-17",
                    "source_type": "official",
                    "geography": "Japan",
                    "lab_automation_relevance": "Direct relevance.",
                    "evidence": "Verified.",
                    "confidence": 0.8,
                    "canonical_source_checked": True,
                    "discovery_mode": "structured",
                    "discovery_modes": ["structured"],
                    "duplicate_key": "structured-candidate",
                    "content_kind": "news",
                    "event_date_start": "",
                    "event_date_end": "",
                    "date_basis": "publication",
                    "original_publication_date": "",
                }
            ],
            "search_coverage": {
                "structured_queries_run": ["q1"],
                "broad_queries_run": [],
                "structured_candidate_count": 1,
                "merged_candidate_count": 1,
                "limitations": "",
            },
        }
        validate_phase_output(
            structured,
            expected_phase="phase_1_domestic_official",
            discovery_lane="structured",
        )

        merged = merge_discovery_lane_outputs(
            structured,
            None,
            expected_phase="phase_1_domestic_official",
            lane_failures=[{"lane": "broad", "error": "invalid output"}],
        )

        self.assertEqual(len(merged["candidates"]), 1)
        self.assertEqual(merged["search_coverage"]["broad_queries_run"], [])
        self.assertEqual(merged["search_coverage"]["lane_failures"][0]["lane"], "broad")

    def test_validate_phase_4_output_accounts_for_every_api_candidate(self):
        paper_api_result = add_paper_api_candidate_ids(
            {
                "candidates": [
                    {
                        "title": "Selected API paper",
                        "source": "Crossref",
                        "url": "https://doi.org/10.1/selected",
                        "doi": "10.1/selected",
                        "published_date": "2026-06-01",
                    },
                    {
                        "title": "Rejected API paper",
                        "source": "Crossref",
                        "url": "https://doi.org/10.1/rejected",
                        "doi": "10.1/rejected",
                        "published_date": "2026-06-02",
                    },
                ]
            }
        )
        selected_id, rejected_id = [
            candidate["candidate_id"] for candidate in paper_api_result["candidates"]
        ]
        value = {
            "phase": "phase_4_papers",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "candidates": [
                {
                    "title": "Selected API paper",
                    "source": "Crossref",
                    "url": "https://doi.org/10.1/selected",
                    "published_date": "2026-06-01",
                    "source_type": "paper",
                    "geography": "Global",
                    "lab_automation_relevance": "Directly relevant.",
                    "evidence": "Verified example.",
                    "confidence": 0.9,
                    "canonical_source_checked": True,
                    "duplicate_key": "selected-api-paper",
                    "paper_api_candidate_ids": [selected_id],
                }
            ],
            "excluded_api_candidates": [
                {
                    "candidate_id": rejected_id,
                    "reason_code": "insufficient_directness",
                    "reason": "No direct connection to experimental automation.",
                }
            ],
            "search_coverage": {
                "queries_run": [],
                "notable_zero_result_queries": [],
                "limitations": "",
            },
        }

        validated = validate_phase_output(
            value,
            expected_phase="phase_4_papers",
            paper_api_candidates=paper_api_result["candidates"],
        )

        self.assertEqual(validated["paper_api_audit"]["input_candidate_count"], 2)
        self.assertEqual(validated["paper_api_audit"]["selected_candidate_count"], 1)
        self.assertEqual(validated["paper_api_audit"]["excluded_candidate_count"], 1)
        self.assertEqual(validated["excluded_api_candidates"][0]["title"], "Rejected API paper")
        self.assertEqual(validated["candidates"][0]["discovery_modes"], ["api"])

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "payload.json"
            artifact = write_phase_4_rejections(output, validated)
            saved = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(saved["paper_api_audit"]["unaccounted_candidate_count"], 0)
        self.assertEqual(saved["excluded_api_candidates"][0]["candidate_id"], rejected_id)

    def test_phase_4_llm_batches_cover_every_candidate_without_truncation(self):
        result = add_paper_api_candidate_ids(
            {
                "candidates": [
                    {
                        "title": f"Paper {index}",
                        "url": f"https://example.com/{index}",
                    }
                    for index in range(5)
                ],
                "coverage": {"candidate_count_after_dedupe": 5},
            }
        )

        batches = split_paper_api_result_for_llm(result, 2)

        self.assertEqual([len(batch["candidates"]) for batch in batches], [2, 2, 1])
        self.assertEqual(
            [
                candidate["candidate_id"]
                for batch in batches
                for candidate in batch["candidates"]
            ],
            [candidate["candidate_id"] for candidate in result["candidates"]],
        )
        self.assertEqual(batches[0]["coverage"]["llm_batch"]["batch_count"], 3)

    def test_merge_phase_4_llm_batches_combines_audit_and_duplicate_candidates(self):
        paper_api_result = add_paper_api_candidate_ids(
            {
                "candidates": [
                    {
                        "title": "Selected paper",
                        "source": "arXiv",
                        "url": "https://arxiv.org/abs/1",
                        "published_date": "2026-08-10",
                    },
                    {
                        "title": "Excluded paper",
                        "source": "arXiv",
                        "url": "https://arxiv.org/abs/2",
                        "published_date": "2026-08-11",
                    },
                ]
            }
        )
        selected_id, excluded_id = [
            candidate["candidate_id"] for candidate in paper_api_result["candidates"]
        ]
        selected_candidate = {
            "title": "Selected paper",
            "source": "arXiv",
            "url": "https://arxiv.org/abs/1",
            "published_date": "2026-08-10",
            "source_type": "preprint",
            "geography": "Global",
            "lab_automation_relevance": "Direct.",
            "evidence": "Verified.",
            "confidence": 0.9,
            "canonical_source_checked": True,
            "duplicate_key": "selected-paper",
            "paper_api_candidate_ids": [selected_id],
        }
        outputs = [
            {
                "phase": "phase_4_papers",
                "period": "2026-08-10 to 2026-08-17 (JST)",
                "candidates": [selected_candidate],
                "excluded_api_candidates": [],
                "search_coverage": {
                    "queries_run": ["query 1"],
                    "notable_zero_result_queries": [],
                    "limitations": "",
                },
            },
            {
                "phase": "phase_4_papers",
                "period": "2026-08-10 to 2026-08-17 (JST)",
                "candidates": [],
                "excluded_api_candidates": [
                    {
                        "candidate_id": excluded_id,
                        "reason_code": "insufficient_directness",
                        "reason": "No experimental execution link.",
                    }
                ],
                "search_coverage": {
                    "queries_run": ["query 2"],
                    "notable_zero_result_queries": ["zero query"],
                    "limitations": "Second batch could not open one publisher page.",
                },
            },
        ]

        merged = merge_phase_4_batch_outputs(
            outputs,
            paper_api_candidates=paper_api_result["candidates"],
        )

        self.assertEqual(merged["paper_api_audit"]["input_candidate_count"], 2)
        self.assertEqual(merged["paper_api_audit"]["selected_candidate_count"], 1)
        self.assertEqual(merged["paper_api_audit"]["excluded_candidate_count"], 1)
        self.assertEqual(merged["search_coverage"]["queries_run"], ["query 1", "query 2"])
        self.assertEqual(merged["search_coverage"]["llm_batch_count"], 2)

    def test_phase_4_broad_lane_can_continue_when_api_lane_fails(self):
        paper_api_result = add_paper_api_candidate_ids(
            {
                "candidates": [
                    {
                        "title": "Unevaluated API paper",
                        "source": "Crossref",
                        "url": "https://doi.org/10.1/unevaluated",
                        "doi": "10.1/unevaluated",
                        "published_date": "2026-06-01",
                    }
                ]
            }
        )
        broad_output = {
            "phase": "phase_4_papers",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "candidates": [],
            "search_coverage": {
                "queries_run": ["q1", "q2", "q3"],
                "broad_queries_run": ["q1", "q2", "q3"],
                "broad_candidate_count": 0,
                "merged_candidate_count": 0,
                "limitations": "",
            },
        }

        merged = merge_phase_4_lane_outputs(
            None,
            broad_output,
            paper_api_candidates=paper_api_result["candidates"],
            lane_failures=[{"lane": "api", "error": "schema failure"}],
        )

        self.assertEqual(merged["paper_api_audit"]["selected_candidate_count"], 0)
        self.assertEqual(merged["paper_api_audit"]["excluded_candidate_count"], 1)
        self.assertEqual(
            merged["excluded_api_candidates"][0]["reason_code"], "lane_failure"
        )
        self.assertEqual(
            merged["search_coverage"]["lane_failures"][0]["lane"], "api"
        )

    def test_validate_phase_4_output_rejects_unaccounted_api_candidate(self):
        paper_api_result = add_paper_api_candidate_ids(
            {
                "candidates": [
                    {
                        "title": "Unaccounted paper",
                        "source": "Crossref",
                        "url": "https://doi.org/10.1/unaccounted",
                        "doi": "10.1/unaccounted",
                        "published_date": "2026-06-01",
                    }
                ]
            }
        )
        value = {
            "phase": "phase_4_papers",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "candidates": [],
            "excluded_api_candidates": [],
            "search_coverage": {},
        }

        with self.assertRaisesRegex(ValueError, "missing a disposition"):
            validate_phase_output(
                value,
                expected_phase="phase_4_papers",
                paper_api_candidates=paper_api_result["candidates"],
            )

    def test_build_master_prompt_embeds_phase_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)
            prompt_dir = template_dir / "lab_automation"
            prompt_dir.mkdir()
            (prompt_dir / "master_builder.md").write_text(
                "Master {{TOPIC}} {{PHASE_OUTPUTS_JSON}} {{SOURCE_PRIORITY_JSON}} {{CONFIG_JSON}}",
                encoding="utf-8",
            )
            output = template_dir / "payload.json"
            prompt, template_path = build_master_prompt(
                topic="lab_automation",
                cadence="weekly",
                period="2026-05-30 to 2026-06-06 (JST)",
                max_items=10,
                output_path=output,
                config_path=Path("config/newsbot.config.json"),
                preference_profile=None,
                phase_outputs=[{"phase": "phase_1_domestic_official", "candidates": [{"title": "Candidate A"}]}],
                config_raw={"source_priority": ["official"]},
                template_dir=template_dir,
            )

        self.assertEqual(template_path.name, "master_builder.md")
        self.assertIn("Candidate A", prompt)
        self.assertIn("official", prompt)

    def test_multi_agent_codex_order_models_and_artifacts(self):
        phase_payloads = [
            {
                "phase": phase.phase,
                "period": "2026-05-30 to 2026-06-06 (JST)",
                "candidates": [
                    {
                        "title": f"{phase.key} candidate",
                        "source": "Example",
                        "url": f"https://example.com/{phase.key}",
                        "published_date": "2026-06-01",
                        "source_type": "official",
                        "geography": "Japan",
                        "lab_automation_relevance": "Directly relevant.",
                        "evidence": "Verified example.",
                        "confidence": 0.8,
                        "canonical_source_checked": True,
                        **(
                            {
                                "discovery_mode": "broad",
                                "discovery_modes": ["broad"],
                            }
                            if phase.key != "phase_4"
                            else {}
                        ),
                        "duplicate_key": f"{phase.key}-candidate",
                    }
                ],
                "search_coverage": {
                    "queries_run": ["q"],
                    **(
                        {
                            "structured_queries_run": ["structured q"],
                            "broad_queries_run": ["broad q1", "broad q2", "broad q3"],
                            "structured_candidate_count": 0,
                            "broad_candidate_count": 1,
                            "merged_candidate_count": 1,
                        }
                        if phase.key in {"phase_1", "phase_2", "phase_3"}
                        else {
                            "broad_queries_run": ["broad q1", "broad q2", "broad q3"],
                            "broad_candidate_count": 1,
                            "merged_candidate_count": 1,
                        }
                        if phase.key in {"phase_4", "phase_5"}
                        else {}
                    ),
                    "notable_zero_result_queries": [],
                    "limitations": "",
                },
            }
            for phase in lab_automation_phase_definitions()
        ]
        master_payload = {
            "topic": "lab_automation",
            "cadence": "weekly",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "items": [
                {
                    "title": "Master selected candidate",
                    "summary": "A useful update.",
                    "source": "Example",
                    "url": "https://example.com/final",
                    "category_primary": "research_infrastructure",
                    "tags": ["lab-automation"],
                    "importance_score": 0.9,
                    "priority": "high",
                }
            ],
        }
        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            name = kwargs["log_path"].name
            if ".master." in name:
                return json.dumps(master_payload)
            phase_key = next(phase.key for phase in lab_automation_phase_definitions() if f".{phase.key}." in name)
            value = json.loads(json.dumps(next(item for item in phase_payloads if item["phase"] == next(phase.phase for phase in lab_automation_phase_definitions() if phase.key == phase_key))))
            if ".structured." in name:
                value["candidates"][0]["discovery_mode"] = "structured"
                value["candidates"][0]["discovery_modes"] = ["structured"]
                value["search_coverage"]["broad_queries_run"] = []
                value["search_coverage"]["structured_candidate_count"] = 1
            elif ".broad." in name:
                value["candidates"][0]["discovery_mode"] = "broad"
                value["candidates"][0]["discovery_modes"] = ["broad"]
                value["search_coverage"].pop("structured_queries_run", None)
                value["search_coverage"].pop("structured_candidate_count", None)
            return json.dumps(value)

        env_overrides = {
            "NEWSBOT_CODEX_MODEL": "",
            "NEWSBOT_CODEX_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_5_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_MASTER_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_5_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, env_overrides, clear=False), patch(
            "scripts.generate_payload_openai.run_codex", side_effect=fake_run_codex
        ) as run:
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"
            with patch("scripts.generate_payload_openai.run_newsbot_command", return_value=0), patch("scripts.generate_payload_openai.write_last_run"):
                exit_code = main(
                    [
                        "--topic",
                        "lab_automation",
                        "--cadence",
                        "weekly",
                        "--period",
                        "2026-05-30 to 2026-06-06 (JST)",
                        "--output",
                        str(output),
                        "--db",
                        str(db_path),
                        "--phase-model",
                        "phase_1=gpt-phase-1",
                        "--phase-model",
                        "phase_4=gpt-phase-4",
                        "--master-model",
                        "gpt-master",
                        "--phase-reasoning",
                        "phase_1=xhigh",
                        "--phase-reasoning",
                        "phase_4=low",
                        "--master-reasoning-effort",
                        "high",
                        "--disable-paper-api",
                        "--validate-only",
                    ]
                )

                self.assertEqual(exit_code, 0)
                self.assertEqual(run.call_count, 9)
                call_by_log = {call.kwargs["log_path"].name: call.kwargs for call in run.call_args_list}
                self.assertEqual(call_by_log["payload.phase_1.structured.attempt_1.codex.log"]["codex_model"], "gpt-phase-1")
                self.assertEqual(call_by_log["payload.phase_1.broad.attempt_1.codex.log"]["reasoning_effort"], "xhigh")
                self.assertEqual(
                    call_by_log[
                        "payload.phase_4.broad.attempt_1.codex.log"
                    ]["codex_model"],
                    "gpt-phase-4",
                )
                self.assertTrue(
                    output.with_suffix(".phase_4.broad.codex.log").exists()
                )
                self.assertEqual(call_by_log["payload.master.codex.log"]["codex_model"], "gpt-master")
                self.assertTrue(output.exists())
                self.assertTrue(output.with_suffix(".phase_1.structured.prompt.txt").exists())
                self.assertTrue(output.with_suffix(".phase_1.broad.prompt.txt").exists())
                self.assertTrue(output.with_suffix(".phase_1.json").exists())
                self.assertTrue(output.with_suffix(".phase_1.structured.codex.log").exists())
                self.assertTrue(output.with_suffix(".phase_1.broad.codex.log").exists())
                self.assertTrue(output.with_suffix(".phase_1.structured.attempt_1.codex.log").exists())
                self.assertTrue(output.with_suffix(".phase_1.broad.attempt_1.codex.log").exists())
                self.assertTrue(output.with_suffix(".phase_1.broad.attempts.audit.json").exists())
                self.assertTrue(output.with_suffix(".master.prompt.txt").exists())
                self.assertTrue(output.with_suffix(".master.json").exists())
                self.assertTrue(output.with_suffix(".master.codex.log").exists())

    def test_multi_agent_uses_env_model_and_reasoning_defaults(self):
        phase_payloads = [
            {
                "phase": phase.phase,
                "period": "2026-05-30 to 2026-06-06 (JST)",
                "candidates": [
                    {
                        "title": f"{phase.key} candidate",
                        "source": "Example",
                        "url": f"https://example.com/{phase.key}",
                        "published_date": "2026-06-01",
                        "source_type": "official",
                        "geography": "Japan",
                        "lab_automation_relevance": "Directly relevant.",
                        "evidence": "Verified example.",
                        "confidence": 0.8,
                        "canonical_source_checked": True,
                        **(
                            {
                                "discovery_mode": "broad",
                                "discovery_modes": ["broad"],
                            }
                            if phase.key != "phase_4"
                            else {}
                        ),
                        "duplicate_key": f"{phase.key}-candidate",
                    }
                ],
                "search_coverage": {
                    "queries_run": ["q"],
                    **(
                        {
                            "structured_queries_run": ["structured q"],
                            "broad_queries_run": ["broad q1", "broad q2", "broad q3"],
                            "structured_candidate_count": 0,
                            "broad_candidate_count": 1,
                            "merged_candidate_count": 1,
                        }
                        if phase.key in {"phase_1", "phase_2", "phase_3"}
                        else {
                            "broad_queries_run": ["broad q1", "broad q2", "broad q3"],
                            "broad_candidate_count": 1,
                            "merged_candidate_count": 1,
                        }
                        if phase.key in {"phase_4", "phase_5"}
                        else {}
                    ),
                    "notable_zero_result_queries": [],
                    "limitations": "",
                },
            }
            for phase in lab_automation_phase_definitions()
        ]
        master_payload = {
            "topic": "lab_automation",
            "cadence": "weekly",
            "period": "2026-05-30 to 2026-06-06 (JST)",
            "items": [
                {
                    "title": "Master selected candidate",
                    "summary": "A useful update.",
                    "source": "Example",
                    "url": "https://example.com/final",
                    "category_primary": "research_infrastructure",
                    "tags": ["lab-automation"],
                    "importance_score": 0.9,
                    "priority": "high",
                }
            ],
        }
        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            name = kwargs["log_path"].name
            if ".master." in name:
                return json.dumps(master_payload)
            phase_def = next(phase for phase in lab_automation_phase_definitions() if f".{phase.key}." in name)
            value = json.loads(json.dumps(next(item for item in phase_payloads if item["phase"] == phase_def.phase)))
            if ".structured." in name:
                value["candidates"][0]["discovery_mode"] = "structured"
                value["candidates"][0]["discovery_modes"] = ["structured"]
                value["search_coverage"]["broad_queries_run"] = []
                value["search_coverage"]["structured_candidate_count"] = 1
            elif ".broad." in name:
                value["candidates"][0]["discovery_mode"] = "broad"
                value["candidates"][0]["discovery_modes"] = ["broad"]
                value["search_coverage"].pop("structured_queries_run", None)
                value["search_coverage"].pop("structured_candidate_count", None)
            return json.dumps(value)

        env_overrides = {
            "NEWSBOT_CODEX_MODEL": "gpt-default",
            "NEWSBOT_CODEX_REASONING_EFFORT": "medium",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_MODEL": "gpt-env-phase-2",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_5_MODEL": "gpt-env-phase-5",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_REASONING_EFFORT": "xhigh",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_5_REASONING_EFFORT": "xhigh",
            "NEWSBOT_LAB_AUTOMATION_MASTER_MODEL": "gpt-env-master",
            "NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT": "high",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, env_overrides, clear=False), patch(
            "scripts.generate_payload_openai.run_codex", side_effect=fake_run_codex
        ) as run:
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"
            with patch("scripts.generate_payload_openai.run_newsbot_command", return_value=0), patch("scripts.generate_payload_openai.write_last_run"):
                exit_code = main(
                    [
                        "--topic",
                        "lab_automation",
                        "--cadence",
                        "weekly",
                        "--period",
                        "2026-05-30 to 2026-06-06 (JST)",
                        "--output",
                        str(output),
                        "--db",
                        str(db_path),
                        "--disable-paper-api",
                        "--validate-only",
                    ]
                )

        self.assertEqual(exit_code, 0)
        call_by_log = {call.kwargs["log_path"].name: call.kwargs for call in run.call_args_list}
        self.assertEqual(len(call_by_log), 9)
        self.assertEqual(call_by_log["payload.phase_2.structured.attempt_1.codex.log"]["codex_model"], "gpt-env-phase-2")
        self.assertEqual(call_by_log["payload.phase_2.broad.attempt_1.codex.log"]["codex_model"], "gpt-env-phase-2")
        self.assertEqual(call_by_log["payload.phase_3.broad.attempt_1.codex.log"]["reasoning_effort"], "xhigh")
        self.assertEqual(
            call_by_log["payload.phase_4.broad.attempt_1.codex.log"]["codex_model"],
            "gpt-default",
        )
        self.assertEqual(call_by_log["payload.phase_5.codex.log"]["codex_model"], "gpt-env-phase-5")
        self.assertEqual(call_by_log["payload.master.codex.log"]["reasoning_effort"], "high")

    def test_multi_agent_phase_failure_does_not_write_final_payload(self):
        bad_phase = json.dumps(
            {
                "phase": "phase_1_domestic_official",
                "period": "2026-05-30 to 2026-06-06 (JST)",
                "candidates": [{"title": "Missing fields"}],
                "search_coverage": {},
            }
        )

        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            return bad_phase

        with tempfile.TemporaryDirectory() as temp_dir, patch("scripts.generate_payload_openai.run_codex", side_effect=fake_run_codex):
            temp_path = Path(temp_dir)
            output = temp_path / "payload.json"
            db_path = temp_path / "newsbot.sqlite3"
            with self.assertRaises(ValueError):
                main(
                    [
                        "--topic",
                        "lab_automation",
                        "--cadence",
                        "weekly",
                        "--period",
                        "2026-05-30 to 2026-06-06 (JST)",
                        "--output",
                        str(output),
                        "--db",
                        str(db_path),
                    ]
                )

            self.assertFalse(output.exists())
            self.assertTrue(
                output.with_suffix(".phase_1.broad.attempt_1.codex.log").exists()
            )
            self.assertTrue(
                output.with_suffix(".phase_1.broad.attempt_2.codex.log").exists()
            )
            self.assertIn(
                "Output repair required",
                output.with_suffix(".phase_1.broad.attempt_2.prompt.txt").read_text(
                    encoding="utf-8"
                ),
            )
            attempts_audit = json.loads(
                output.with_suffix(
                    ".phase_1.broad.attempts.audit.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["status"] for item in attempts_audit["attempts"]],
                ["rejected", "rejected"],
            )

    def test_select_review_items_sorts_and_limits(self):
        payload = {
            "topic": "lab_automation",
            "cadence": "weekly",
            "period": "x",
            "items": [
                {
                    "title": f"Item {index:02d}",
                    "url": f"https://example.com/{index}",
                    "importance_score": index / 100,
                }
                for index in range(35)
            ],
        }

        selected = select_review_items(payload, preference_profile=None, limit=30)

        self.assertEqual(len(selected["items"]), 30)
        self.assertEqual(selected["items"][0]["title"], "Item 34")

    def test_run_codex_passes_model_argument(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def fake_run(command, **kwargs):
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text('{"topic":"lab_automation","cadence":"weekly","period":"x","items":[]}', encoding="utf-8")
            return completed

        with tempfile.TemporaryDirectory() as temp_dir, patch("scripts.generate_payload_openai.subprocess.run", side_effect=fake_run) as run:
            run_codex(
                codex_bin="codex",
                codex_model="gpt-test",
                reasoning_effort="",
                codex_args=[],
                prompt="prompt",
                timeout=1,
                log_path=Path(temp_dir) / "codex.log",
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "--search", "exec"])
        self.assertIn("--model", command)
        self.assertIn("gpt-test", command)
        self.assertIn("--search", command)
        self.assertIn("--json", command)

    def test_run_codex_does_not_duplicate_search_argument(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def fake_run(command, **kwargs):
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text('{"topic":"lab_automation","cadence":"weekly","period":"x","items":[]}', encoding="utf-8")
            return completed

        with tempfile.TemporaryDirectory() as temp_dir, patch("scripts.generate_payload_openai.subprocess.run", side_effect=fake_run) as run:
            run_codex(
                codex_bin="codex",
                codex_model="",
                reasoning_effort="",
                codex_args=["--search", "--json"],
                prompt="prompt",
                timeout=1,
                log_path=Path(temp_dir) / "codex.log",
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "--search", "exec"])
        self.assertEqual(command.count("--search"), 1)
        self.assertEqual(command.count("--json"), 1)

    def test_run_codex_can_disable_search_for_controlled_evaluation(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def fake_run(command, **kwargs):
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                '{"topic":"lab_automation","cadence":"weekly","period":"x","items":[]}',
                encoding="utf-8",
            )
            return completed

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.subprocess.run",
            side_effect=fake_run,
        ) as run:
            run_codex(
                codex_bin="codex",
                codex_model="",
                reasoning_effort="",
                codex_args=["--search"],
                prompt="prompt",
                timeout=1,
                log_path=Path(temp_dir) / "codex.log",
                enable_search=False,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertNotIn("--search", command)
        self.assertIn("--json", command)

    def test_run_codex_passes_reasoning_effort_config(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def fake_run(command, **kwargs):
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text('{"topic":"lab_automation","cadence":"weekly","period":"x","items":[]}', encoding="utf-8")
            return completed

        with tempfile.TemporaryDirectory() as temp_dir, patch("scripts.generate_payload_openai.subprocess.run", side_effect=fake_run) as run:
            run_codex(
                codex_bin="codex",
                codex_model="",
                reasoning_effort="xhigh",
                codex_args=[],
                prompt="prompt",
                timeout=1,
                log_path=Path(temp_dir) / "codex.log",
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["codex", "--search", "--config", 'model_reasoning_effort="xhigh"'])
        self.assertIn("exec", command)

    def test_run_codex_writes_jsonl_events_and_usage_summary(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": '{"items":[]}'},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 600,
                            "output_tokens": 200,
                            "reasoning_output_tokens": 50,
                        },
                    }
                ),
            ]
        )
        completed = type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        def fake_run(command, **kwargs):
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text('{"items":[]}', encoding="utf-8")
            return completed

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.generate_payload_openai.subprocess.run", side_effect=fake_run
        ):
            log_path = Path(temp_dir) / "sample.phase_1.codex.log"
            output = run_codex(
                codex_bin="codex",
                codex_model="gpt-5.6-luna",
                reasoning_effort="high",
                codex_args=[],
                prompt="prompt",
                timeout=1,
                log_path=log_path,
            )
            events_path = Path(temp_dir) / "sample.phase_1.codex.events.jsonl"
            usage_path = Path(temp_dir) / "sample.phase_1.codex.usage.json"

            self.assertEqual(output, '{"items":[]}')
            self.assertEqual(events_path.read_text(encoding="utf-8"), stdout)
            summary = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["usage"]["input_tokens"], 1000)
            self.assertEqual(summary["usage"]["cached_input_tokens"], 600)
            self.assertEqual(summary["usage"]["non_cached_input_tokens"], 400)
            self.assertEqual(summary["usage"]["output_tokens"], 200)
            self.assertEqual(summary["usage"]["reasoning_output_tokens"], 50)
            self.assertEqual(summary["usage"]["total_tokens"], 1200)


if __name__ == "__main__":
    unittest.main()
