from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_env import codex_subprocess_env
from .config import PROJECT_ROOT


DEFAULT_CODEX_TIMEOUT_SECONDS = 60 * 10
MAX_ERROR_DETAIL_CHARS = 800


@dataclass(frozen=True)
class SourceCheckResult:
    status: str
    current_url_reachable: bool | None
    replacement_url: str
    reason: str


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
                value = json.loads(stripped[start : index + 1])
                if not isinstance(value, dict):
                    raise ValueError("Extracted JSON was not an object")
                return value
    raise ValueError("Could not find a complete JSON object in Codex output")


def build_source_check_prompt(*, title: str, current_url: str, source_name: str = "") -> str:
    return f"""
Check whether this news source URL is still valid. If it is expired, redirected to an unrelated page, paywalled teaser only, or returns a not-found/error page, find the best replacement URL for the same article or the same official announcement.

Title: {title}
Source name: {source_name or "unknown"}
Current URL: {current_url}

Return exactly one JSON object and no surrounding prose:
{{
  "status": "ok|replace|unknown",
  "current_url_reachable": true,
  "replacement_url": "",
  "reason": "short explanation"
}}

Rules:
- Use "ok" when the current URL is valid enough to keep.
- Use "replace" only when you found a better URL for the same article/event.
- Use "unknown" when you cannot verify confidently.
- replacement_url must be empty unless status is "replace".
- Prefer official/company/research-institute URLs over syndicated copies.
""".strip()


def parse_source_check_result(value: dict[str, Any]) -> SourceCheckResult:
    status = str(value.get("status") or "unknown").strip().lower()
    if status not in {"ok", "replace", "unknown"}:
        status = "unknown"
    replacement_url = str(value.get("replacement_url") or "").strip()
    if status != "replace":
        replacement_url = ""
    reachable_raw = value.get("current_url_reachable")
    reachable = reachable_raw if isinstance(reachable_raw, bool) else None
    return SourceCheckResult(
        status=status,
        current_url_reachable=reachable,
        replacement_url=replacement_url,
        reason=str(value.get("reason") or "").strip(),
    )


def check_source_with_codex(
    *,
    title: str,
    current_url: str,
    source_name: str = "",
    codex_bin: str | None = None,
    codex_model: str | None = None,
    timeout: int = DEFAULT_CODEX_TIMEOUT_SECONDS,
) -> SourceCheckResult:
    codex_command = codex_bin or os.getenv("NEWSBOT_CODEX_BIN") or "codex"
    model = codex_model or os.getenv("NEWSBOT_CODEX_MODEL") or ""
    prompt = build_source_check_prompt(title=title, current_url=current_url, source_name=source_name)
    model_args = ["--model", model] if model else []
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".md") as handle:
        output_path = Path(handle.name)

    command = [
        codex_command,
        "--search",
        "exec",
        "--ephemeral",
        "--output-last-message",
        str(output_path),
        *model_args,
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
    output = output_path.read_text(encoding="utf-8", errors="replace").strip()
    output_path.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > MAX_ERROR_DETAIL_CHARS:
            detail = detail[:MAX_ERROR_DETAIL_CHARS].rstrip() + "..."
        raise RuntimeError(f"codex exec failed with exit code {result.returncode}: {detail}")
    return parse_source_check_result(extract_json_object(output or result.stdout))
