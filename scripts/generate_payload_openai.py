from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsbot.config import DEFAULT_CONFIG_PATH, load_config, resolve_project_path
from newsbot.db import DEFAULT_DB_PATH, NewsbotStore
from newsbot.models import NewsPayload
from newsbot.ranking import REVIEW_MAX_ITEMS, rank_news_items


DEFAULT_CODEX_TIMEOUT_SECONDS = 60 * 20
PROMPT_TEMPLATE_DIR = PROJECT_ROOT / "prompts"
LAB_AUTOMATION_TOPIC = "lab_automation"
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}


@dataclass(frozen=True)
class PhaseDefinition:
    key: str
    phase: str
    label: str
    prompt_file: str


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
        ),
        PhaseDefinition(
            key="phase_2",
            phase="phase_2_pr_resolution",
            label="Phase 2 Agent: PR探索 + 公式URL解決",
            prompt_file="phase_2_pr_resolution.md",
        ),
        PhaseDefinition(
            key="phase_3",
            phase="phase_3_global_official",
            label="Phase 3 Agent: 海外公式・企業・標準化団体探索",
            prompt_file="phase_3_global_official.md",
        ),
        PhaseDefinition(
            key="phase_4",
            phase="phase_4_papers",
            label="Phase 4 Agent: 論文・preprint探索",
            prompt_file="phase_4_papers.md",
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


def lab_automation_prompt_dir(template_dir: Path = PROMPT_TEMPLATE_DIR) -> Path:
    return template_dir / LAB_AUTOMATION_TOPIC


def validate_phase_output(value: dict[str, Any], *, expected_phase: str) -> dict[str, Any]:
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
    return value


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
    template_dir: Path = PROMPT_TEMPLATE_DIR,
) -> tuple[str, Path]:
    template_path = lab_automation_prompt_dir(template_dir) / phase.prompt_file
    if not template_path.exists():
        raise FileNotFoundError(f"Missing phase prompt template: {template_path}")
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
            "{{PHASE_KEY}}": phase.key,
            "{{PHASE_NAME}}": phase.phase,
            "{{PHASE_LABEL}}": phase.label,
        },
    )
    return prompt, template_path


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
    return prompt, template_path


def phase_artifact_path(output_path: Path, phase_key: str, suffix: str) -> Path:
    return output_path.with_suffix(f".{phase_key}{suffix}")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def format_preference_profile(profile: dict[str, Any] | None) -> str:
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


def run_codex(
    *,
    codex_bin: str,
    codex_model: str,
    reasoning_effort: str,
    codex_args: list[str],
    prompt: str,
    timeout: int,
    log_path: Path,
) -> str:
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".md") as handle:
        output_last_message = Path(handle.name)

    model_args = ["--model", codex_model] if codex_model else []
    reasoning_args = ["--config", f'model_reasoning_effort="{reasoning_effort}"'] if reasoning_effort else []
    exec_args = [arg for arg in codex_args if arg != "--search"]
    command = [
        codex_bin,
        "--search",
        *reasoning_args,
        "exec",
        "--ephemeral",
        "--output-last-message",
        str(output_last_message),
        *model_args,
        *exec_args,
        prompt,
    ]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "COMMAND:",
                " ".join(command[:-1] + ["<prompt>"]),
                "",
                f"EXIT_CODE: {result.returncode}",
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
    return output or result.stdout


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


def run_lab_automation_multi_agent(
    *,
    args: argparse.Namespace,
    period: str,
    review_limit: int,
    output_path: Path,
    config_path: Path,
    config_raw: dict[str, Any],
    preference_profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    phases = lab_automation_phase_definitions()
    phase_models = parse_phase_models(list(args.phase_model))
    phase_reasoning = parse_phase_reasoning(list(args.phase_reasoning))
    default_model = default_codex_model(args.codex_model)
    default_effort = default_reasoning_effort(args.codex_reasoning_effort)
    codex_args = list(args.codex_arg)

    log_step("using lab_automation multi-agent workflow")
    phase_outputs: list[dict[str, Any]] = []

    if args.skip_codex:
        for phase in phases:
            prompt, template_path = build_phase_prompt(
                phase=phase,
                topic=args.topic,
                cadence=args.cadence,
                period=period,
                max_items=review_limit,
                output_path=output_path,
                config_path=config_path,
                preference_profile=preference_profile,
            )
            prompt_path = phase_artifact_path(output_path, phase.key, ".prompt.txt")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            log_step(f"wrote {phase.key} prompt from {template_path}: {prompt_path}")
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
        prompt, template_path = build_phase_prompt(
            phase=phase,
            topic=args.topic,
            cadence=args.cadence,
            period=period,
            max_items=review_limit,
            output_path=output_path,
            config_path=config_path,
            preference_profile=preference_profile,
        )
        prompt_path = phase_artifact_path(output_path, phase.key, ".prompt.txt")
        json_path = phase_artifact_path(output_path, phase.key, ".json")
        log_path = phase_artifact_path(output_path, phase.key, ".codex.log")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        model = phase_model_for(phase, phase_models, default_model)
        reasoning_effort = phase_reasoning_for(phase, phase_reasoning, default_effort)
        model_label = model or "(Codex CLI default)"
        reasoning_label = reasoning_effort or "(Codex CLI default)"
        log_step(f"running {phase.key}: template={template_path}, model={model_label}, reasoning={reasoning_label}")
        output = run_codex(
            codex_bin=args.codex_bin,
            codex_model=model,
            reasoning_effort=reasoning_effort,
            codex_args=codex_args,
            prompt=prompt,
            timeout=args.codex_timeout,
            log_path=log_path,
        )
        phase_json = validate_phase_output(extract_json_object(output), expected_phase=phase.phase)
        write_json(json_path, phase_json)
        phase_outputs.append(phase_json)
        log_step(f"{phase.key} finished: candidates={len(phase_json.get('candidates') or [])}, output={json_path}")

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
    master_output = run_codex(
        codex_bin=args.codex_bin,
        codex_model=master_model,
        reasoning_effort=master_reasoning_effort,
        codex_args=codex_args,
        prompt=master_prompt,
        timeout=args.codex_timeout,
        log_path=master_log_path,
    )
    payload = extract_json_object(master_output)
    write_json(master_json_path, payload)
    log_step(f"master builder finished: raw_items={len(payload.get('items') or [])}, output={master_json_path}")
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
    parser.add_argument("--submit-review", action="store_true", help="Submit the generated payload to Discord reviewer channel.")
    parser.add_argument("--submit-dry-run", action="store_true", help="Run submit-review with --dry-run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
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
    log_step(f"review candidate limit={review_limit}")
    store = NewsbotStore(args.db)
    store.init()
    log_step(f"initialized database: {store.path}")
    preference_profile = store.audience_preference_profile(topic=args.topic, cadence=args.cadence)
    log_step(f"loaded Interested preference profile: total_feedback={preference_profile.get('total_feedback', 0)}")
    use_multi_agent = args.topic == LAB_AUTOMATION_TOPIC and not args.single_agent
    if use_multi_agent:
        payload = run_lab_automation_multi_agent(
            args=args,
            period=period,
            review_limit=review_limit,
            output_path=output_path,
            config_path=config_path,
            config_raw=config.raw,
            preference_profile=preference_profile,
        )
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
    payload = select_review_items(payload, preference_profile=preference_profile, limit=review_limit)
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
