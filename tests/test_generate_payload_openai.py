import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts.generate_payload_openai import (
    build_prompt,
    build_generation_prompt,
    build_master_prompt,
    default_output_path,
    default_period,
    extract_json_object,
    lab_automation_phase_definitions,
    main,
    parse_phase_models,
    parse_phase_reasoning,
    read_last_run,
    resolve_period,
    run_codex,
    select_review_items,
    validate_phase_output,
    validate_payload_dict,
    write_last_run,
)


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
                self.assertTrue((output.with_suffix(f".{phase.key}.prompt.txt")).exists())
            self.assertTrue(output.with_suffix(".phase_4.paper_api.json").exists())
            phase_4_prompt = output.with_suffix(".phase_4.prompt.txt").read_text(encoding="utf-8")
            self.assertIn("API candidate paper", phase_4_prompt)

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
        parsed = parse_phase_models(["phase_1=gpt-a", "phase_4=gpt-b"])

        self.assertEqual(parsed, {"phase_1": "gpt-a", "phase_4": "gpt-b"})

    def test_parse_phase_models_rejects_unknown_phase(self):
        with self.assertRaises(ValueError):
            parse_phase_models(["phase_9=gpt-x"])

    def test_parse_phase_reasoning(self):
        parsed = parse_phase_reasoning(["phase_1=xhigh", "phase_4=high"])

        self.assertEqual(parsed, {"phase_1": "xhigh", "phase_4": "high"})

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
                        "duplicate_key": f"{phase.key}-candidate",
                    }
                ],
                "search_coverage": {"queries_run": ["q"], "notable_zero_result_queries": [], "limitations": ""},
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
        outputs = [json.dumps(value) for value in [*phase_payloads, master_payload]]

        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            return outputs.pop(0)

        env_overrides = {
            "NEWSBOT_CODEX_MODEL": "",
            "NEWSBOT_CODEX_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_MASTER_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_REASONING_EFFORT": "",
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
                self.assertEqual(run.call_count, 5)
                models = [call.kwargs["codex_model"] for call in run.call_args_list]
                self.assertEqual(models, ["gpt-phase-1", "", "", "gpt-phase-4", "gpt-master"])
                reasoning_efforts = [call.kwargs["reasoning_effort"] for call in run.call_args_list]
                self.assertEqual(reasoning_efforts, ["xhigh", "", "", "low", "high"])
                self.assertTrue(output.exists())
                self.assertTrue(output.with_suffix(".phase_1.prompt.txt").exists())
                self.assertTrue(output.with_suffix(".phase_1.json").exists())
                self.assertTrue(output.with_suffix(".phase_1.codex.log").exists())
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
                        "duplicate_key": f"{phase.key}-candidate",
                    }
                ],
                "search_coverage": {"queries_run": ["q"], "notable_zero_result_queries": [], "limitations": ""},
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
        outputs = [json.dumps(value) for value in [*phase_payloads, master_payload]]

        def fake_run_codex(**kwargs):
            kwargs["log_path"].write_text("fake log", encoding="utf-8")
            return outputs.pop(0)

        env_overrides = {
            "NEWSBOT_CODEX_MODEL": "gpt-default",
            "NEWSBOT_CODEX_REASONING_EFFORT": "medium",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_MODEL": "gpt-env-phase-2",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_MODEL": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_2_REASONING_EFFORT": "",
            "NEWSBOT_LAB_AUTOMATION_PHASE_3_REASONING_EFFORT": "xhigh",
            "NEWSBOT_LAB_AUTOMATION_PHASE_4_REASONING_EFFORT": "",
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
        models = [call.kwargs["codex_model"] for call in run.call_args_list]
        self.assertEqual(models, ["gpt-default", "gpt-env-phase-2", "gpt-default", "gpt-default", "gpt-env-master"])
        reasoning_efforts = [call.kwargs["reasoning_effort"] for call in run.call_args_list]
        self.assertEqual(reasoning_efforts, ["medium", "medium", "xhigh", "medium", "high"])

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
