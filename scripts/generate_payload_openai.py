from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsbot.config import DEFAULT_CONFIG_PATH, load_config, resolve_project_path
from newsbot.codex_env import codex_subprocess_env
from newsbot.db import DEFAULT_DB_PATH, NewsbotStore
from newsbot.feed_discovery import collect_feed_candidates, load_feed_sources
from newsbot.models import NewsPayload, canonicalize_url, normalize_title, sha256_text, stable_json, title_similarity
from newsbot.paper_api import (
    DEFAULT_API_CACHE_TTL_SECONDS,
    collect_paper_api_candidates,
    redact_sensitive_values,
)
from newsbot.ranking import REVIEW_MAX_ITEMS, rank_news_items


DEFAULT_CODEX_TIMEOUT_SECONDS = 90 * 60
DEFAULT_PHASE_4_LLM_BATCH_SIZE = 50
PROMPT_TEMPLATE_DIR = PROJECT_ROOT / "prompts"
LAB_AUTOMATION_TOPIC = "lab_automation"
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
PHASE_4_EXCLUSION_REASON_CODES = {
    "duplicate",
    "insufficient_directness",
    "insufficient_evidence",
    "lane_failure",
    "outside_period",
    "out_of_scope",
    "superseded_by_canonical_version",
    "unverifiable",
    "other",
}
DISCOVERY_AUDIT_PHASES = {
    "phase_1_domestic_official",
    "phase_2_pr_resolution",
    "phase_3_global_official",
    "phase_5_cross_phase_broad_sweep",
}
DISCOVERY_MODES = {"structured", "broad", "feed", "api"}
CONFIDENCE_LABEL_VALUES = {"low": 0.4, "medium": 0.65, "high": 0.85}
CONTENT_KINDS = {"news", "event_announcement", "event_occurrence", "post_event_report", "official_resurfacing"}
DATE_BASES = {"publication", "event", "report", "resurfacing"}
DEFAULT_FEEDBACK_LOOKBACK_WEEKS = 12
EVENT_SCHEMA_INSTRUCTIONS = """

## Mandatory content kind and date fields

Every candidate/item must include `content_kind` (`news`, `event_announcement`, `event_occurrence`, `post_event_report`, or `official_resurfacing`), canonical-page `published_date`, `event_date_start`, `event_date_end`, `date_basis` (`publication`, `event`, `report`, or `resurfacing`), and `original_publication_date`.
Never copy an event date into `published_date`. An important event occurrence may be evaluated even when its announcement publication is outside the normal period; use `content_kind=event_occurrence`, `date_basis=event`, retain the real canonical publication date (or empty if unverifiable), and supply `event_date_start`. Event announcements and substantive post-event reports are eligible. Mere exhibition attendance and generic seminars are excluded.
""".strip()
PHASE_CANDIDATE_LIMIT_INSTRUCTIONS = """
## Candidate count policy

This phase has no fixed candidate-count ceiling. Return every verified candidate that meets the phase criteria; do not truncate to 30 or to the final reviewer limit. Do not add weak candidates merely to increase the count. Only the final Master stage applies the item limit; Python enforces that same limit afterward as a defensive validation step.
""".strip()


@dataclass(frozen=True)
class PhaseDefinition:
    key: str
    phase: str
    label: str
    prompt_file: str
    broad_prompt_file: str = ""


def log_step(message: str) -> None:
    print(f"[newsbot-generate] {message}", flush=True)


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


def run_state_path(topic: str, cadence: str) -> Path:
    safe_topic = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in topic)
    safe_cadence = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in cadence)
    return PROJECT_ROOT / "data" / f"codex_last_run_{safe_topic}_{safe_cadence}.txt"


