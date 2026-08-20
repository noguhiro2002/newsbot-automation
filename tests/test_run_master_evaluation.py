import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsbot.config import DEFAULT_CONFIG_PATH, load_config
from scripts.run_master_evaluation import (
    build_fixed_candidate_pool,
    main,
)


PERIOD = "2026-07-18 to 2026-07-25 (JST)"


def candidate(url: str, title: str, source_type: str = "official"):
    return {
        "title": title,
        "source": "Example",
        "url": url,
        "published_date": "2026-07-20",
        "source_type": source_type,
        "geography": "Japan",
        "lab_automation_relevance": "Directly relevant.",
        "evidence": "Verified candidate evidence.",
        "confidence": 0.8,
        "canonical_source_checked": True,
        "duplicate_key": title.lower().replace(" ", "-"),
    }


class MasterEvaluationTests(unittest.TestCase):
    def write_source_run(
        self,
        root: Path,
        name: str,
        shared_url: str,
    ) -> Path:
        stem = root / name
        for phase_number in range(1, 5):
            phase_name = (
                "phase_1_domestic_official"
                if phase_number == 1
                else f"phase_{phase_number}"
            )
            payload = {
                "phase": phase_name,
                "period": PERIOD,
                "candidates": [
                    candidate(
                        shared_url,
                        "Shared candidate",
                    ),
                    candidate(
                        f"https://example.com/{name}/{phase_number}",
                        f"{name} candidate {phase_number}",
                    ),
                ],
                "search_coverage": {},
            }
            (root / f"{name}.phase_{phase_number}.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        return stem

    def make_pool(self, root: Path):
        run_1 = self.write_source_run(
            root,
            "run_1",
            "https://www.example.com/shared/?utm_source=x",
        )
        run_2 = self.write_source_run(
            root,
            "run_2",
            "https://example.com/shared",
        )
        pool = build_fixed_candidate_pool(
            source_runs=[run_1, run_2],
            period=PERIOD,
            seed=101,
        )
        pool["config_path"] = str(DEFAULT_CONFIG_PATH)
        pool["config_raw"] = load_config(DEFAULT_CONFIG_PATH).raw
        pool["preference_profile"] = None
        return pool

    def test_fixed_pool_is_deterministic_and_tracks_votes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pool_1 = self.make_pool(root)
            pool_2 = build_fixed_candidate_pool(
                source_runs=[root / "run_1", root / "run_2"],
                period=PERIOD,
                seed=101,
            )

        self.assertEqual(
            pool_1["candidate_order_digest"],
            pool_2["candidate_order_digest"],
        )
        self.assertEqual(pool_1["candidate_count"], 9)
        self.assertEqual(len(pool_1["missing_optional_phase_outputs"]), 2)
        shared = next(
            item
            for item in pool_1["phase_outputs"][0]["candidates"]
            if "/shared" in item["url"]
        )
        self.assertEqual(shared["paired_vote_count"], 2)
        self.assertEqual(shared["paired_origin_count"], 8)

    def test_fixed_pool_requires_exactly_two_source_runs(self):
        with self.assertRaisesRegex(ValueError, "exactly two"):
            build_fixed_candidate_pool(
                source_runs=["one"],
                period=PERIOD,
                seed=101,
            )

    def test_run_master_disables_search_and_writes_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pool = self.make_pool(root)
            pool_path = root / "pool.json"
            pool_path.write_text(json.dumps(pool), encoding="utf-8")
            selected = pool["phase_outputs"][0]["candidates"][0]
            output = root / "result.json"

            def fake_run_codex(**kwargs):
                self.assertFalse(kwargs["enable_search"])
                self.assertIn(
                    pool["candidate_order_digest"],
                    kwargs["prompt"],
                )
                return json.dumps(
                    {
                        "topic": "lab_automation",
                        "cadence": "weekly",
                        "period": PERIOD,
                        "items": [
                            {
                                "title": selected["title"],
                                "summary": (
                                    "2026.07.20のLab Automation更新。"
                                ),
                                "source": selected["source"],
                                "url": selected["url"],
                                "category_primary": (
                                    "research_infrastructure"
                                ),
                                "tags": ["lab-automation"],
                                "importance_score": 0.8,
                                "priority": "high",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )

            with patch(
                "scripts.run_master_evaluation.run_codex",
                side_effect=fake_run_codex,
            ):
                exit_code = main(
                    [
                        "run-master",
                        "--candidate-pool",
                        str(pool_path),
                        "--master-model",
                        "gpt-test",
                        "--reasoning-effort",
                        "high",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            metadata = json.loads(
                output.with_suffix(".metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["selected_item_count"], 1)
            self.assertEqual(
                metadata["candidate_order_digest"],
                pool["candidate_order_digest"],
            )

    def test_run_master_rejects_url_outside_fixed_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pool = self.make_pool(root)
            pool_path = root / "pool.json"
            pool_path.write_text(json.dumps(pool), encoding="utf-8")

            external_payload = {
                "topic": "lab_automation",
                "cadence": "weekly",
                "period": PERIOD,
                "items": [
                    {
                        "title": "External",
                        "summary": "External candidate.",
                        "source": "Example",
                        "url": "https://outside.example/news",
                        "category_primary": "robotics",
                        "tags": ["lab-automation"],
                        "importance_score": 0.5,
                        "priority": "normal",
                    }
                ],
            }
            with patch(
                "scripts.run_master_evaluation.run_codex",
                return_value=json.dumps(external_payload),
            ):
                with self.assertRaisesRegex(
                    ValueError, "outside the fixed pool"
                ):
                    main(
                        [
                            "run-master",
                            "--candidate-pool",
                            str(pool_path),
                            "--master-model",
                            "gpt-test",
                            "--reasoning-effort",
                            "high",
                            "--output",
                            str(root / "result.json"),
                        ]
                    )


if __name__ == "__main__":
    unittest.main()
