from __future__ import annotations

import ipaddress
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .codex_env import codex_subprocess_env
from .config import PROJECT_ROOT
from .db import NewsbotStore
from .models import canonicalize_url, stable_json
from .source_check import extract_json_object


DEFAULT_MISSED_ITEM_MODEL = "gpt-5.6-luna"
DEFAULT_MISSED_ITEM_REASONING_EFFORT = "high"
DEFAULT_MISSED_ITEM_TIMEOUT_SECONDS = 30 * 60
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
VALID_PHASES = {"phase_1", "phase_2", "phase_3", "phase_4", "phase_5"}
VALID_CONTENT_KINDS = {
    "news",
    "event_announcement",
    "event_occurrence",
    "post_event_report",
    "official_resurfacing",
}
MAX_ERROR_DETAIL_CHARS = 800


@dataclass(frozen=True)
class MissedItemResearchResult:
    supplied_url: str
    canonical_url: str
    title: str
    organization: str
    domain: str
    phase: str
    published_date: str
    content_kind: str
    relevant: bool
    relevance_summary: str
    evidence: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def missed_item_model() -> str:
    return os.getenv("NEWSBOT_MISSED_ITEM_MODEL", "").strip() or DEFAULT_MISSED_ITEM_MODEL


def missed_item_reasoning_effort() -> str:
    effort = (
        os.getenv("NEWSBOT_MISSED_ITEM_REASONING_EFFORT", "").strip()
        or DEFAULT_MISSED_ITEM_REASONING_EFFORT
    )
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(
            "NEWSBOT_MISSED_ITEM_REASONING_EFFORT must be one of: "
            + ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
            + f"; got: {effort}"
        )
    return effort