def read_last_run(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_last_run(path: Path, value: datetime | None = None) -> None:
    current = value or datetime.now()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(current.isoformat(timespec="seconds") + "\n", encoding="utf-8")


def default_period(cadence: str, now: datetime | None = None) -> str:
    current = now or datetime.now()
    if cadence == "daily":
        start = current - timedelta(days=1)
    elif cadence == "monthly":
        start = current - timedelta(days=30)
    else:
        start = current - timedelta(days=7)
    return f"{start:%Y-%m-%d} to {current:%Y-%m-%d} (JST)"


def resolve_period(
    *,
    topic: str,
    cadence: str,
    explicit_period: str = "",
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> str:
    if explicit_period.strip():
        return explicit_period.strip()
    current = now or datetime.now()
    if lookback_days is not None and lookback_days > 0:
        start = current - timedelta(days=lookback_days)
        return f"{start:%Y-%m-%d} to {current:%Y-%m-%d} (JST)"
    last_run = read_last_run(run_state_path(topic, cadence))
    if last_run is not None:
        return f"{last_run:%Y-%m-%d %H:%M} to {current:%Y-%m-%d %H:%M} (JST)"
    return default_period(cadence, current)


def default_output_path(topic: str, cadence: str, now: datetime | None = None) -> Path:
    current = now or datetime.now()
    return PROJECT_ROOT / "payloads" / f"{topic}_{cadence}_{current:%Y%m%d_%H%M%S_%f}.json"


def isolated_dry_run_db_path(output_path: Path) -> Path:
    return output_path.with_suffix(".isolated-dry-run.sqlite")


def create_isolated_dry_run_db(source_path: Path, output_path: Path) -> Path:
    """Snapshot the current DB so dry-run audit writes cannot touch production."""
    target_path = isolated_dry_run_db_path(output_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        raise ValueError(f"Isolated dry-run database already exists: {target_path}")
    if not source_path.exists():
        NewsbotStore(target_path).init()
        return target_path

    source_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn, sqlite3.connect(target_path) as target_conn:
        source_conn.backup(target_conn)
    target_path.chmod(0o600)
    return target_path


def safe_topic_name(topic: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in topic)


def prompt_template_path(topic: str, template_dir: Path = PROMPT_TEMPLATE_DIR) -> Path:
    return template_dir / f"{safe_topic_name(topic)}.md"


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Codex output was empty")
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    fenced = _extract_fenced_json(stripped)
    if fenced is not None:
        return fenced

    start = stripped.find("{")
    if start < 0:
        raise ValueError("Codex output did not contain a JSON object")

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start : index + 1]
                value = json.loads(candidate)
                if not isinstance(value, dict):
                    raise ValueError("Extracted JSON was not an object")
                return value
    raise ValueError("Could not find a complete JSON object in Codex output")


def _extract_fenced_json(text: str) -> dict[str, Any] | None:
    markers = ("```json", "```")
    for marker in markers:
        start = text.find(marker)
        if start < 0:
            continue
        body_start = start + len(marker)
        end = text.find("```", body_start)
        if end < 0:
            continue
        body = text[body_start:end].strip()
        if marker == "```" and body.lower().startswith("json"):
            body = body[4:].strip()
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def build_prompt(
    *,
    topic: str,
    cadence: str,
    period: str,
    max_items: int,
    output_path: Path,
    config_path: Path,
    preference_profile: dict[str, Any] | None = None,
) -> str:
    preference_text = format_preference_profile(preference_profile)
    return f"""
You are generating a Newsbot payload JSON for reviewer approval.

Search for recent, high-signal news for:
- topic: {topic}
- cadence: {cadence}
- period: {period}
- maximum items: {max_items}

Use the repository configuration at {config_path.as_posix()} as guidance for topic scope, source priority, language, and deduplication policy.

Audience preference profile from prior Discord `Interested` feedback:
{preference_text}

Return exactly one JSON object and no surrounding prose. Do not wrap it in markdown unless your interface forces it.

Required JSON shape:
{{
  "topic": "{topic}",
  "cadence": "{cadence}",
  "period": "{period}",
  "items": [
    {{
      "title": "...",
      "summary": "...",
      "source": "...",
      "url": "https://...",
      "category_primary": "...",
      "tags": ["..."],
      "importance_score": 0.0,
      "priority": "normal"
    }}
  ]
}}

Rules:
- Prefer official company announcements, investor relations, regulatory filings, official research institutes, trusted industry media, papers/preprints, and patent databases.
- Use the audience preference profile as a ranking signal, not as a hard filter. Keep high-impact news even if it is outside previous preferences.
- Set `importance_score` and `priority` so the most review-worthy items come first.
- Avoid duplicate coverage of the same event.
- Include only verifiable source URLs.
- Keep summaries concise and suitable for Discord reviewer drafts.
- Avoid investment advice and avoid unsupported medical claims.
- If fewer than {max_items} credible items are available, return fewer items.
- The JSON will be saved to {output_path.as_posix()} and then passed to `python -m newsbot.cli validate-payload`.
""".strip()


def render_prompt_template(
    template_text: str,
    *,
    topic: str,
    cadence: str,
    period: str,
    max_items: int,
    output_path: Path,
    config_path: Path,
    preference_profile: dict[str, Any] | None = None,
    extra_values: dict[str, str] | None = None,
) -> str:
    values = {
        "{{TOPIC}}": topic,
        "{{CADENCE}}": cadence,
        "{{PERIOD}}": period,
        "{{MAX_ITEMS}}": str(max_items),
        "{{OUTPUT_PATH}}": output_path.as_posix(),
        "{{CONFIG_PATH}}": config_path.as_posix(),
        "{{PREFERENCE_PROFILE}}": format_preference_profile(preference_profile),
    }
    if extra_values:
        values.update(extra_values)
    rendered = template_text
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered.strip()


def build_generation_prompt(
    *,
    topic: str,
    cadence: str,
    period: str,
    max_items: int,
    output_path: Path,
    config_path: Path,
    preference_profile: dict[str, Any] | None = None,
    template_dir: Path = PROMPT_TEMPLATE_DIR,
) -> tuple[str, Path | None]:
    template_path = prompt_template_path(topic, template_dir=template_dir)
    if template_path.exists():
        return (
            render_prompt_template(
                template_path.read_text(encoding="utf-8"),
                topic=topic,
                cadence=cadence,
                period=period,
                max_items=max_items,
                output_path=output_path,
                config_path=config_path,
                preference_profile=preference_profile,
            ),
            template_path,
        )
    return (
        build_prompt(
            topic=topic,
            cadence=cadence,
            period=period,
            max_items=max_items,
            output_path=output_path,
            config_path=config_path,
            preference_profile=preference_profile,
        ),
        None,
    )


def lab_automation_phase_definitions() -> list[PhaseDefinition]:
    return [
        PhaseDefinition(
            key="phase_1",
            phase="phase_1_domestic_official",
            label="Phase 1 Agent: 国内公式・研究基盤探索",
            prompt_file="phase_1_domestic_official.md",
            broad_prompt_file="phase_1_domestic_official_broad.md",
        ),
        PhaseDefinition(
            key="phase_2",
            phase="phase_2_pr_resolution",
            label="Phase 2 Agent: PR探索 + 公式URL解決",
            prompt_file="phase_2_pr_resolution.md",
            broad_prompt_file="phase_2_pr_resolution_broad.md",
        ),
        PhaseDefinition(
            key="phase_3",
            phase="phase_3_global_official",
            label="Phase 3 Agent: 海外公式・企業・標準化団体探索",
            prompt_file="phase_3_global_official.md",
            broad_prompt_file="phase_3_global_official_broad.md",
        ),
        PhaseDefinition(
            key="phase_4",
            phase="phase_4_papers",
            label="Phase 4 Agent: 論文・preprint探索",
            prompt_file="phase_4_papers.md",
            broad_prompt_file="phase_4_papers_broad.md",
        ),
        PhaseDefinition(
            key="phase_5",
            phase="phase_5_cross_phase_broad_sweep",
            label="Phase 5 Agent: Phase横断・独立広域探索",
            prompt_file="phase_5_cross_phase_broad_sweep.md",
        ),
    ]


def parse_phase_models(values: list[str]) -> dict[str, str]:
    return parse_phase_overrides(values, option_name="--phase-model")


def parse_phase_reasoning(values: list[str]) -> dict[str, str]:
    parsed = parse_phase_overrides(values, option_name="--phase-reasoning")
    for key, effort in parsed.items():
        validate_reasoning_effort(effort, label=f"{key} reasoning effort")
    return parsed


def parse_phase_overrides(values: list[str], *, option_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    valid_keys = {phase.key for phase in lab_automation_phase_definitions()}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{option_name} must be KEY=VALUE, got: {value}")
        key, override = (part.strip() for part in value.split("=", 1))
        if not key or not override:
            raise ValueError(f"{option_name} must include both key and value, got: {value}")
        if key not in valid_keys:
            raise ValueError(f"Unknown phase key: {key}; expected one of: {', '.join(sorted(valid_keys))}")
        parsed[key] = override
    return parsed


def default_codex_model(explicit_model: str) -> str:
    return explicit_model.strip() or os.getenv("NEWSBOT_CODEX_MODEL", "").strip()


def default_reasoning_effort(explicit_effort: str) -> str:
    effort = explicit_effort.strip() or os.getenv("NEWSBOT_CODEX_REASONING_EFFORT", "").strip()
    if effort:
        validate_reasoning_effort(effort, label="default reasoning effort")
    return effort


def validate_reasoning_effort(effort: str, *, label: str) -> None:
    if effort not in SUPPORTED_REASONING_EFFORTS:
        supported = ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
        raise ValueError(f"{label} must be one of: {supported}; got: {effort}")


def env_phase_value(phase: PhaseDefinition, suffix: str) -> str:
    env_key = f"NEWSBOT_LAB_AUTOMATION_{phase.key.upper()}_{suffix}"
    return os.getenv(env_key, "").strip()


def phase_model_for(phase: PhaseDefinition, phase_models: dict[str, str], default_model: str) -> str:
    return phase_models.get(phase.key) or env_phase_value(phase, "MODEL") or default_model


def master_model_for(master_model: str, default_model: str) -> str:
    return master_model.strip() or os.getenv("NEWSBOT_LAB_AUTOMATION_MASTER_MODEL", "").strip() or default_model


def phase_reasoning_for(phase: PhaseDefinition, phase_reasoning: dict[str, str], default_effort: str) -> str:
    effort = phase_reasoning.get(phase.key) or env_phase_value(phase, "REASONING_EFFORT") or default_effort
    if effort:
        validate_reasoning_effort(effort, label=f"{phase.key} reasoning effort")
    return effort


def master_reasoning_for(master_reasoning_effort: str, default_effort: str) -> str:
    effort = master_reasoning_effort.strip() or os.getenv("NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT", "").strip() or default_effort
    if effort:
        validate_reasoning_effort(effort, label="master reasoning effort")
    return effort


def paper_api_retmax(explicit_retmax: int | None) -> int:
    if explicit_retmax is not None:
        return max(1, explicit_retmax)
    env_value = os.getenv("NEWSBOT_PAPER_API_RETMAX", "").strip()
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError as exc:
            raise ValueError(f"NEWSBOT_PAPER_API_RETMAX must be an integer: {env_value}") from exc
    return 50


def phase_4_llm_batch_size(explicit_batch_size: int | None) -> int:
    if explicit_batch_size is not None:
        return max(1, explicit_batch_size)
    env_value = os.getenv("NEWSBOT_PHASE_4_LLM_BATCH_SIZE", "").strip()
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError as exc:
            raise ValueError(
                f"NEWSBOT_PHASE_4_LLM_BATCH_SIZE must be an integer: {env_value}"
            ) from exc
    return DEFAULT_PHASE_4_LLM_BATCH_SIZE


def feedback_lookback_weeks(explicit_value: int | None = None) -> int:
    """Resolve feedback window using future-compatible CLI > env > default precedence."""
    if explicit_value is not None:
        value: Any = explicit_value
        source = "CLI"
    else:
        raw = os.getenv("NEWSBOT_FEEDBACK_LOOKBACK_WEEKS", "").strip()
        if not raw:
            return DEFAULT_FEEDBACK_LOOKBACK_WEEKS
        value = raw
        source = "NEWSBOT_FEEDBACK_LOOKBACK_WEEKS"
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError(f"{source} must be an integer of 1 or greater; got: {value}")
    weeks = int(value)
    if weeks < 1:
        raise ValueError(f"{source} must be an integer of 1 or greater; got: {value}")
    return weeks


def lab_automation_prompt_dir(template_dir: Path = PROMPT_TEMPLATE_DIR) -> Path:
    return template_dir / LAB_AUTOMATION_TOPIC


def validate_phase_output(
    value: dict[str, Any],
    *,
    expected_phase: str,
    paper_api_candidates: list[dict[str, Any]] | None = None,
    discovery_lane: str | None = None,
) -> dict[str, Any]:
    if value.get("phase") != expected_phase:
        raise ValueError(f"Phase output must include phase={expected_phase!r}")
    if not str(value.get("period") or "").strip():
        raise ValueError(f"{expected_phase}: period is required")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError(f"{expected_phase}: candidates must be a list")
    coverage = value.get("search_coverage")
    if not isinstance(coverage, dict):
        raise ValueError(f"{expected_phase}: search_coverage must be an object")

    discovery_audit_required = (
        expected_phase in DISCOVERY_AUDIT_PHASES
        or discovery_lane in {"structured", "broad"}
    )
    if discovery_audit_required:
        failed_lanes = {
            str(item.get("lane") or "")
            for item in (coverage.get("lane_failures") or [])
            if isinstance(item, dict)
        }
        broad_queries = coverage.get("broad_queries_run", [])
        broad_lane_required = not (
            discovery_lane == "structured"
            or (discovery_lane is None and "broad" in failed_lanes)
        )
        valid_broad_queries = (
            isinstance(broad_queries, list)
            and len(broad_queries) >= 3
            and all(isinstance(query, str) and query.strip() for query in broad_queries)
        )
        if broad_lane_required and not valid_broad_queries:
            raise ValueError(
                f"{expected_phase}: search_coverage.broad_queries_run must be a JSON array "
                "with at least three queries; every query must be a non-empty string"
            )
        if expected_phase != "phase_5_cross_phase_broad_sweep" and discovery_lane != "broad":
            structured_queries = coverage.get("structured_queries_run")
            if not isinstance(structured_queries, list):
                raise ValueError(
                    f"{expected_phase}: search_coverage.structured_queries_run must be a list"
                )
        required_counts = ["merged_candidate_count"]
        if discovery_lane == "structured":
            required_counts.append("structured_candidate_count")
        elif discovery_lane == "broad" or expected_phase == "phase_5_cross_phase_broad_sweep":
            required_counts.append("broad_candidate_count")
        else:
            required_counts.extend(["structured_candidate_count", "broad_candidate_count"])
        for count_field in required_counts:
            count = coverage.get(count_field)
            if not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"{expected_phase}: search_coverage.{count_field} must be a non-negative integer"
                )
        merged_count = coverage["merged_candidate_count"]
        if merged_count != len(candidates):
            raise ValueError(
                f"{expected_phase}: search_coverage.merged_candidate_count must equal candidates length"
            )
        broad_count = int(coverage.get("broad_candidate_count") or 0)
        if expected_phase == "phase_5_cross_phase_broad_sweep" or discovery_lane == "broad":
            if broad_count < merged_count:
                raise ValueError(
                    f"{expected_phase}: broad_candidate_count cannot be smaller than merged_candidate_count"
                )
        elif discovery_lane != "structured" and coverage["structured_candidate_count"] + broad_count < merged_count:
            raise ValueError(
                f"{expected_phase}: lane candidate counts cannot be smaller than merged_candidate_count"
            )

    required_candidate_fields = {
        "title",
        "source",
        "url",
        "published_date",
        "source_type",
        "geography",
        "lab_automation_relevance",
        "evidence",
        "confidence",
        "canonical_source_checked",
        "duplicate_key",
    }
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"{expected_phase}: candidate {index} must be an object")
        missing = sorted(field for field in required_candidate_fields if field not in candidate)
        if missing:
            raise ValueError(f"{expected_phase}: candidate {index} missing fields: {', '.join(missing)}")
        try:
            confidence = float(candidate.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{expected_phase}: candidate {index} confidence must be numeric") from exc
        if confidence < 0 or confidence > 1:
            raise ValueError(f"{expected_phase}: candidate {index} confidence must be between 0.0 and 1.0")
        if not isinstance(candidate.get("canonical_source_checked"), bool):
            raise ValueError(f"{expected_phase}: candidate {index} canonical_source_checked must be boolean")
        if discovery_audit_required:
            discovery_mode = str(candidate.get("discovery_mode") or "").strip()
            discovery_modes = candidate.get("discovery_modes")
            if discovery_mode not in DISCOVERY_MODES:
                raise ValueError(
                    f"{expected_phase}: candidate {index} discovery_mode must be structured, broad, feed, or api"
                )
            if (
                not isinstance(discovery_modes, list)
                or not discovery_modes
                or any(mode not in DISCOVERY_MODES for mode in discovery_modes)
                or discovery_mode not in discovery_modes
            ):
                raise ValueError(
                    f"{expected_phase}: candidate {index} discovery_modes must include the primary discovery_mode"
                )
            candidate["discovery_modes"] = list(dict.fromkeys(discovery_modes))
            allowed_for_lane = {
                "structured": {"structured", "feed"},
                "broad": {"broad"},
            }.get(discovery_lane or "", DISCOVERY_MODES)
            if any(mode not in allowed_for_lane for mode in candidate["discovery_modes"]):
                raise ValueError(
                    f"{expected_phase}: candidate {index} contains a discovery mode from another isolated lane"
                )

        content_kind = str(candidate.get("content_kind") or "news").strip()
        if content_kind not in CONTENT_KINDS:
            raise ValueError(f"{expected_phase}: candidate {index} has invalid content_kind: {content_kind}")
        candidate["content_kind"] = content_kind
        default_basis = {
            "event_occurrence": "event",
            "post_event_report": "report",
            "official_resurfacing": "resurfacing",
        }.get(content_kind, "publication")
        date_basis = str(candidate.get("date_basis") or default_basis).strip()
        if date_basis not in DATE_BASES:
            raise ValueError(f"{expected_phase}: candidate {index} has invalid date_basis: {date_basis}")
        candidate["date_basis"] = date_basis
        candidate.setdefault("event_date_start", "")
        candidate.setdefault("event_date_end", "")
        candidate.setdefault("original_publication_date", "")
        if content_kind == "event_occurrence" and not str(candidate.get("event_date_start") or "").strip():
            raise ValueError(f"{expected_phase}: event_occurrence candidate {index} requires event_date_start")

    if expected_phase == "phase_4_papers" and paper_api_candidates is not None:
        expected_by_id = {
            str(candidate.get("candidate_id") or ""): candidate
            for candidate in paper_api_candidates
        }
        if "" in expected_by_id or len(expected_by_id) != len(paper_api_candidates):
            raise ValueError("phase_4_papers: paper API candidate IDs must be present and unique")

        selected_ids: list[str] = []
        for index, candidate in enumerate(candidates):
            ids = candidate.get("paper_api_candidate_ids", [])
            modes = set(candidate.get("discovery_modes") or [])
            broad_only = modes == {"broad"}
            if (
                not isinstance(ids, list)
                or (not ids and not broad_only)
                or not all(isinstance(item, str) and item for item in ids)
            ):
                raise ValueError(
                    f"phase_4_papers: API-lane candidate {index} paper_api_candidate_ids "
                    "must be a non-empty list of strings"
                )
            selected_ids.extend(ids)
            if ids and not modes:
                candidate["discovery_mode"] = "api"
                candidate["discovery_modes"] = ["api"]

        excluded = value.get("excluded_api_candidates")
        if not isinstance(excluded, list):
            raise ValueError("phase_4_papers: excluded_api_candidates must be a list")
        excluded_by_id: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(excluded):
            if not isinstance(item, dict):
                raise ValueError(f"phase_4_papers: excluded candidate {index} must be an object")
            candidate_id = str(item.get("candidate_id") or "").strip()
            reason_code = str(item.get("reason_code") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not candidate_id:
                raise ValueError(f"phase_4_papers: excluded candidate {index} candidate_id is required")
            if reason_code not in PHASE_4_EXCLUSION_REASON_CODES:
                allowed = ", ".join(sorted(PHASE_4_EXCLUSION_REASON_CODES))
                raise ValueError(
                    f"phase_4_papers: excluded candidate {index} reason_code must be one of: {allowed}"
                )
            if not reason:
                raise ValueError(f"phase_4_papers: excluded candidate {index} reason is required")
            if candidate_id in excluded_by_id:
                raise ValueError(f"phase_4_papers: duplicate excluded candidate ID: {candidate_id}")
            excluded_by_id[candidate_id] = item

        selected_set = set(selected_ids)
        excluded_set = set(excluded_by_id)
        expected_set = set(expected_by_id)
        duplicate_selected = sorted(candidate_id for candidate_id in selected_set if selected_ids.count(candidate_id) > 1)
        overlap = sorted(selected_set & excluded_set)
        unknown = sorted((selected_set | excluded_set) - expected_set)
        missing = sorted(expected_set - selected_set - excluded_set)
        if duplicate_selected:
            raise ValueError(f"phase_4_papers: API candidate selected more than once: {', '.join(duplicate_selected)}")
        if overlap:
            raise ValueError(f"phase_4_papers: API candidate both selected and excluded: {', '.join(overlap)}")
        if unknown:
            raise ValueError(f"phase_4_papers: unknown API candidate IDs: {', '.join(unknown)}")
        if missing:
            raise ValueError(f"phase_4_papers: API candidates missing a disposition: {', '.join(missing)}")

        value["excluded_api_candidates"] = [
            {
                "candidate_id": candidate_id,
                "title": expected_by_id[candidate_id].get("title", ""),
                "source": expected_by_id[candidate_id].get("source", ""),
                "url": expected_by_id[candidate_id].get("url", ""),
                "published_date": expected_by_id[candidate_id].get("published_date", ""),
                "reason_code": excluded_by_id[candidate_id]["reason_code"],
                "reason": excluded_by_id[candidate_id]["reason"],
            }
            for candidate_id in expected_by_id
            if candidate_id in excluded_by_id
        ]
        value["paper_api_audit"] = {
            "input_candidate_count": len(expected_set),
            "selected_candidate_count": len(selected_set),
            "excluded_candidate_count": len(excluded_set),
            "unaccounted_candidate_count": 0,
        }
    return value


def normalize_discovery_lane_output(
    value: dict[str, Any],
    lane: str,
    *,
    web_search_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Repair narrow, unambiguous schema slips while retaining an audit trail."""
    if lane not in {"structured", "broad"}:
        raise ValueError(f"Unsupported discovery lane normalization: {lane}")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        return value
    normalizations: list[dict[str, Any]] = []
    coverage = value.get("search_coverage")
    if lane == "broad" and isinstance(coverage, dict):
        reported_queries = coverage.get("broad_queries_run")
        valid_reported_queries = (
            isinstance(reported_queries, list)
            and len(reported_queries) >= 3
            and all(isinstance(query, str) and query.strip() for query in reported_queries)
        )
        if not valid_reported_queries:
            recovered_queries = unique_nonempty_strings(web_search_queries or [])
            recovery_source = "codex_web_search_events"
            if len(recovered_queries) < 3:
                recovered_queries = unique_nonempty_strings(
                    coverage.get("broad_query_examples") or []
                )
                recovery_source = "broad_query_examples"
            if len(recovered_queries) >= 3:
                if isinstance(reported_queries, (int, float)) and not isinstance(
                    reported_queries, bool
                ):
                    coverage.setdefault(
                        "broad_query_count_reported", int(reported_queries)
                    )
                coverage["broad_queries_run"] = recovered_queries
                normalizations.append(
                    {
                        "field": "search_coverage.broad_queries_run",
                        "from": reported_queries,
                        "to": recovered_queries,
                        "source": recovery_source,
                    }
                )
    allowed_modes = {"structured", "feed"} if lane == "structured" else {"broad"}
    default_mode = "broad" if lane == "broad" else "structured"
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        raw_mode = str(candidate.get("discovery_mode") or "").strip()
        raw_modes = candidate.get("discovery_modes")
        valid_modes = (
            [str(mode) for mode in raw_modes if str(mode) in allowed_modes]
            if isinstance(raw_modes, list)
            else []
        )
        if raw_mode not in allowed_modes:
            candidate["discovery_mode"] = valid_modes[0] if valid_modes else default_mode
            normalizations.append(
                {
                    "candidate_index": index,
                    "field": "discovery_mode",
                    "from": raw_mode or None,
                    "to": candidate["discovery_mode"],
                }
            )
        primary_mode = str(candidate.get("discovery_mode") or default_mode)
        if primary_mode not in valid_modes:
            valid_modes.insert(0, primary_mode)
        normalized_modes = list(dict.fromkeys(valid_modes))
        if raw_modes != normalized_modes:
            candidate["discovery_modes"] = normalized_modes
            normalizations.append(
                {
                    "candidate_index": index,
                    "field": "discovery_modes",
                    "from": raw_modes,
                    "to": normalized_modes,
                }
            )
        confidence = candidate.get("confidence")
        if isinstance(confidence, str) and confidence.strip().casefold() in CONFIDENCE_LABEL_VALUES:
            numeric_confidence = CONFIDENCE_LABEL_VALUES[confidence.strip().casefold()]
            candidate["confidence"] = numeric_confidence
            normalizations.append(
                {
                    "candidate_index": index,
                    "field": "confidence",
                    "from": confidence,
                    "to": numeric_confidence,
                }
            )
    if normalizations:
        if isinstance(coverage, dict):
            coverage.setdefault("output_normalizations", []).extend(normalizations)
    return value


def unique_nonempty_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def discovery_retry_instructions(lane: str, error: str) -> str:
    instructions = [
        "## Output repair required",
        f"The previous attempt failed schema validation: {error}",
        "Return the complete result again as one valid JSON object. Follow every field type in the original schema exactly.",
    ]
    if lane == "broad":
        instructions.append(
            '`search_coverage.broad_queries_run` must be a JSON array containing the literal '
            'web-search query strings, for example `["query 1", "query 2", "query 3"]`. '
            "Never replace this array with a numeric query count."
        )
    return "\n".join(instructions)


def build_phase_prompt(
    *,
    phase: PhaseDefinition,
    topic: str,
    cadence: str,
    period: str,
    max_items: int,
    output_path: Path,
    config_path: Path,
    preference_profile: dict[str, Any] | None,
    extra_values: dict[str, str] | None = None,
    prompt_file: str | None = None,
    template_dir: Path = PROMPT_TEMPLATE_DIR,
) -> tuple[str, Path]:
    template_path = lab_automation_prompt_dir(template_dir) / (prompt_file or phase.prompt_file)
    if not template_path.exists():
        raise FileNotFoundError(f"Missing phase prompt template: {template_path}")
    phase_values = {
        "{{PHASE_KEY}}": phase.key,
        "{{PHASE_NAME}}": phase.phase,
        "{{PHASE_LABEL}}": phase.label,
        "{{PAPER_API_CANDIDATES_JSON}}": "[]",
        "{{PAPER_API_COVERAGE_JSON}}": "{}",
        "{{FEED_CANDIDATES_JSON}}": "[]",
    }
    if extra_values:
        phase_values.update(extra_values)
    prompt = render_prompt_template(
        template_path.read_text(encoding="utf-8"),
        topic=topic,
        cadence=cadence,
        period=period,
        max_items=max_items,
        output_path=output_path,
        config_path=config_path,
        preference_profile=preference_profile,
        extra_values=phase_values,
    )
    return (
        prompt
        + "\n\n"
        + PHASE_CANDIDATE_LIMIT_INSTRUCTIONS
        + "\n\n"
        + EVENT_SCHEMA_INSTRUCTIONS,
        template_path,
    )


def build_master_prompt(
    *,
    topic: str,
    cadence: str,
    period: str,
    max_items: int,
    output_path: Path,
    config_path: Path,
    preference_profile: dict[str, Any] | None,
    phase_outputs: list[dict[str, Any]],
    config_raw: dict[str, Any],
    template_dir: Path = PROMPT_TEMPLATE_DIR,
) -> tuple[str, Path]:
    template_path = lab_automation_prompt_dir(template_dir) / "master_builder.md"
    if not template_path.exists():
        raise FileNotFoundError(f"Missing master prompt template: {template_path}")
    prompt = render_prompt_template(
        template_path.read_text(encoding="utf-8"),
        topic=topic,
        cadence=cadence,
        period=period,
        max_items=max_items,
        output_path=output_path,
        config_path=config_path,
        preference_profile=preference_profile,
        extra_values={
            "{{PHASE_OUTPUTS_JSON}}": json.dumps(phase_outputs, ensure_ascii=False, indent=2),
            "{{SOURCE_PRIORITY_JSON}}": json.dumps(config_raw.get("source_priority", []), ensure_ascii=False, indent=2),
            "{{CONFIG_JSON}}": json.dumps(config_raw, ensure_ascii=False, indent=2),
        },
    )
    return prompt + "\n\n" + EVENT_SCHEMA_INSTRUCTIONS.replace("candidate/item", "item"), template_path


def phase_artifact_path(output_path: Path, phase_key: str, suffix: str) -> Path:
    return output_path.with_suffix(f".{phase_key}{suffix}")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _candidate_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_url = canonicalize_url(str(left.get("url") or ""))
    right_url = canonicalize_url(str(right.get("url") or ""))
    if left_url and left_url == right_url:
        return True
    left_key = normalize_title(str(left.get("duplicate_key") or ""))
    right_key = normalize_title(str(right.get("duplicate_key") or ""))
    if left_key and left_key == right_key:
        return True
    return title_similarity(str(left.get("title") or ""), str(right.get("title") or "")) >= 0.82


def merge_discovery_lane_outputs(
    structured: dict[str, Any] | None,
    broad: dict[str, Any] | None,
    *, expected_phase: str,
    lane_failures: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if structured is None and broad is None:
        raise ValueError(f"{expected_phase}: structured and broad lanes both failed")
    candidates: list[dict[str, Any]] = []
    for lane_output in (structured, broad):
        if lane_output is None:
            continue
        for raw in lane_output.get("candidates") or []:
            candidate = dict(raw)
            existing = next((item for item in candidates if _candidate_match(item, candidate)), None)
            if existing is None:
                candidate["discovery_modes"] = list(dict.fromkeys(candidate.get("discovery_modes") or [candidate.get("discovery_mode")]))
                candidates.append(candidate)
                continue
            modes = list(existing.get("discovery_modes") or []) + list(candidate.get("discovery_modes") or [])
            existing["discovery_modes"] = list(dict.fromkeys(mode for mode in modes if mode))
            if float(candidate.get("confidence") or 0) > float(existing.get("confidence") or 0):
                preserved_modes = existing["discovery_modes"]
                existing.update(candidate)
                existing["discovery_modes"] = preserved_modes

    structured_coverage = (structured or {}).get("search_coverage") or {}
    broad_coverage = (broad or {}).get("search_coverage") or {}
    limitations = [str(item).strip() for item in (structured_coverage.get("limitations"), broad_coverage.get("limitations")) if str(item or "").strip()]
    coverage = {
        "queries_run": list(structured_coverage.get("queries_run") or structured_coverage.get("structured_queries_run") or []) + list(broad_coverage.get("queries_run") or broad_coverage.get("broad_queries_run") or []),
        "structured_queries_run": list(structured_coverage.get("structured_queries_run") or structured_coverage.get("queries_run") or []),
        "broad_queries_run": list(broad_coverage.get("broad_queries_run") or broad_coverage.get("queries_run") or []),
        "structured_candidate_count": len((structured or {}).get("candidates") or []),
        "broad_candidate_count": len((broad or {}).get("candidates") or []),
        "feed_candidate_count": sum("feed" in (item.get("discovery_modes") or []) for item in (structured or {}).get("candidates") or []),
        "merged_candidate_count": len(candidates),
        "notable_zero_result_queries": list(structured_coverage.get("notable_zero_result_queries") or []) + list(broad_coverage.get("notable_zero_result_queries") or []),
        "limitations": " | ".join(dict.fromkeys(limitations)),
        "lane_failures": lane_failures or [],
    }
    value = {"phase": expected_phase, "period": (structured or broad or {}).get("period"), "candidates": candidates, "search_coverage": coverage}
    return validate_phase_output(value, expected_phase=expected_phase)


def discovery_lane_counts(phase_outputs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "structured_only": 0,
        "api_only": 0,
        "broad_only": 0,
        "feed_only": 0,
        "multiple_lanes": 0,
    }
    for phase in phase_outputs:
        for candidate in phase.get("candidates") or []:
            modes = set(candidate.get("discovery_modes") or [])
            if len(modes) > 1:
                counts["multiple_lanes"] += 1
            elif modes == {"structured"}:
                counts["structured_only"] += 1
            elif modes == {"api"}:
                counts["api_only"] += 1
            elif modes == {"broad"}:
                counts["broad_only"] += 1
            elif modes == {"feed"}:
                counts["feed_only"] += 1
    return counts


def selected_discovery_metrics(phase_outputs: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    lane_counts = {"structured": 0, "api": 0, "broad": 0, "feed": 0}
    for item in items:
        matches = [candidate for phase in phase_outputs for candidate in phase.get("candidates") or [] if _candidate_match(candidate, item)]
        modes = {mode for candidate in matches for mode in candidate.get("discovery_modes") or []}
        for mode in modes:
            if mode in lane_counts:
                lane_counts[mode] += 1
    phase5 = next((phase for phase in phase_outputs if phase.get("phase") == "phase_5_cross_phase_broad_sweep"), {"candidates": []})
    other = [candidate for phase in phase_outputs if phase.get("phase") != "phase_5_cross_phase_broad_sweep" for candidate in phase.get("candidates") or []]
    phase5_unique = [candidate for candidate in phase5.get("candidates") or [] if not any(_candidate_match(candidate, candidate_other) for candidate_other in other)]
    phase5_selected = sum(any(_candidate_match(candidate, item) for item in items) for candidate in phase5_unique)
    return {"master_selected_by_lane": lane_counts, "phase_5_unique_candidate_count": len(phase5_unique), "phase_5_unique_master_selected_count": phase5_selected}


def enrich_master_items_from_candidates(payload: dict[str, Any], phase_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    for item in payload.get("items") or []:
        matches = [(phase, candidate) for phase in phase_outputs for candidate in phase.get("candidates") or [] if _candidate_match(candidate, item)]
        source = max(matches, key=lambda pair: float(pair[1].get("confidence") or 0), default=(None, None))
        phase, candidate = source
        if candidate:
            for field in ("content_kind", "published_date", "event_date_start", "event_date_end", "date_basis", "original_publication_date", "discovery_mode", "discovery_modes", "duplicate_key"):
                if not item.get(field) and candidate.get(field) not in (None, ""):
                    item[field] = candidate[field]
            item.setdefault("origin_phase", phase.get("phase"))
        item.setdefault("content_kind", "news")
        item.setdefault("published_date", "")
        item.setdefault("event_date_start", "")
        item.setdefault("event_date_end", "")
        item.setdefault("date_basis", "publication")
        item.setdefault("original_publication_date", "")
        if item["content_kind"] not in CONTENT_KINDS or item["date_basis"] not in DATE_BASES:
            raise ValueError(f"Master item has invalid content/date classification: {item.get('title', '')}")
        if item["content_kind"] == "event_occurrence" and not item["event_date_start"]:
            raise ValueError(f"Master event_occurrence item requires event_date_start: {item.get('title', '')}")
    return payload


def aggregate_codex_usage(log_paths: list[Path]) -> dict[str, int]:
    total: dict[str, int] = {}
    for log_path in log_paths:
        path = codex_sibling_artifact_path(log_path, ".codex.usage.json")
        if not path.exists():
            continue
        try:
            usage = json.loads(path.read_text(encoding="utf-8")).get("usage") or {}
        except (OSError, json.JSONDecodeError):
            continue
        for key, value in usage.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def format_preference_profile(profile: dict[str, Any] | None) -> str:
    if profile and ("audience" in profile or "editorial" in profile):
        audience = profile.get("audience") or {}
        editorial = profile.get("editorial") or {}
        lines = ["Audience Interested signal (separate from editorial decisions):", format_preference_profile(audience)]
        lines.extend([
            "Editorial decisions (negative feedback is context, never an automatic hard exclusion):",
            f"- lookback: {editorial.get('lookback_weeks', 12)} weeks ({editorial.get('window_start', '')} to {editorial.get('window_end', '')})",
            f"- judgments: {editorial.get('judgment_count', 0)}; decisions={editorial.get('decisions', {})}; reasons={editorial.get('reason_codes', {})}",
            f"- dynamic watchlist from missed items: {', '.join(editorial.get('dynamic_watchlist') or []) or '(none)'}",
            f"- positive auxiliary terms: {', '.join(editorial.get('positive_terms') or []) or '(none)'}",
            "- examples: " + json.dumps(editorial.get("examples") or [], ensure_ascii=False),
        ])
        return "\n".join(lines)
    if not profile or not profile.get("total_feedback"):
        return "- No historical Interested feedback yet. Rank by newsworthiness, source quality, and topic fit."

    lines = [f"- total Interested feedback events considered: {profile.get('total_feedback', 0)}"]
    for label, key in (("preferred categories", "categories"), ("preferred tags", "tags"), ("preferred sources", "sources")):
        values = profile.get(key) or {}
        if values:
            formatted = ", ".join(f"{name} ({count})" for name, count in values.items())
            lines.append(f"- {label}: {formatted}")
    examples = profile.get("examples") or []
    if examples:
        lines.append("- strongest historical examples:")
        for example in examples[:5]:
            lines.append(
                "  - "
                + f"{example.get('title', '')} "
                + f"[Interested {example.get('interested_count', 0)}; "
                + f"category={example.get('category', '')}; "
                + f"tags={', '.join(example.get('tags') or [])}]"
            )
    return "\n".join(lines)


def broad_lane_preference_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep editorial outcomes while withholding watchlists/search terms from broad discovery."""
    if not profile or "editorial" not in profile:
        return profile
    copied = json.loads(json.dumps(profile))
    copied["editorial"]["dynamic_watchlist"] = []
    copied["editorial"]["positive_terms"] = []
    return copied


def preference_profile_for_phase(profile: dict[str, Any] | None, phase: PhaseDefinition) -> dict[str, Any] | None:
    if not profile or "editorial" not in profile:
        return profile
    copied = json.loads(json.dumps(profile))
    by_phase = copied["editorial"].get("dynamic_watchlist_by_phase") or {}
    copied["editorial"]["dynamic_watchlist"] = list(by_phase.get(phase.key) or by_phase.get(phase.phase) or [])
    return copied


def run_codex(
    *,
    codex_bin: str,
    codex_model: str,
    reasoning_effort: str,
    codex_args: list[str],
    prompt: str,
    timeout: int,
    log_path: Path,
    enable_search: bool = True,
) -> str:
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".md") as handle:
        output_last_message = Path(handle.name)

    model_args = ["--model", codex_model] if codex_model else []
    reasoning_args = ["--config", f'model_reasoning_effort="{reasoning_effort}"'] if reasoning_effort else []
    exec_args = [arg for arg in codex_args if arg not in {"--search", "--json"}]
    search_args = ["--search"] if enable_search else []
    command = [
        codex_bin,
        *search_args,
        *reasoning_args,
        "exec",
        "--json",
        "--ephemeral",
        "--output-last-message",
        str(output_last_message),
        *model_args,
        *exec_args,
        "-",
    ]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=codex_subprocess_env(),
    )
    events_path = codex_sibling_artifact_path(log_path, ".codex.events.jsonl")
    usage_path = codex_sibling_artifact_path(log_path, ".codex.usage.json")
    events, usage, parse_errors = parse_codex_jsonl(result.stdout)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(result.stdout, encoding="utf-8")
    write_json(
        usage_path,
        {
            "model": codex_model or None,
            "reasoning_effort": reasoning_effort or None,
            "exit_code": result.returncode,
            "complete": result.returncode == 0 and usage is not None,
            "event_count": len(events),
            "parse_error_count": parse_errors,
            "usage": usage,
            "events_path": str(events_path),
        },
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "COMMAND:",
                " ".join(command[:-1] + ["<prompt-from-stdin>"]),
                "",
                f"EXIT_CODE: {result.returncode}",
                "",
                f"JSONL_EVENTS_PATH: {events_path}",
                f"USAGE_SUMMARY_PATH: {usage_path}",
                "",
                "STDOUT:",
                result.stdout,
                "",
                "STDERR:",
                result.stderr,
            ]
        ),
        encoding="utf-8",
    )
    output = output_last_message.read_text(encoding="utf-8", errors="replace").strip()
    output_last_message.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"codex exec failed with exit code {result.returncode}; see {log_path}")
    return output or final_agent_message(events) or result.stdout


