from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsbot.config import DEFAULT_CONFIG_PATH, load_config, resolve_project_path
from newsbot.db import DEFAULT_DB_PATH, NewsbotStore
from newsbot.models import sha256_text, stable_json
from scripts.generate_payload_openai import (
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    SUPPORTED_REASONING_EFFORTS,
    build_master_prompt,
    codex_sibling_artifact_path,
    extract_json_object,
    lab_automation_phase_definitions,
    load_env_file,
    run_codex,
    validate_payload_dict,
    write_json,
)


SOURCE_TYPE_PRIORITY = {
    "official": 4,
    "paper": 3,
    "preprint": 3,
    "pr_distribution": 2,
    "media": 1,
}


def comparison_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def source_run_stem(value: str | Path) -> Path:
    path = resolve_project_path(value)
    text = str(path)
    for suffix in (".master.json", ".json"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return Path(text)


def _candidate_score(candidate: dict[str, Any]) -> tuple[Any, ...]:
    try:
        confidence = float(candidate.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    source_type = str(candidate.get("source_type") or "").lower()
    return (
        SOURCE_TYPE_PRIORITY.get(source_type, 0),
        bool(candidate.get("canonical_source_checked")),
        confidence,
        len(str(candidate.get("evidence") or "")),
        stable_json(candidate),
    )


def build_fixed_candidate_pool(
    *,
    source_runs: list[str | Path],
    period: str,
    seed: int,
) -> dict[str, Any]:
    if len(source_runs) != 2:
        raise ValueError(
            "Paired Master audit requires exactly two --source-run values"
        )
    if not period.strip():
        raise ValueError("period must not be empty")

    stems = [source_run_stem(value) for value in source_runs]
    candidates_by_url: dict[str, dict[str, Any]] = {}
    origins_by_url: dict[str, list[dict[str, str]]] = {}

    for stem in stems:
        for phase in lab_automation_phase_definitions():
            phase_path = stem.with_name(
                f"{stem.name}.{phase.key}.json"
            )
            if not phase_path.exists():
                raise FileNotFoundError(
                    f"Missing source phase output: {phase_path}"
                )
            phase_output = json.loads(
                phase_path.read_text(encoding="utf-8")
            )
            source_period = str(phase_output.get("period") or "")
            if source_period != period:
                raise ValueError(
                    f"Period mismatch in {phase_path}: "
                    f"{source_period!r} != {period!r}"
                )

            for candidate in phase_output.get("candidates") or []:
                if not isinstance(candidate, dict):
                    raise ValueError(
                        f"Non-object candidate in {phase_path}"
                    )
                url_key = comparison_url(candidate.get("url"))
                if not url_key:
                    raise ValueError(
                        f"Candidate without a comparable URL in {phase_path}"
                    )
                origin = {
                    "source_run": stem.name,
                    "source_phase": phase.key,
                }
                origins_by_url.setdefault(url_key, []).append(origin)
                current = candidates_by_url.get(url_key)
                if current is None or _candidate_score(
                    candidate
                ) > _candidate_score(current):
                    candidates_by_url[url_key] = dict(candidate)

    pooled_candidates = []
    for url_key in sorted(candidates_by_url):
        candidate = dict(candidates_by_url[url_key])
        origins = origins_by_url[url_key]
        candidate["paired_vote_count"] = len(
            {origin["source_run"] for origin in origins}
        )
        candidate["paired_origin_count"] = len(origins)
        candidate["paired_origins"] = origins
        pooled_candidates.append(candidate)

    random.Random(seed).shuffle(pooled_candidates)
    candidate_order_digest = sha256_text(
        stable_json(pooled_candidates)
    )
    phase_output = {
        "phase": "paired_fixed_candidate_pool",
        "period": period,
        "candidates": pooled_candidates,
        "search_coverage": {
            "queries_run": [],
            "notable_zero_result_queries": [],
            "limitations": (
                "Fixed retrospective pool; Master browsing is disabled."
            ),
        },
    }
    return {
        "schema_version": 1,
        "topic": "lab_automation",
        "cadence": "weekly",
        "period": period,
        "candidate_seed": seed,
        "candidate_count": len(pooled_candidates),
        "candidate_order_digest": candidate_order_digest,
        "source_runs": [stem.name for stem in stems],
        "phase_outputs": [phase_output],
    }


def prepare_pool(args: argparse.Namespace) -> int:
    config_path = resolve_project_path(args.config)
    config = load_config(config_path)
    store = NewsbotStore(args.db)
    store.init()
    preference_profile = store.audience_preference_profile(
        topic="lab_automation",
        cadence="weekly",
    )
    pool = build_fixed_candidate_pool(
        source_runs=list(args.source_run),
        period=args.period,
        seed=args.seed,
    )
    pool["config_path"] = str(config_path)
    pool["config_raw"] = config.raw
    pool["preference_profile"] = preference_profile

    output_path = resolve_project_path(args.output)
    if output_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite candidate pool: {output_path}"
        )
    write_json(output_path, pool)
    print(
        "Prepared fixed candidate pool: "
        f"seed={args.seed}, candidates={pool['candidate_count']}, "
        f"digest={pool['candidate_order_digest']}, output={output_path}",
        flush=True,
    )
    return 0


def _controlled_master_instructions(
    candidate_order_digest: str,
) -> str:
    return f"""

## Controlled Master Evaluation Override

This is a paired, selection-only experiment.

* Do not browse or search the Web.
* Select only from the candidate objects embedded above.
* Do not add, replace, resolve, or rewrite a candidate URL.
* Preserve every selected candidate URL exactly as supplied.
* Rank the selected items from most to least important.
* Do not fill the list with low-relevance items merely to increase count.
* `paired_vote_count` is a soft ranking signal, not a hard inclusion rule.
* Candidate-order digest: `{candidate_order_digest}`
""".rstrip()


def run_master(args: argparse.Namespace) -> int:
    load_env_file()
    pool_path = resolve_project_path(args.candidate_pool)
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    phase_outputs = pool.get("phase_outputs")
    if not isinstance(phase_outputs, list) or len(phase_outputs) != 1:
        raise ValueError(
            "candidate pool must contain exactly one phase_outputs entry"
        )
    candidates = phase_outputs[0].get("candidates") or []
    if len(candidates) != int(pool.get("candidate_count") or -1):
        raise ValueError("candidate_count does not match phase_outputs")
    calculated_digest = sha256_text(stable_json(candidates))
    expected_digest = str(pool.get("candidate_order_digest") or "")
    if calculated_digest != expected_digest:
        raise ValueError("candidate pool order digest mismatch")

    config_path = resolve_project_path(args.config)
    config_raw = pool.get("config_raw")
    if not isinstance(config_raw, dict):
        config_raw = load_config(config_path).raw
    max_items = min(
        int(config_raw.get("max_items", 30)),
        int(args.max_items),
    )
    output_path = resolve_project_path(args.output)
    prompt_path = output_path.with_suffix(".prompt.txt")
    log_path = output_path.with_suffix(".codex.log")
    metadata_path = output_path.with_suffix(".metadata.json")
    protected_paths = [
        output_path,
        prompt_path,
        log_path,
        metadata_path,
        codex_sibling_artifact_path(log_path, ".codex.events.jsonl"),
        codex_sibling_artifact_path(log_path, ".codex.usage.json"),
    ]
    existing = [path for path in protected_paths if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite Master audit artifacts: "
            + ", ".join(str(path) for path in existing)
        )

    prompt, template_path = build_master_prompt(
        topic=str(pool.get("topic") or "lab_automation"),
        cadence=str(pool.get("cadence") or "weekly"),
        period=str(pool["period"]),
        max_items=max_items,
        output_path=output_path,
        config_path=config_path,
        preference_profile=pool.get("preference_profile"),
        phase_outputs=phase_outputs,
        config_raw=config_raw,
    )
    prompt = (
        f"{prompt.rstrip()}\n\n"
        f"{_controlled_master_instructions(expected_digest)}\n"
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    master_output = run_codex(
        codex_bin=args.codex_bin,
        codex_model=args.master_model,
        reasoning_effort=args.reasoning_effort,
        codex_args=list(args.codex_arg),
        prompt=prompt,
        timeout=args.codex_timeout,
        log_path=log_path,
        enable_search=False,
    )
    payload = extract_json_object(master_output)
    parsed = validate_payload_dict(payload, config_path=config_path)
    if parsed.topic != str(pool.get("topic") or "lab_automation"):
        raise ValueError("Master output topic differs from candidate pool")
    if parsed.cadence != str(pool.get("cadence") or "weekly"):
        raise ValueError("Master output cadence differs from candidate pool")
    if parsed.period != str(pool["period"]):
        raise ValueError("Master output period differs from candidate pool")
    if len(parsed.items) > max_items:
        raise ValueError(
            f"Master selected {len(parsed.items)} items; max is {max_items}"
        )

    allowed_exact_urls = {
        str(candidate.get("url") or "").strip()
        for candidate in candidates
    }
    selected_exact_urls = [
        str(item.url or "").strip() for item in parsed.items
    ]
    selected_comparison_urls = [
        comparison_url(item.url) for item in parsed.items
    ]
    outside_pool = [
        url
        for url in selected_exact_urls
        if not url or url not in allowed_exact_urls
    ]
    if outside_pool:
        raise ValueError(
            "Master selected URL(s) outside the fixed pool: "
            + ", ".join(outside_pool)
        )
    if len(selected_comparison_urls) != len(
        set(selected_comparison_urls)
    ):
        raise ValueError("Master output contains duplicate normalized URLs")

    write_json(output_path, payload)
    write_json(
        metadata_path,
        {
            "schema_version": 1,
            "master_model": args.master_model,
            "reasoning_effort": args.reasoning_effort,
            "candidate_seed": pool["candidate_seed"],
            "candidate_count": pool["candidate_count"],
            "candidate_order_digest": expected_digest,
            "selected_item_count": len(parsed.items),
            "candidate_pool_path": str(pool_path),
            "prompt_template_path": str(template_path),
            "prompt_path": str(prompt_path),
            "output_path": str(output_path),
            "codex_log_path": str(log_path),
        },
    )
    print(
        "Master evaluation completed: "
        f"model={args.master_model}, "
        f"reasoning={args.reasoning_effort}, "
        f"seed={pool['candidate_seed']}, "
        f"selected={len(parsed.items)}, output={output_path}",
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a paired fixed candidate pool or run one "
            "selection-only Master evaluation."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare-pool",
        help="Build one deterministic candidate ordering.",
    )
    prepare.add_argument(
        "--source-run",
        action="append",
        required=True,
        help=(
            "Source run stem or final JSON. Exactly two values are "
            "required."
        ),
    )
    prepare.add_argument("--period", required=True)
    prepare.add_argument("--seed", required=True, type=int)
    prepare.add_argument("--output", required=True)
    prepare.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH)
    )
    prepare.add_argument("--db", default=str(DEFAULT_DB_PATH))
    prepare.set_defaults(handler=prepare_pool)

    master = subparsers.add_parser(
        "run-master",
        help="Run one Master against a prepared candidate pool.",
    )
    master.add_argument("--candidate-pool", required=True)
    master.add_argument("--master-model", required=True)
    master.add_argument(
        "--reasoning-effort",
        required=True,
        choices=sorted(SUPPORTED_REASONING_EFFORTS),
    )
    master.add_argument("--output", required=True)
    master.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH)
    )
    master.add_argument("--max-items", type=int, default=30)
    master.add_argument("--codex-bin", default="codex")
    master.add_argument(
        "--codex-timeout",
        type=int,
        default=DEFAULT_CODEX_TIMEOUT_SECONDS,
    )
    master.add_argument(
        "--codex-arg",
        action="append",
        default=[],
    )
    master.set_defaults(handler=run_master)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