def missed_item_timeout_seconds() -> int:
    raw = os.getenv("NEWSBOT_MISSED_ITEM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MISSED_ITEM_TIMEOUT_SECONDS
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(
            "NEWSBOT_MISSED_ITEM_TIMEOUT_SECONDS must be an integer of 1 or greater; "
            f"got: {raw}"
        )
    return int(raw)


def validate_public_url(url: str) -> str:
    value = url.strip()
    if len(value) > 2048:
        raise ValueError("URL must be 2048 characters or fewer")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be an absolute http or https URL")
    if parts.username or parts.password:
        raise ValueError("URLs containing credentials are not allowed")
    hostname = parts.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Local URLs are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("Private, loopback, and link-local URLs are not allowed")
    return value


def build_missed_item_prompt(url: str) -> str:
    return f"""
You are investigating one editor-supplied URL that the Lab Automation news pipeline missed.

Supplied URL: {url}

Open the supplied page, verify the canonical page and publication date, and use web search to find only the related official announcement or primary evidence needed to understand the same event. Determine whether it concretely relates to laboratory automation, autonomous experimentation, scientific experiment workflows, research equipment/data infrastructure, programmable experimental environments, or closely related Lab Automation operations.

Classify it into exactly one discovery phase:
- phase_1: Japanese official/public research infrastructure
- phase_2: PR/newswire discovery with official URL resolution
- phase_3: international official/company/research/standards news
- phase_4: paper or preprint
- phase_5: relevant cross-phase item that does not fit the above cleanly

Return exactly one JSON object and no markdown:
{{
  "canonical_url": "https://...",
  "title": "canonical title",
  "organization": "issuing or responsible organization",
  "phase": "phase_1|phase_2|phase_3|phase_4|phase_5",
  "published_date": "YYYY-MM-DD or empty when unverifiable",
  "content_kind": "news|event_announcement|event_occurrence|post_event_report|official_resurfacing",
  "relevant": true,
  "relevance_summary": "concise explanation of the concrete Lab Automation connection",
  "evidence": "specific facts verified from the page and related primary source",
  "confidence": 0.0
}}

Rules:
- Do not mark an item relevant merely because it contains AI, robotics, automation, or science terms.
- General LLM news, generic market reports, ordinary seminars, and simple exhibition attendance are not relevant.
- Prefer the issuing organization's canonical URL over a syndicated URL.
- `published_date` is the canonical page publication/update date, never an event date.
- Set relevant=false when the page cannot be verified or lacks a concrete scientific experiment/workflow connection.
- Confidence must be between 0.0 and 1.0.
""".strip()


def parse_missed_item_result(value: dict[str, Any], supplied_url: str) -> MissedItemResearchResult:
    canonical_url = validate_public_url(str(value.get("canonical_url") or supplied_url))
    title = str(value.get("title") or "").strip()
    organization = str(value.get("organization") or "").strip()
    phase = str(value.get("phase") or "").strip()
    content_kind = str(value.get("content_kind") or "news").strip()
    if not title:
        raise ValueError("Missed-item research result must include title")
    if not organization:
        raise ValueError("Missed-item research result must include organization")
    if phase not in VALID_PHASES:
        raise ValueError(f"Missed-item research result has invalid phase: {phase}")
    if content_kind not in VALID_CONTENT_KINDS:
        raise ValueError(f"Missed-item research result has invalid content_kind: {content_kind}")
    relevant = value.get("relevant")
    if not isinstance(relevant, bool):
        raise ValueError("Missed-item research result relevant must be boolean")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Missed-item research result confidence must be numeric") from exc
    if confidence < 0 or confidence > 1:
        raise ValueError("Missed-item research result confidence must be between 0.0 and 1.0")
    published_date = str(value.get("published_date") or "").strip()
    if published_date:
        try:
            date.fromisoformat(published_date)
        except ValueError as exc:
            raise ValueError("Missed-item research result published_date must be YYYY-MM-DD or empty") from exc
    relevance_summary = str(value.get("relevance_summary") or "").strip()
    evidence = str(value.get("evidence") or "").strip()
    if relevant and (not relevance_summary or not evidence):
        raise ValueError("Relevant missed-item research results must include relevance_summary and evidence")
    return MissedItemResearchResult(
        supplied_url=supplied_url,
        canonical_url=canonicalize_url(canonical_url),
        title=title,
        organization=organization,
        domain=(urlsplit(canonical_url).hostname or "").casefold(),
        phase=phase,
        published_date=published_date,
        content_kind=content_kind,
        relevant=relevant,
        relevance_summary=relevance_summary,
        evidence=evidence,
        confidence=confidence,
    )


def research_missed_item_with_codex(
    url: str,
    *,
    codex_bin: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout: int | None = None,
) -> MissedItemResearchResult:
    supplied_url = validate_public_url(url)
    resolved_model = model or missed_item_model()
    resolved_effort = reasoning_effort or missed_item_reasoning_effort()
    if resolved_effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(f"Unsupported missed-item reasoning effort: {resolved_effort}")
    resolved_timeout = timeout if timeout is not None else missed_item_timeout_seconds()
    if resolved_timeout < 1:
        raise ValueError("Missed-item timeout must be 1 or greater")
    command_bin = codex_bin or os.getenv("NEWSBOT_CODEX_BIN", "").strip() or "codex"
    prompt = build_missed_item_prompt(supplied_url)
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".json") as handle:
        output_path = Path(handle.name)
    command = [
        command_bin,
        "--search",
        "--config",
        f'model_reasoning_effort="{resolved_effort}"',
        "exec",
        "--ephemeral",
        "--output-last-message",
        str(output_path),
        "--model",
        resolved_model,
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=resolved_timeout,
            check=False,
            env=codex_subprocess_env(),
        )
        output = output_path.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        output_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > MAX_ERROR_DETAIL_CHARS:
            detail = detail[:MAX_ERROR_DETAIL_CHARS].rstrip() + "..."
        raise RuntimeError(f"codex missed-item research failed with exit code {completed.returncode}: {detail}")
    return parse_missed_item_result(extract_json_object(output or completed.stdout), supplied_url)


def record_researched_missed_item(
    store: NewsbotStore,
    result: MissedItemResearchResult,
    *,
    reviewer_user_id: str,
    topic: str = "lab_automation",
    cadence: str = "weekly",
) -> str:
    if not result.relevant:
        raise ValueError("The supplied URL was not verified as relevant; it was not registered")
    note = stable_json(
        {
            "research_model": missed_item_model(),
            "published_date": result.published_date,
            "content_kind": result.content_kind,
            "relevance_summary": result.relevance_summary,
            "evidence": result.evidence,
            "confidence": result.confidence,
            "supplied_url": result.supplied_url,
        }
    )
    return store.record_editorial_judgment(
        decision="missed",
        reason_code="search_miss",
        reviewer_user_id=reviewer_user_id,
        topic=topic,
        cadence=cadence,
        canonical_url=result.canonical_url,
        title=result.title,
        phase=result.phase,
        note=note,
        organization=result.organization,
        domain=result.domain,
    )


def format_missed_item_result(result: MissedItemResearchResult, *, judgment_id: str = "", dry_run: bool = False) -> str:
    status = "Dry run: not registered" if dry_run else f"Registered: {judgment_id}"
    return "\n".join(
        [
            status,
            f"Relevant: {result.relevant}",
            f"Title: {result.title}",
            f"Organization: {result.organization}",
            f"Phase: {result.phase}",
            f"Published: {result.published_date or 'unverified'}",
            f"Confidence: {result.confidence:.2f}",
            f"Canonical URL: {result.canonical_url}",
            f"Why: {result.relevance_summary or '(none)'}",
        ]
    )