def codex_sibling_artifact_path(log_path: Path, suffix: str) -> Path:
    marker = ".codex.log"
    name = log_path.name
    base = name[: -len(marker)] if name.endswith(marker) else log_path.stem
    return log_path.with_name(base + suffix)


def publish_codex_attempt_alias(attempt_log_path: Path, canonical_log_path: Path) -> None:
    """Keep the successful attempt artifacts at both audit and legacy paths."""
    for suffix in (".codex.log", ".codex.events.jsonl", ".codex.usage.json"):
        source = (
            attempt_log_path
            if suffix == ".codex.log"
            else codex_sibling_artifact_path(attempt_log_path, suffix)
        )
        target = (
            canonical_log_path
            if suffix == ".codex.log"
            else codex_sibling_artifact_path(canonical_log_path, suffix)
        )
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def parse_codex_jsonl(text: str) -> tuple[list[dict[str, Any]], dict[str, int] | None, int]:
    events: list[dict[str, Any]] = []
    parse_errors = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            parse_errors += 1

    completed_usage = [
        event.get("usage")
        for event in events
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)
    ]
    if not completed_usage:
        return events, None, parse_errors

    usage_keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    usage = {
        key: sum(int(item.get(key) or 0) for item in completed_usage)
        for key in usage_keys
    }
    usage["non_cached_input_tokens"] = max(
        usage["input_tokens"] - usage["cached_input_tokens"], 0
    )
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return events, usage, parse_errors


def codex_web_search_queries(events_path: Path) -> list[str]:
    if not events_path.exists():
        return []
    try:
        events, _, _ = parse_codex_jsonl(events_path.read_text(encoding="utf-8"))
    except OSError:
        return []
    queries: list[str] = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "web_search"
            and isinstance(item.get("query"), str)
        ):
            queries.append(item["query"])
    return unique_nonempty_strings(queries)


def final_agent_message(events: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
    return messages[-1].strip() if messages else ""


def validate_payload_dict(payload: dict[str, Any], *, config_path: Path) -> NewsPayload:
    parsed = NewsPayload.from_dict(payload)
    if not parsed.topic:
        raise ValueError("Payload must include topic")
    if not parsed.cadence:
        raise ValueError("Payload must include cadence")
    config = load_config(config_path)
    if not config.has_topic_cadence(parsed.topic, parsed.cadence):
        raise ValueError(f"Unknown topic/cadence: {parsed.topic}/{parsed.cadence}")
    if len(parsed.items) > config.max_items:
        raise ValueError(f"Payload contains {len(parsed.items)} items; max is {config.max_items}")
    return parsed


def select_review_items(
    payload: dict[str, Any],
    *,
    preference_profile: dict[str, Any] | None,
    limit: int,
) -> dict[str, Any]:
    parsed = NewsPayload.from_dict(payload)
    selected = rank_news_items(parsed.items, preference_profile=preference_profile, limit=limit)
    updated = dict(payload)
    updated["items"] = [item.raw or {} for item in selected]
    return updated


def run_newsbot_command(args: list[str]) -> int:
    command = [sys.executable, "-m", "newsbot.cli", *args]
    log_step("running: " + " ".join(command))
    completed = subprocess.run(command, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, check=False)
    log_step(f"command exited with code {completed.returncode}")
    return completed.returncode


def paper_api_candidate_id(candidate: dict[str, Any]) -> str:
    identity = str(
        candidate.get("doi")
        or candidate.get("url")
        or candidate.get("title")
        or ""
    ).strip().lower()
    return "paper-api-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def add_paper_api_candidate_ids(result: dict[str, Any]) -> dict[str, Any]:
    for candidate in result.get("candidates") or []:
        candidate["candidate_id"] = paper_api_candidate_id(candidate)
    return result


def split_paper_api_result_for_llm(
    result: dict[str, Any], batch_size: int
) -> list[dict[str, Any]]:
    candidates = list(result.get("candidates") or [])
    if not candidates:
        return [result]
    size = max(1, batch_size)
    batch_count = (len(candidates) + size - 1) // size
    batches: list[dict[str, Any]] = []
    for batch_index, offset in enumerate(range(0, len(candidates), size), start=1):
        batch = dict(result)
        batch["candidates"] = candidates[offset : offset + size]
        coverage = dict(result.get("coverage") or {})
        coverage["llm_batch"] = {
            "batch_index": batch_index,
            "batch_count": batch_count,
            "batch_size": len(batch["candidates"]),
            "total_llm_candidate_count": len(candidates),
        }
        batch["coverage"] = coverage
        batches.append(batch)
    return batches


def merge_phase_4_batch_outputs(
    outputs: list[dict[str, Any]],
    *,
    paper_api_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("phase_4_papers: at least one LLM batch output is required")

    merged_candidates: list[dict[str, Any]] = []
    candidate_by_key: dict[str, dict[str, Any]] = {}
    merged_exclusions: list[dict[str, Any]] = []
    merged_coverage: dict[str, Any] = {
        "queries_run": [],
        "notable_zero_result_queries": [],
        "limitations": "",
        "llm_batch_count": len(outputs),
    }
    limitations: list[str] = []

    for output in outputs:
        for candidate in output.get("candidates") or []:
            key = str(
                candidate.get("duplicate_key")
                or candidate.get("url")
                or candidate.get("title")
                or ""
            ).strip().lower()
            existing = candidate_by_key.get(key)
            if existing is None:
                copied = dict(candidate)
                copied["paper_api_candidate_ids"] = list(
                    dict.fromkeys(copied.get("paper_api_candidate_ids") or [])
                )
                candidate_by_key[key] = copied
                merged_candidates.append(copied)
            else:
                existing["paper_api_candidate_ids"] = list(
                    dict.fromkeys(
                        list(existing.get("paper_api_candidate_ids") or [])
                        + list(candidate.get("paper_api_candidate_ids") or [])
                    )
                )
                if float(candidate.get("confidence") or 0) > float(
                    existing.get("confidence") or 0
                ):
                    preserved_ids = existing["paper_api_candidate_ids"]
                    existing.update(candidate)
                    existing["paper_api_candidate_ids"] = preserved_ids

        merged_exclusions.extend(output.get("excluded_api_candidates") or [])
        coverage = output.get("search_coverage") or {}
        for key, value in coverage.items():
            if key == "limitations":
                text = str(value or "").strip()
                if text and text not in limitations:
                    limitations.append(text)
            elif isinstance(value, list):
                target = merged_coverage.setdefault(key, [])
                if isinstance(target, list):
                    for item in value:
                        if item not in target:
                            target.append(item)
            elif key not in merged_coverage:
                merged_coverage[key] = value

    selected_ids = {
        candidate_id
        for candidate in merged_candidates
        for candidate_id in candidate.get("paper_api_candidate_ids") or []
    }
    merged_exclusions = [
        exclusion
        for exclusion in merged_exclusions
        if exclusion.get("candidate_id") not in selected_ids
    ]
    merged_coverage["limitations"] = " | ".join(limitations)
    merged = {
        "phase": "phase_4_papers",
        "period": outputs[0].get("period"),
        "candidates": merged_candidates,
        "excluded_api_candidates": merged_exclusions,
        "search_coverage": merged_coverage,
    }
    return validate_phase_output(
        merged,
        expected_phase="phase_4_papers",
        paper_api_candidates=paper_api_candidates,
    )


def merge_phase_4_lane_outputs(
    api_output: dict[str, Any] | None,
    broad_output: dict[str, Any] | None,
    *,
    paper_api_candidates: list[dict[str, Any]] | None,
    lane_failures: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Merge independent Phase 4 API and broad lanes without losing API disposition audit."""
    if api_output is None and broad_output is None:
        raise ValueError("phase_4_papers: API and broad lanes both failed")

    merged_candidates: list[dict[str, Any]] = []
    for lane_output in (api_output, broad_output):
        if lane_output is None:
            continue
        for raw in lane_output.get("candidates") or []:
            candidate = dict(raw)
            candidate["paper_api_candidate_ids"] = list(
                dict.fromkeys(candidate.get("paper_api_candidate_ids") or [])
            )
            candidate["discovery_modes"] = list(
                dict.fromkeys(
                    candidate.get("discovery_modes")
                    or [candidate.get("discovery_mode")]
                )
            )
            existing = next(
                (item for item in merged_candidates if _candidate_match(item, candidate)),
                None,
            )
            if existing is None:
                merged_candidates.append(candidate)
                continue
            preserved_ids = list(
                dict.fromkeys(
                    list(existing.get("paper_api_candidate_ids") or [])
                    + list(candidate.get("paper_api_candidate_ids") or [])
                )
            )
            preserved_modes = list(
                dict.fromkeys(
                    list(existing.get("discovery_modes") or [])
                    + list(candidate.get("discovery_modes") or [])
                )
            )
            if float(candidate.get("confidence") or 0) > float(
                existing.get("confidence") or 0
            ):
                existing.update(candidate)
            existing["paper_api_candidate_ids"] = preserved_ids
            existing["discovery_modes"] = preserved_modes

    api_coverage = (api_output or {}).get("search_coverage") or {}
    broad_coverage = (broad_output or {}).get("search_coverage") or {}
    limitations = unique_nonempty_strings(
        [api_coverage.get("limitations"), broad_coverage.get("limitations")]
    )
    failures = lane_failures or []
    if failures:
        limitations.append(
            " | ".join(
                f"{item.get('lane')}: {item.get('error')}" for item in failures
            )
        )

    excluded_api_candidates = list(
        (api_output or {}).get("excluded_api_candidates") or []
    )
    if api_output is None and paper_api_candidates is not None:
        excluded_api_candidates = [
            {
                "candidate_id": candidate.get("candidate_id", ""),
                "reason_code": "lane_failure",
                "reason": (
                    "Phase 4 API review lane failed after retry; the candidate was not "
                    "available to Master and remains recorded for operational audit."
                ),
            }
            for candidate in paper_api_candidates
        ]

    coverage = {
        "queries_run": list(api_coverage.get("queries_run") or [])
        + list(broad_coverage.get("queries_run") or []),
        "api_queries_run": list(api_coverage.get("queries_run") or []),
        "broad_queries_run": list(
            broad_coverage.get("broad_queries_run")
            or broad_coverage.get("queries_run")
            or []
        ),
        "api_input_candidate_count": len(paper_api_candidates or []),
        "api_candidate_count": len((api_output or {}).get("candidates") or []),
        "broad_candidate_count": len((broad_output or {}).get("candidates") or []),
        "merged_candidate_count": len(merged_candidates),
        "notable_zero_result_queries": list(
            api_coverage.get("notable_zero_result_queries") or []
        )
        + list(broad_coverage.get("notable_zero_result_queries") or []),
        "limitations": " | ".join(dict.fromkeys(limitations)),
        "lane_failures": failures,
    }
    merged = {
        "phase": "phase_4_papers",
        "period": (api_output or broad_output or {}).get("period"),
        "candidates": merged_candidates,
        "excluded_api_candidates": excluded_api_candidates,
        "search_coverage": coverage,
    }
    return validate_phase_output(
        merged,
        expected_phase="phase_4_papers",
        paper_api_candidates=paper_api_candidates,
    )


def phase_output_for_master(phase_output: dict[str, Any]) -> dict[str, Any]:
    if phase_output.get("phase") != "phase_4_papers":
        return phase_output
    compact = dict(phase_output)
    compact.pop("excluded_api_candidates", None)
    return compact


def write_phase_4_rejections(
    output_path: Path,
    phase_output: dict[str, Any],
    paper_api_result: dict[str, Any] | None = None,
) -> Path:
    artifact_path = phase_artifact_path(output_path, "phase_4", ".rejections.json")
    write_json(
        artifact_path,
        {
            "phase": phase_output.get("phase"),
            "period": phase_output.get("period"),
            "paper_api_audit": phase_output.get("paper_api_audit") or {},
            "excluded_api_candidates": phase_output.get("excluded_api_candidates") or [],
            "prefilter_excluded_candidates": (
                (paper_api_result or {}).get("prefilter_excluded_candidates") or []
            ),
        },
    )
    return artifact_path


def collect_phase_4_paper_api_if_enabled(args: argparse.Namespace, *, period: str, output_path: Path, preference_profile: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if args.disable_paper_api:
        return None
    cache_dir_value = os.getenv("NEWSBOT_API_CACHE_DIR", "").strip() or str(
        PROJECT_ROOT / "data" / "api-cache"
    )
    cache_ttl_value = os.getenv(
        "NEWSBOT_API_CACHE_TTL_SECONDS", str(DEFAULT_API_CACHE_TTL_SECONDS)
    ).strip()
    result = add_paper_api_candidate_ids(
        redact_sensitive_values(
            collect_paper_api_candidates(
                period=period,
                retmax=paper_api_retmax(args.paper_api_retmax),
                ncbi_api_key=os.getenv("NCBI_API_KEY", "").strip(),
                ncbi_tool=os.getenv("NEWSBOT_NCBI_TOOL", "").strip(),
                ncbi_email=os.getenv("NEWSBOT_NCBI_EMAIL", "").strip(),
                crossref_mailto=os.getenv("NEWSBOT_CROSSREF_MAILTO", "").strip(),
                cache_dir=cache_dir_value,
                cache_ttl_seconds=int(cache_ttl_value),
                positive_auxiliary_terms=((preference_profile or {}).get("editorial") or {}).get("positive_terms") or [],
            )
        )
    )
    artifact_path = phase_artifact_path(output_path, "phase_4", ".paper_api.json")
    write_json(artifact_path, result)
    candidate_count = len(result.get("candidates") or [])
    log_step(f"phase_4 paper API candidates collected: candidates={candidate_count}, output={artifact_path}")
    return result


def phase_prompt_extra_values(phase: PhaseDefinition, paper_api_result: dict[str, Any] | None) -> dict[str, str]:
    if phase.key != "phase_4" or paper_api_result is None:
        return {}
    return {
        "{{PAPER_API_CANDIDATES_JSON}}": json.dumps(paper_api_result.get("candidates") or [], ensure_ascii=False, indent=2),
        "{{PAPER_API_COVERAGE_JSON}}": json.dumps(paper_api_result.get("coverage") or {}, ensure_ascii=False, indent=2),
    }


def run_lab_automation_multi_agent(
    *,
    args: argparse.Namespace,
    period: str,
    review_limit: int,
    output_path: Path,
    config_path: Path,
    config_raw: dict[str, Any],
    preference_profile: dict[str, Any] | None,
    store: NewsbotStore | None = None,
    feedback_audit: dict[str, Any] | None = None,
    pipeline_run_id: str = "",
    feed_cache_dir: Path | None = None,
) -> dict[str, Any] | None:
    pipeline_started_at = time.monotonic()
    phases = lab_automation_phase_definitions()
    if args.only_phase:
        phases = [phase for phase in phases if phase.key == args.only_phase]
    phase_models = parse_phase_models(list(args.phase_model))
    phase_reasoning = parse_phase_reasoning(list(args.phase_reasoning))
    default_model = default_codex_model(args.codex_model)
    default_effort = default_reasoning_effort(args.codex_reasoning_effort)
    codex_args = list(args.codex_arg)

    log_step("using lab_automation multi-agent workflow")
    phase_outputs: list[dict[str, Any]] = []
    run_metrics: dict[str, Any] = {"phases": {}, "feedback": feedback_audit or {}, "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    feed_result: dict[str, Any] = {"candidates": [], "sources": [], "candidate_count": 0, "failure_count": 0}
    if any(
        phase.key in {"phase_1", "phase_2", "phase_3"}
        and phase.broad_prompt_file
        for phase in phases
    ) and not args.skip_codex:
        feed_config = Path(os.getenv("NEWSBOT_FEED_CONFIG", "").strip() or PROJECT_ROOT / "config" / "lab_automation_feeds.json")
        feed_cache = feed_cache_dir or Path(
            os.getenv("NEWSBOT_FEED_CACHE_DIR", "").strip()
            or PROJECT_ROOT / "data" / "feed-cache"
        )
        feed_result = collect_feed_candidates(period=period, sources=load_feed_sources(feed_config), cache_dir=feed_cache, timeout=15)
        feed_path = output_path.with_suffix(".feeds.audit.json")
        write_json(feed_path, feed_result)
        log_step(f"feed discovery finished: candidates={feed_result['candidate_count']}, failures={feed_result['failure_count']}, output={feed_path}")

    if args.skip_codex:
        for phase in phases:
            paper_api_result = collect_phase_4_paper_api_if_enabled(args, period=period, output_path=output_path, preference_profile=preference_profile) if phase.key == "phase_4" else None
            paper_api_batches: list[dict[str, Any] | None] = [paper_api_result]
            if phase.key == "phase_4" and paper_api_result is not None:
                paper_api_batches = split_paper_api_result_for_llm(
                    paper_api_result,
                    phase_4_llm_batch_size(args.phase_4_llm_batch_size),
                )
            for batch_index, batch_result in enumerate(paper_api_batches, start=1):
                prompt, template_path = build_phase_prompt(
                    phase=phase,
                    topic=args.topic,
                    cadence=args.cadence,
                    period=period,
                    max_items=review_limit,
                    output_path=output_path,
                    config_path=config_path,
                    preference_profile=preference_profile_for_phase(preference_profile, phase),
                    extra_values=phase_prompt_extra_values(phase, batch_result),
                )
                suffix = (
                    f".api.batch_{batch_index:03d}.prompt.txt"
                    if phase.key == "phase_4" and len(paper_api_batches) > 1
                    else ".api.prompt.txt"
                    if phase.key == "phase_4"
                    else f".batch_{batch_index:03d}.prompt.txt"
                    if len(paper_api_batches) > 1
                    else ".structured.prompt.txt"
                    if phase.broad_prompt_file
                    else ".prompt.txt"
                )
                prompt_path = phase_artifact_path(output_path, phase.key, suffix)
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(prompt, encoding="utf-8")
                log_step(
                    f"wrote {phase.key} prompt from {template_path}: {prompt_path}"
                )
            if phase.broad_prompt_file:
                broad_prompt, broad_template = build_phase_prompt(
                    phase=phase, topic=args.topic, cadence=args.cadence, period=period,
                    max_items=review_limit, output_path=output_path, config_path=config_path,
                    preference_profile=broad_lane_preference_profile(preference_profile_for_phase(preference_profile, phase)), prompt_file=phase.broad_prompt_file,
                )
                broad_path = phase_artifact_path(output_path, phase.key, ".broad.prompt.txt")
                broad_path.parent.mkdir(parents=True, exist_ok=True)
                broad_path.write_text(broad_prompt, encoding="utf-8")
                log_step(f"wrote {phase.key} broad prompt from {broad_template}: {broad_path}")
        if args.only_phase:
            log_step(f"{args.only_phase}-only prompt generation finished")
            return None
        master_prompt, master_template_path = build_master_prompt(
            topic=args.topic,
            cadence=args.cadence,
            period=period,
            max_items=review_limit,
            output_path=output_path,
            config_path=config_path,
            preference_profile=preference_profile,
            phase_outputs=[],
            config_raw=config_raw,
        )
        master_prompt_path = resolve_project_path(args.prompt_output) if args.prompt_output else phase_artifact_path(output_path, "master", ".prompt.txt")
        master_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        master_prompt_path.write_text(master_prompt, encoding="utf-8")
        log_step(f"wrote master prompt from {master_template_path}: {master_prompt_path}")
        log_step("skip-codex requested; exiting after multi-agent prompt write")
        return None

    for phase in phases:
        phase_started_at = time.monotonic()
        if phase.key == "phase_4":
            model = phase_model_for(phase, phase_models, default_model)
            reasoning_effort = phase_reasoning_for(
                phase, phase_reasoning, default_effort
            )
            model_label = model or "(Codex CLI default)"
            reasoning_label = reasoning_effort or "(Codex CLI default)"
            lane_results: dict[str, dict[str, Any]] = {}
            lane_failures: list[dict[str, str]] = []
            lane_metrics: dict[str, Any] = {}
            api_state: dict[str, Any] = {"paper_api_result": None}

            def execute_phase_4_api_lane() -> tuple[
                str, dict[str, Any] | None, float, dict[str, Any]
            ]:
                started = time.monotonic()
                if args.disable_paper_api:
                    return "api", None, time.monotonic() - started, {
                        "disabled": True,
                        "candidate_count": 0,
                        "token_usage": {},
                    }
                paper_result = collect_phase_4_paper_api_if_enabled(
                    args,
                    period=period,
                    output_path=output_path,
                    preference_profile=preference_profile,
                )
                api_state["paper_api_result"] = paper_result
                batches = split_paper_api_result_for_llm(
                    paper_result or {},
                    phase_4_llm_batch_size(args.phase_4_llm_batch_size),
                )
                if len(batches) > 1:
                    log_step(
                        "phase_4 API review split into "
                        f"{len(batches)} batches; "
                        f"batch_size={phase_4_llm_batch_size(args.phase_4_llm_batch_size)}"
                    )
                batch_outputs: list[dict[str, Any]] = []
                attempt_log_paths: list[Path] = []
                batch_audit: list[dict[str, Any]] = []
                for batch_index, batch_result in enumerate(batches, start=1):
                    prompt, template_path = build_phase_prompt(
                        phase=phase,
                        topic=args.topic,
                        cadence=args.cadence,
                        period=period,
                        max_items=review_limit,
                        output_path=output_path,
                        config_path=config_path,
                        preference_profile=preference_profile_for_phase(
                            preference_profile, phase
                        ),
                        extra_values=phase_prompt_extra_values(phase, batch_result),
                    )
                    multi_batch = len(batches) > 1
                    artifact_stem = (
                        f".api.batch_{batch_index:03d}"
                        if multi_batch
                        else ".api"
                    )
                    canonical_prompt_path = phase_artifact_path(
                        output_path, phase.key, f"{artifact_stem}.prompt.txt"
                    )
                    canonical_log_path = phase_artifact_path(
                        output_path, phase.key, f"{artifact_stem}.codex.log"
                    )
                    canonical_prompt_path.parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    canonical_prompt_path.write_text(prompt, encoding="utf-8")
                    last_error: Exception | None = None
                    value: dict[str, Any] | None = None
                    attempt_prompt = prompt
                    attempts: list[dict[str, Any]] = []
                    for attempt in (1, 2):
                        attempt_prompt_path = phase_artifact_path(
                            output_path,
                            phase.key,
                            f"{artifact_stem}.attempt_{attempt}.prompt.txt",
                        )
                        attempt_log_path = phase_artifact_path(
                            output_path,
                            phase.key,
                            f"{artifact_stem}.attempt_{attempt}.codex.log",
                        )
                        attempt_prompt_path.write_text(
                            attempt_prompt, encoding="utf-8"
                        )
                        attempt_log_paths.append(attempt_log_path)
                        batch_label = (
                            f" batch {batch_index}/{len(batches)}"
                            if multi_batch
                            else ""
                        )
                        log_step(
                            f"running phase_4 API lane{batch_label} attempt {attempt}/2: "
                            f"template={template_path}, model={model_label}, "
                            f"reasoning={reasoning_label}"
                        )
                        try:
                            output = run_codex(
                                codex_bin=args.codex_bin,
                                codex_model=model,
                                reasoning_effort=reasoning_effort,
                                codex_args=codex_args,
                                prompt=attempt_prompt,
                                timeout=args.codex_timeout,
                                log_path=attempt_log_path,
                            )
                            value = validate_phase_output(
                                extract_json_object(output),
                                expected_phase=phase.phase,
                                paper_api_candidates=(batch_result or {}).get(
                                    "candidates"
                                )
                                or [],
                            )
                            attempts.append(
                                {
                                    "attempt": attempt,
                                    "status": "accepted",
                                    "prompt_path": str(attempt_prompt_path),
                                    "log_path": str(attempt_log_path),
                                }
                            )
                            publish_codex_attempt_alias(
                                attempt_log_path, canonical_log_path
                            )
                            break
                        except Exception as exc:
                            last_error = exc
                            attempts.append(
                                {
                                    "attempt": attempt,
                                    "status": "rejected",
                                    "prompt_path": str(attempt_prompt_path),
                                    "log_path": str(attempt_log_path),
                                    "error": str(exc),
                                }
                            )
                            log_step(
                                f"phase_4 API lane{batch_label} attempt {attempt}/2 failed: {exc}"
                            )
                            attempt_prompt = prompt + "\n\n" + discovery_retry_instructions(
                                "api", str(exc)
                            )
                    batch_audit.append(
                        {"batch_index": batch_index, "attempts": attempts}
                    )
                    if value is None:
                        write_json(
                            phase_artifact_path(
                                output_path,
                                phase.key,
                                ".api.attempts.audit.json",
                            ),
                            {"lane": "api", "batches": batch_audit},
                        )
                        assert last_error is not None
                        raise last_error
                    batch_outputs.append(value)
                    if multi_batch:
                        write_json(
                            phase_artifact_path(
                                output_path,
                                phase.key,
                                f".api.batch_{batch_index:03d}.json",
                            ),
                            value,
                        )
                write_json(
                    phase_artifact_path(
                        output_path, phase.key, ".api.attempts.audit.json"
                    ),
                    {"lane": "api", "batches": batch_audit},
                )
                if len(batch_outputs) > 1:
                    api_output = merge_phase_4_batch_outputs(
                        batch_outputs,
                        paper_api_candidates=(paper_result or {}).get("candidates")
                        or [],
                    )
                else:
                    api_output = batch_outputs[0]
                write_json(
                    phase_artifact_path(output_path, phase.key, ".api.json"),
                    api_output,
                )
                metrics = {
                    "batch_count": len(batches),
                    "candidate_count": len(api_output.get("candidates") or []),
                    "input_candidate_count": len(
                        (paper_result or {}).get("candidates") or []
                    ),
                    "token_usage": aggregate_codex_usage(attempt_log_paths),
                }
                return "api", api_output, time.monotonic() - started, metrics

            def execute_phase_4_broad_lane() -> tuple[
                str, dict[str, Any], float, dict[str, Any]
            ]:
                started = time.monotonic()
                prompt, template_path = build_phase_prompt(
                    phase=phase,
                    topic=args.topic,
                    cadence=args.cadence,
                    period=period,
                    max_items=review_limit,
                    output_path=output_path,
                    config_path=config_path,
                    preference_profile=broad_lane_preference_profile(
                        preference_profile_for_phase(preference_profile, phase)
                    ),
                    prompt_file=phase.broad_prompt_file,
                )
                canonical_prompt_path = phase_artifact_path(
                    output_path, phase.key, ".broad.prompt.txt"
                )
                canonical_log_path = phase_artifact_path(
                    output_path, phase.key, ".broad.codex.log"
                )
                canonical_prompt_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_prompt_path.write_text(prompt, encoding="utf-8")
                attempt_prompt = prompt
                attempt_log_paths: list[Path] = []
                attempt_audit: list[dict[str, Any]] = []
                value: dict[str, Any] | None = None
                last_error: Exception | None = None
                for attempt in (1, 2):
                    attempt_prompt_path = phase_artifact_path(
                        output_path,
                        phase.key,
                        f".broad.attempt_{attempt}.prompt.txt",
                    )
                    attempt_log_path = phase_artifact_path(
                        output_path,
                        phase.key,
                        f".broad.attempt_{attempt}.codex.log",
                    )
                    attempt_prompt_path.write_text(attempt_prompt, encoding="utf-8")
                    attempt_log_paths.append(attempt_log_path)
                    log_step(
                        f"running phase_4 broad lane attempt {attempt}/2: "
                        f"template={template_path}, model={model_label}, "
                        f"reasoning={reasoning_label}"
                    )
                    try:
                        output = run_codex(
                            codex_bin=args.codex_bin,
                            codex_model=model,
                            reasoning_effort=reasoning_effort,
                            codex_args=codex_args,
                            prompt=attempt_prompt,
                            timeout=args.codex_timeout,
                            log_path=attempt_log_path,
                        )
                        raw_value = extract_json_object(output)
                        search_queries = codex_web_search_queries(
                            codex_sibling_artifact_path(
                                attempt_log_path, ".codex.events.jsonl"
                            )
                        )
                        value = validate_phase_output(
                            normalize_discovery_lane_output(
                                raw_value,
                                "broad",
                                web_search_queries=search_queries,
                            ),
                            expected_phase=phase.phase,
                            discovery_lane="broad",
                        )
                        for candidate in value.get("candidates") or []:
                            candidate["paper_api_candidate_ids"] = []
                        attempt_audit.append(
                            {
                                "attempt": attempt,
                                "status": "accepted",
                                "prompt_path": str(attempt_prompt_path),
                                "log_path": str(attempt_log_path),
                                "web_search_query_count": len(search_queries),
                            }
                        )
                        publish_codex_attempt_alias(
                            attempt_log_path, canonical_log_path
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        attempt_audit.append(
                            {
                                "attempt": attempt,
                                "status": "rejected",
                                "prompt_path": str(attempt_prompt_path),
                                "log_path": str(attempt_log_path),
                                "error": str(exc),
                            }
                        )
                        log_step(
                            f"phase_4 broad lane attempt {attempt}/2 failed: {exc}"
                        )
                        attempt_prompt = prompt + "\n\n" + discovery_retry_instructions(
                            "broad", str(exc)
                        )
                write_json(
                    phase_artifact_path(
                        output_path, phase.key, ".broad.attempts.audit.json"
                    ),
                    {"lane": "broad", "attempts": attempt_audit},
                )
                if value is None:
                    assert last_error is not None
                    raise last_error
                write_json(
                    phase_artifact_path(output_path, phase.key, ".broad.json"),
                    value,
                )
                metrics = {
                    "attempts": len(attempt_audit),
                    "candidate_count": len(value.get("candidates") or []),
                    "token_usage": aggregate_codex_usage(attempt_log_paths),
                }
                return "broad", value, time.monotonic() - started, metrics

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_by_lane = {
                    executor.submit(execute_phase_4_api_lane): "api",
                    executor.submit(execute_phase_4_broad_lane): "broad",
                }
                for future, lane in [
                    (future, future_by_lane[future]) for future in future_by_lane
                ]:
                    try:
                        completed_lane, value, elapsed, metrics = future.result()
                        if value is not None:
                            lane_results[completed_lane] = value
                        lane_metrics[completed_lane] = {
                            **metrics,
                            "elapsed_seconds": round(elapsed, 3),
                        }
                    except Exception as exc:
                        lane_failures.append({"lane": lane, "error": str(exc)})
                        lane_metrics[lane] = {"failed": True, "error": str(exc)}

            paper_api_result = api_state.get("paper_api_result")
            phase_json = merge_phase_4_lane_outputs(
                lane_results.get("api"),
                lane_results.get("broad"),
                paper_api_candidates=(paper_api_result or {}).get("candidates")
                if paper_api_result is not None
                else None,
                lane_failures=lane_failures,
            )
            json_path = phase_artifact_path(output_path, phase.key, ".json")
            write_json(json_path, phase_json)
            if paper_api_result is not None:
                rejections_path = write_phase_4_rejections(
                    output_path, phase_json, paper_api_result
                )
                log_step(
                    "phase_4 API candidate audit written: "
                    f"selected={phase_json['paper_api_audit']['selected_candidate_count']}, "
                    f"excluded={phase_json['paper_api_audit']['excluded_candidate_count']}, "
                    f"output={rejections_path}"
                )
            phase_outputs.append(phase_output_for_master(phase_json))
            if store and pipeline_run_id:
                store.record_pipeline_candidates(
                    pipeline_run_id,
                    phase.phase,
                    phase_json.get("candidates") or [],
                )
            elapsed = time.monotonic() - phase_started_at
            run_metrics["phases"][phase.key] = {
                "elapsed_seconds": round(elapsed, 3),
                "lanes": lane_metrics,
                "candidate_count": len(phase_json.get("candidates") or []),
            }
            log_step(
                "phase_4 parallel lanes finished: "
                f"candidates={len(phase_json.get('candidates') or [])}, "
                f"elapsed_seconds={elapsed:.1f}, output={json_path}"
            )
            continue
        if phase.broad_prompt_file:
            lane_prompts: dict[str, tuple[str, Path]] = {}
            lane_prompts["structured"] = build_phase_prompt(
                phase=phase, topic=args.topic, cadence=args.cadence, period=period,
                max_items=review_limit, output_path=output_path, config_path=config_path,
                preference_profile=preference_profile_for_phase(preference_profile, phase),
                extra_values={"{{FEED_CANDIDATES_JSON}}": json.dumps([item for item in feed_result.get("candidates") or [] if item.get("phase") == phase.key], ensure_ascii=False, indent=2)},
            )
            lane_prompts["broad"] = build_phase_prompt(
                phase=phase, topic=args.topic, cadence=args.cadence, period=period,
                max_items=review_limit, output_path=output_path, config_path=config_path,
                preference_profile=broad_lane_preference_profile(preference_profile_for_phase(preference_profile, phase)), prompt_file=phase.broad_prompt_file,
            )
            model = phase_model_for(phase, phase_models, default_model)
            reasoning_effort = phase_reasoning_for(phase, phase_reasoning, default_effort)
            lane_results: dict[str, dict[str, Any]] = {}
            lane_failures: list[dict[str, str]] = []
            lane_metrics: dict[str, Any] = {}

            def execute_lane(lane: str) -> tuple[str, dict[str, Any], float, int, dict[str, Any]]:
                started = time.monotonic()
                prompt, template_path = lane_prompts[lane]
                prompt_path = phase_artifact_path(output_path, phase.key, f".{lane}.prompt.txt")
                canonical_log_path = phase_artifact_path(
                    output_path, phase.key, f".{lane}.codex.log"
                )
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(prompt, encoding="utf-8")
                log_step(f"running {phase.key} {lane} lane: template={template_path}, model={model or '(default)'}, reasoning={reasoning_effort or '(default)'}")
                last_error: Exception | None = None
                value: dict[str, Any] | None = None
                attempts = 0
                attempt_log_paths: list[Path] = []
                attempt_audit: list[dict[str, Any]] = []
                attempt_prompt = prompt
                for attempts in (1, 2):
                    attempt_log_path = phase_artifact_path(
                        output_path,
                        phase.key,
                        f".{lane}.attempt_{attempts}.codex.log",
                    )
                    attempt_prompt_path = phase_artifact_path(
                        output_path,
                        phase.key,
                        f".{lane}.attempt_{attempts}.prompt.txt",
                    )
                    attempt_prompt_path.write_text(attempt_prompt, encoding="utf-8")
                    attempt_log_paths.append(attempt_log_path)
                    try:
                        output = run_codex(
                            codex_bin=args.codex_bin, codex_model=model, reasoning_effort=reasoning_effort,
                            codex_args=codex_args, prompt=attempt_prompt, timeout=args.codex_timeout, log_path=attempt_log_path,
                        )
                        raw_value = extract_json_object(output)
                        search_queries = codex_web_search_queries(
                            codex_sibling_artifact_path(
                                attempt_log_path, ".codex.events.jsonl"
                            )
                        )
                        value = validate_phase_output(
                            normalize_discovery_lane_output(
                                raw_value,
                                lane,
                                web_search_queries=search_queries,
                            ),
                            expected_phase=phase.phase,
                            discovery_lane=lane,
                        )
                        attempt_audit.append(
                            {
                                "attempt": attempts,
                                "status": "accepted",
                                "prompt_path": str(attempt_prompt_path),
                                "log_path": str(attempt_log_path),
                                "web_search_query_count": len(search_queries),
                            }
                        )
                        publish_codex_attempt_alias(
                            attempt_log_path, canonical_log_path
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        attempt_audit.append(
                            {
                                "attempt": attempts,
                                "status": "rejected",
                                "prompt_path": str(attempt_prompt_path),
                                "log_path": str(attempt_log_path),
                                "error": str(exc),
                            }
                        )
                        log_step(f"{phase.key} {lane} lane attempt {attempts}/2 failed: {exc}")
                        attempt_prompt = prompt + "\n\n" + discovery_retry_instructions(
                            lane, str(exc)
                        )
                write_json(
                    phase_artifact_path(
                        output_path, phase.key, f".{lane}.attempts.audit.json"
                    ),
                    {"lane": lane, "attempts": attempt_audit},
                )
                if value is None:
                    assert last_error is not None
                    raise last_error
                write_json(phase_artifact_path(output_path, phase.key, f".{lane}.json"), value)
                usage = aggregate_codex_usage(attempt_log_paths)
                return lane, value, time.monotonic() - started, attempts, usage

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_by_lane = {executor.submit(execute_lane, lane): lane for lane in ("structured", "broad")}
                for future, lane in [(future, future_by_lane[future]) for future in future_by_lane]:
                    try:
                        completed_lane, value, elapsed, attempts, usage = future.result()
                        lane_results[completed_lane] = value
                        lane_metrics[completed_lane] = {"elapsed_seconds": round(elapsed, 3), "attempts": attempts, "candidate_count": len(value.get("candidates") or []), "token_usage": usage}
                    except Exception as exc:
                        lane_failures.append({"lane": lane, "error": str(exc)})
                        lane_metrics[lane] = {"failed": True, "error": str(exc)}
            phase_json = merge_discovery_lane_outputs(lane_results.get("structured"), lane_results.get("broad"), expected_phase=phase.phase, lane_failures=lane_failures)
            json_path = phase_artifact_path(output_path, phase.key, ".json")
            write_json(json_path, phase_json)
            phase_outputs.append(phase_output_for_master(phase_json))
            if store and pipeline_run_id:
                store.record_pipeline_candidates(pipeline_run_id, phase.phase, phase_json.get("candidates") or [])
            elapsed = time.monotonic() - phase_started_at
            run_metrics["phases"][phase.key] = {"elapsed_seconds": round(elapsed, 3), "lanes": lane_metrics, "candidate_count": len(phase_json.get("candidates") or [])}
            log_step(f"{phase.key} isolated lanes finished: candidates={len(phase_json.get('candidates') or [])}, elapsed_seconds={elapsed:.1f}, output={json_path}")
            continue
        paper_api_result = collect_phase_4_paper_api_if_enabled(args, period=period, output_path=output_path, preference_profile=preference_profile) if phase.key == "phase_4" else None
        paper_api_batches: list[dict[str, Any] | None] = [paper_api_result]
        if phase.key == "phase_4" and paper_api_result is not None:
            paper_api_batches = split_paper_api_result_for_llm(
                paper_api_result,
                phase_4_llm_batch_size(args.phase_4_llm_batch_size),
            )
            if len(paper_api_batches) > 1:
                log_step(
                    "phase_4 LLM review split into "
                    f"{len(paper_api_batches)} batches; "
                    f"batch_size={phase_4_llm_batch_size(args.phase_4_llm_batch_size)}"
                )
        json_path = phase_artifact_path(output_path, phase.key, ".json")
        model = phase_model_for(phase, phase_models, default_model)
        reasoning_effort = phase_reasoning_for(phase, phase_reasoning, default_effort)
        model_label = model or "(Codex CLI default)"
        reasoning_label = reasoning_effort or "(Codex CLI default)"
        batch_outputs: list[dict[str, Any]] = []
        phase_log_paths: list[Path] = []
        for batch_index, batch_result in enumerate(paper_api_batches, start=1):
            prompt, template_path = build_phase_prompt(
                phase=phase,
                topic=args.topic,
                cadence=args.cadence,
                period=period,
                max_items=review_limit,
                output_path=output_path,
                config_path=config_path,
                preference_profile=preference_profile_for_phase(preference_profile, phase),
                extra_values=phase_prompt_extra_values(phase, batch_result),
            )
            multi_batch = len(paper_api_batches) > 1
            prompt_suffix = (
                f".batch_{batch_index:03d}.prompt.txt"
                if multi_batch
                else ".prompt.txt"
            )
            log_suffix = (
                f".batch_{batch_index:03d}.codex.log"
                if multi_batch
                else ".codex.log"
            )
            batch_json_suffix = f".batch_{batch_index:03d}.json"
            prompt_path = phase_artifact_path(output_path, phase.key, prompt_suffix)
            log_path = phase_artifact_path(output_path, phase.key, log_suffix)
            phase_log_paths.append(log_path)
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            batch_label = (
                f" batch {batch_index}/{len(paper_api_batches)}" if multi_batch else ""
            )
            log_step(
                f"running {phase.key}{batch_label}: template={template_path}, "
                f"model={model_label}, reasoning={reasoning_label}"
            )
            output = run_codex(
                codex_bin=args.codex_bin,
                codex_model=model,
                reasoning_effort=reasoning_effort,
                codex_args=codex_args,
                prompt=prompt,
                timeout=args.codex_timeout,
                log_path=log_path,
            )
            batch_json = validate_phase_output(
                extract_json_object(output),
                expected_phase=phase.phase,
                paper_api_candidates=(batch_result or {}).get("candidates")
                if phase.key == "phase_4" and batch_result is not None
                else None,
            )
            batch_outputs.append(batch_json)
            if multi_batch:
                write_json(
                    phase_artifact_path(output_path, phase.key, batch_json_suffix),
                    batch_json,
                )

        if phase.key == "phase_4" and len(batch_outputs) > 1:
            phase_json = merge_phase_4_batch_outputs(
                batch_outputs,
                paper_api_candidates=(paper_api_result or {}).get("candidates") or [],
            )
        else:
            phase_json = batch_outputs[0]
        write_json(json_path, phase_json)
        if phase.key == "phase_4" and paper_api_result is not None:
            rejections_path = write_phase_4_rejections(
                output_path, phase_json, paper_api_result
            )
            log_step(
                "phase_4 API candidate audit written: "
                f"selected={phase_json['paper_api_audit']['selected_candidate_count']}, "
                f"excluded={phase_json['paper_api_audit']['excluded_candidate_count']}, "
                f"output={rejections_path}"
            )
        phase_outputs.append(phase_output_for_master(phase_json))
        if store and pipeline_run_id:
            store.record_pipeline_candidates(pipeline_run_id, phase.phase, phase_json.get("candidates") or [])
        phase_elapsed_seconds = time.monotonic() - phase_started_at
        run_metrics["phases"][phase.key] = {"elapsed_seconds": round(phase_elapsed_seconds, 3), "candidate_count": len(phase_json.get("candidates") or []), "token_usage": aggregate_codex_usage(phase_log_paths)}
        log_step(
            f"{phase.key} finished: candidates={len(phase_json.get('candidates') or [])}, "
            f"elapsed_seconds={phase_elapsed_seconds:.1f}, output={json_path}"
        )

    if args.only_phase:
        run_metrics["pipeline_elapsed_seconds"] = round(time.monotonic() - pipeline_started_at, 3)
        run_metrics["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        run_metrics["lane_candidate_counts"] = discovery_lane_counts(phase_outputs)
        write_json(output_path.with_suffix(".run.audit.json"), run_metrics)
        if store and pipeline_run_id:
            store.finish_pipeline_run(pipeline_run_id, status="complete", audit=run_metrics)
        log_step(f"{args.only_phase}-only execution finished; skipping master builder and Discord submission")
        return None

    master_prompt, master_template_path = build_master_prompt(
        topic=args.topic,
        cadence=args.cadence,
        period=period,
        max_items=review_limit,
        output_path=output_path,
        config_path=config_path,
        preference_profile=preference_profile,
        phase_outputs=phase_outputs,
        config_raw=config_raw,
    )
    master_prompt_path = resolve_project_path(args.prompt_output) if args.prompt_output else phase_artifact_path(output_path, "master", ".prompt.txt")
    master_json_path = phase_artifact_path(output_path, "master", ".json")
    master_log_path = phase_artifact_path(output_path, "master", ".codex.log")
    master_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    master_prompt_path.write_text(master_prompt, encoding="utf-8")
    master_model = master_model_for(args.master_model, default_model)
    master_reasoning_effort = master_reasoning_for(args.master_reasoning_effort, default_effort)
    model_label = master_model or "(Codex CLI default)"
    reasoning_label = master_reasoning_effort or "(Codex CLI default)"
    log_step(f"running master builder: template={master_template_path}, model={model_label}, reasoning={reasoning_label}")
    master_started_at = time.monotonic()
    master_output = run_codex(
        codex_bin=args.codex_bin,
        codex_model=master_model,
        reasoning_effort=master_reasoning_effort,
        codex_args=codex_args,
        prompt=master_prompt,
        timeout=args.codex_timeout,
        log_path=master_log_path,
    )
    payload = enrich_master_items_from_candidates(extract_json_object(master_output), phase_outputs)
    if store and pipeline_run_id:
        store.mark_master_selected(pipeline_run_id, payload.get("items") or [])
    write_json(master_json_path, payload)
    master_elapsed_seconds = time.monotonic() - master_started_at
    pipeline_elapsed_seconds = time.monotonic() - pipeline_started_at
    run_metrics["master"] = {"elapsed_seconds": round(master_elapsed_seconds, 3), "selected_count": len(payload.get("items") or [])}
    run_metrics.update(selected_discovery_metrics(phase_outputs, payload.get("items") or []))
    run_metrics["editorial_exclusion_reason_counts"] = (preference_profile or {}).get("editorial", {}).get("reason_codes_by_decision", {}).get("excluded", {})
    if store:
        prior_runs = store.completed_pipeline_run_count(topic=args.topic, cadence=args.cadence)
        run_metrics["formal_comparison"] = {"normal_run_count_after_this_run": prior_runs + 1, "ready": prior_runs + 1 >= 4, "minimum_runs": 4}
        editorial = (preference_profile or {}).get("editorial") or {}
        run_metrics["human_evaluation"] = store.editorial_discovery_metrics(
            topic=args.topic, cadence=args.cadence,
            window_start=str(editorial.get("window_start") or ""),
            window_end=str(editorial.get("window_end") or ""),
        )
    run_metrics["pipeline_elapsed_seconds"] = round(pipeline_elapsed_seconds, 3)
    run_metrics["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    run_metrics["lane_candidate_counts"] = discovery_lane_counts(phase_outputs)
    write_json(output_path.with_suffix(".run.audit.json"), run_metrics)
    if store and pipeline_run_id:
        store.finish_pipeline_run(pipeline_run_id, status="complete", audit=run_metrics)
    log_step(
        f"master builder finished: raw_items={len(payload.get('items') or [])}, "
        f"elapsed_seconds={master_elapsed_seconds:.1f}, output={master_json_path}"
    )
    log_step(f"multi-agent pipeline finished: elapsed_seconds={pipeline_elapsed_seconds:.1f}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a Newsbot payload with Codex CLI, validate it, and optionally submit it for review."
    )
    parser.add_argument("--topic", default="lab_automation")
    parser.add_argument("--cadence", default="weekly")
    parser.add_argument("--period", default="")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Search this many days back. Defaults to NEWSBOT_LOOKBACK_DAYS, then previous successful run.",
    )
    parser.add_argument("--output", default="")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-model", default="", help="Model name passed to `codex exec --model`. Defaults to NEWSBOT_CODEX_MODEL.")
    parser.add_argument(
        "--codex-reasoning-effort",
        default="",
        choices=sorted(SUPPORTED_REASONING_EFFORTS),
        help="Reasoning effort passed to Codex CLI via model_reasoning_effort. Defaults to NEWSBOT_CODEX_REASONING_EFFORT.",
    )
    parser.add_argument("--codex-arg", action="append", default=[], help="Extra argument passed to `codex exec`.")
    parser.add_argument("--codex-timeout", type=int, default=DEFAULT_CODEX_TIMEOUT_SECONDS)
    parser.add_argument("--single-agent", action="store_true", help="Use the legacy single-prompt workflow even for lab_automation.")
    parser.add_argument(
        "--only-phase",
        choices=[phase.key for phase in lab_automation_phase_definitions()],
        default="",
        help="Run only one lab_automation phase and skip the master builder and Discord submission.",
    )
    parser.add_argument("--disable-paper-api", action="store_true", help="Disable Phase 4 paper API collection for lab_automation multi-agent runs.")
    parser.add_argument(
        "--paper-api-retmax",
        type=int,
        default=None,
        help=(
            "Legacy-named Phase 4 API scan budget used for PubMed/Crossref query retrieval. "
            "It does not cap candidates after semantic filtering. Defaults to "
            "NEWSBOT_PAPER_API_RETMAX, then 50."
        ),
    )
    parser.add_argument(
        "--phase-4-llm-batch-size",
        type=int,
        default=None,
        help=(
            "Maximum paper API candidates reviewed by one Phase 4 Codex call. "
            "Defaults to NEWSBOT_PHASE_4_LLM_BATCH_SIZE, then 50."
        ),
    )
    parser.add_argument("--phase-model", action="append", default=[], help="Model for a lab_automation phase, e.g. phase_1=gpt-5.")
    parser.add_argument("--phase-reasoning", action="append", default=[], help="Reasoning effort for a lab_automation phase, e.g. phase_1=xhigh.")
    parser.add_argument("--master-model", default="", help="Model for the lab_automation master builder. Defaults to --codex-model, then NEWSBOT_CODEX_MODEL.")
    parser.add_argument(
        "--master-reasoning-effort",
        default="",
        choices=sorted(SUPPORTED_REASONING_EFFORTS),
        help="Reasoning effort for the lab_automation master builder. Defaults to NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT, then NEWSBOT_CODEX_REASONING_EFFORT.",
    )
    parser.add_argument("--skip-codex", action="store_true", help="Write only the prompt file; do not run Codex.")
    parser.add_argument("--prompt-output", default="", help="Optional path to save the generated Codex prompt.")
    parser.add_argument("--validate-only", action="store_true", help="Do not submit to Discord after validation.")
    parser.add_argument(
        "--isolated-dry-run",
        action="store_true",
        help=(
            "Run all requested phases without changing the production database, "
            "feed cache, successful-run timestamp, or Discord."
        ),
    )
    parser.add_argument("--submit-review", action="store_true", help="Submit the generated payload to Discord reviewer channel.")
    parser.add_argument("--submit-dry-run", action="store_true", help="Run submit-review with --dry-run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    load_env_file()
    args = build_parser().parse_args(argv)
    if args.isolated_dry_run and (args.submit_review or args.submit_dry_run):
        raise ValueError("--isolated-dry-run cannot be combined with Discord submission options")
    log_step("starting Codex payload generation")
    config_path = resolve_project_path(args.config)
    config = load_config(config_path)
    log_step(f"loaded config: {config_path}")
    env_lookback = os.getenv("NEWSBOT_LOOKBACK_DAYS", "").strip()
    lookback_days = args.lookback_days
    if lookback_days is None and env_lookback:
        lookback_days = int(env_lookback)
    period = resolve_period(
        topic=args.topic,
        cadence=args.cadence,
        explicit_period=args.period,
        lookback_days=lookback_days,
    )
    log_step(f"topic={args.topic}, cadence={args.cadence}, period={period}")
    output_path = resolve_project_path(args.output) if args.output else default_output_path(args.topic, args.cadence)
    review_limit = min(config.max_items, REVIEW_MAX_ITEMS)
    log_step(f"master/reviewer item limit={review_limit}; phase candidate limits=none")
    configured_db_path = resolve_project_path(args.db)
    isolated_feed_cache_dir: Path | None = None
    if args.isolated_dry_run:
        dry_run_db_path = create_isolated_dry_run_db(configured_db_path, output_path)
        isolated_feed_cache_dir = output_path.with_suffix(".isolated-feed-cache")
        args.validate_only = True
        store = NewsbotStore(dry_run_db_path)
        log_step(
            "isolated dry-run enabled: "
            f"database_snapshot={dry_run_db_path}, production_database={configured_db_path}"
        )
    else:
        store = NewsbotStore(configured_db_path)
    store.init()
    log_step(f"initialized database: {store.path}")
    lookback_weeks = feedback_lookback_weeks()
    audience_profile = store.audience_preference_profile(topic=args.topic, cadence=args.cadence)
    editorial_profile = store.editorial_feedback_profile(topic=args.topic, cadence=args.cadence, lookback_weeks=lookback_weeks)
    preference_profile = {"audience": audience_profile, "editorial": editorial_profile}
    profile_hash = sha256_text(stable_json(preference_profile))
    feedback_audit = {
        "lookback_weeks": lookback_weeks,
        "window_start": editorial_profile["window_start"],
        "window_end": editorial_profile["window_end"],
        "judgment_count": editorial_profile["judgment_count"],
        "profile_hash": profile_hash,
    }
    log_step(f"loaded feedback profiles: Interested={audience_profile.get('total_feedback', 0)}, editorial={editorial_profile.get('judgment_count', 0)}, lookback_weeks={lookback_weeks}")
    use_multi_agent = args.topic == LAB_AUTOMATION_TOPIC and not args.single_agent
    if args.only_phase and not use_multi_agent:
        raise ValueError("--only-phase is available only for the lab_automation multi-agent workflow")
    if use_multi_agent:
        pipeline_run_id = "" if args.skip_codex else store.start_pipeline_run(
            topic=args.topic, cadence=args.cadence, period=period,
            feedback_lookback_weeks=lookback_weeks,
            feedback_window_start=editorial_profile["window_start"],
            feedback_window_end=editorial_profile["window_end"],
            feedback_profile_hash=profile_hash,
        )
        try:
            payload = run_lab_automation_multi_agent(
                args=args,
                period=period,
                review_limit=review_limit,
                output_path=output_path,
                config_path=config_path,
                config_raw=config.raw,
                preference_profile=preference_profile,
                store=store,
                feedback_audit=feedback_audit,
                pipeline_run_id=pipeline_run_id,
                feed_cache_dir=isolated_feed_cache_dir,
            )
        except Exception as exc:
            if pipeline_run_id:
                store.finish_pipeline_run(pipeline_run_id, status="failed", audit={"feedback": feedback_audit, "error": str(exc)})
            raise
        if payload is None:
            return 0
        codex_log_label = phase_artifact_path(output_path, "master", ".codex.log")
    else:
        prompt, template_path = build_generation_prompt(
            topic=args.topic,
            cadence=args.cadence,
            period=period,
            max_items=review_limit,
            output_path=output_path,
            config_path=config_path,
            preference_profile=preference_profile,
        )
        if template_path:
            log_step(f"loaded prompt template: {template_path}")
        else:
            log_step("no prompt template found; using built-in prompt")

        prompt_path = resolve_project_path(args.prompt_output) if args.prompt_output else output_path.with_suffix(".prompt.txt")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        log_step(f"wrote Codex prompt: {prompt_path}")

        if args.skip_codex:
            log_step("skip-codex requested; exiting after prompt write")
            return 0

        log_path = output_path.with_suffix(".codex.log")
        codex_model = default_codex_model(args.codex_model)
        reasoning_effort = default_reasoning_effort(args.codex_reasoning_effort)
        model_label = codex_model or "(Codex CLI default)"
        reasoning_label = reasoning_effort or "(Codex CLI default)"
        log_step(f"running Codex CLI: bin={args.codex_bin}, model={model_label}, reasoning={reasoning_label}")
        output = run_codex(
            codex_bin=args.codex_bin,
            codex_model=codex_model,
            reasoning_effort=reasoning_effort,
            codex_args=list(args.codex_arg),
            prompt=prompt,
            timeout=args.codex_timeout,
            log_path=log_path,
        )
        log_step(f"Codex CLI finished; parsing output from {log_path}")
        payload = extract_json_object(output)
        codex_log_label = log_path
    raw_item_count = len(payload.get("items") or [])
    log_step(f"parsed payload JSON: raw_items={raw_item_count}")
    payload = select_review_items(payload, preference_profile=audience_profile, limit=review_limit)
    log_step(f"ranked and selected review items: selected_items={len(payload.get('items') or [])}")
    parsed = validate_payload_dict(payload, config_path=config_path)
    log_step("payload passed local schema/config validation")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "topic": parsed.topic,
                "cadence": parsed.cadence,
                "period": parsed.period,
                "items": [item.raw or {} for item in parsed.items],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    log_step(f"wrote payload: {output_path}")
    log_step(f"wrote Codex log: {codex_log_label}")

    log_step("running validate-payload")
    validate_code = run_newsbot_command(["validate-payload", "--input", str(output_path)])
    if validate_code != 0:
        log_step("validate-payload failed; not updating last-run state")
        return validate_code
    if args.isolated_dry_run:
        log_step("isolated dry-run: successful-run state was not updated")
    else:
        write_last_run(run_state_path(args.topic, args.cadence))
        log_step(f"updated last-run state: {run_state_path(args.topic, args.cadence)}")

    if args.validate_only or not (args.submit_review or args.submit_dry_run):
        log_step("done; submit-review was not requested")
        return 0

    submit_args = ["submit-review", "--input", str(output_path)]
    if args.submit_dry_run:
        submit_args.append("--dry-run")
    log_step("running submit-review")
    result = run_newsbot_command(submit_args)
    log_step("finished Codex payload generation workflow")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
