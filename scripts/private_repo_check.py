#!/usr/bin/env python3
from __future__ import annotations

import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_FILE_BYTES = 3 * 1024 * 1024
MAX_PRINTED_FINDINGS = 100
TEXT_SUFFIXES = {
    ".bak",
    ".cfg",
    ".conf",
    ".csv",
    ".env",
    ".example",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
SPECIAL_TEXT_NAMES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "Dockerfile",
    "LICENSE",
}
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|bearer[_-]?token|"
    r"bot[_-]?token|client[_-]?secret|password|private[_-]?key)"
)
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE)),
    (
        "Discord webhook",
        re.compile(
            r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+",
            re.IGNORECASE,
        ),
    ),
    (
        "sensitive URL parameter",
        re.compile(
            r"(?i)[?&](?:access_token|api_?key|apikey|key|secret|token)="
            r"(?!<redacted>|%3Credacted%3E|\{\{)[^&\s\"']{8,}"
        ),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|bearer[_-]?token|"
            r"bot[_-]?token|client[_-]?secret|password|private[_-]?key)"
            r"[ \t]*[=:][ \t]*['\"]?[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
]
HIGH_ENTROPY_ASSIGNMENT = re.compile(
    r"(?i)([A-Za-z_][A-Za-z0-9_-]*(?:key|secret|token|password)[A-Za-z0-9_-]*)"
    r"[ \t]*[=:][ \t]*['\"]?([A-Za-z0-9._~+/=-]{24,})"
)
FORBIDDEN_TRACKED_PATH_PATTERNS = [
    re.compile(r"^(COMMIT_EDITMSG|FETCH_HEAD|HEAD|ORIG_HEAD|index|description)$"),
    re.compile(r"^(objects|refs|logs|hooks|info)/"),
    re.compile(r"^\.env$"),
    re.compile(r"^(analysis|data|logs|payloads|reports|security-scans)/"),
    re.compile(r".*\.bak(?:[./_].*)?$"),
    re.compile(r".*\.(?:sqlite|sqlite3|db)$"),
]


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def candidate_files() -> list[str]:
    result = git("ls-files", "--cached", "--others", "--exclude-standard")
    return [line for line in result.stdout.splitlines() if line]


def tracked_files() -> list[str]:
    return [line for line in git("ls-files").stdout.splitlines() if line]


def is_forbidden_path(path: str) -> bool:
    return any(
        pattern.fullmatch(path) or pattern.match(path)
        for pattern in FORBIDDEN_TRACKED_PATH_PATTERNS
    )


def is_text_candidate(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.suffix.lower() in TEXT_SUFFIXES
        or candidate.name in SPECIAL_TEXT_NAMES
    )


def decode_text(body: bytes) -> str | None:
    if len(body) > MAX_TEXT_FILE_BYTES or b"\0" in body:
        return None
    return body.decode("utf-8", errors="ignore")


def shannon_entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def scan_text(text: str, live_secret_values: set[str]) -> list[str]:
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    for match in HIGH_ENTROPY_ASSIGNMENT.finditer(text):
        value = match.group(2)
        if shannon_entropy(value) >= 4.0:
            findings.append("high-entropy credential assignment")
            break
    if any(secret in text for secret in live_secret_values):
        findings.append("value matching a live local credential")
    return sorted(set(findings))


def local_live_secret_values() -> set[str]:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return set()
    values: set[str] = set()
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if SECRET_KEY_PATTERN.search(key) and len(value) >= 8:
            values.add(value)
    return values


def scan_worktree(live_secret_values: set[str]) -> list[str]:
    findings: list[str] = []
    for relative_path in candidate_files():
        if not is_text_candidate(relative_path):
            continue
        path = ROOT / relative_path
        if not path.is_file():
            continue
        text = decode_text(path.read_bytes())
        if text is None:
            continue
        for label in scan_text(text, live_secret_values):
            findings.append(f"worktree:{relative_path}: {label}")
    return findings


def history_objects() -> list[tuple[str, str, str]]:
    commits = [line for line in git("rev-list", "--all").stdout.splitlines() if line]
    objects: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for commit in commits:
        tree = git("ls-tree", "-r", commit).stdout.splitlines()
        for entry in tree:
            metadata, separator, path = entry.partition("\t")
            if not separator:
                continue
            parts = metadata.split()
            if len(parts) != 3 or parts[1] != "blob":
                continue
            blob = parts[2]
            key = (blob, path)
            if key not in seen:
                seen.add(key)
                objects.append((commit, blob, path))
    return objects


def scan_history(live_secret_values: set[str]) -> list[str]:
    findings: list[str] = []
    scanned_blobs: dict[str, list[str]] = {}
    for commit, blob, path in history_objects():
        short_commit = commit[:12]
        if is_forbidden_path(path):
            findings.append(f"history:{short_commit}:{path}: forbidden tracked path")
        if not is_text_candidate(path):
            continue
        labels = scanned_blobs.get(blob)
        if labels is None:
            body = subprocess.run(
                ["git", "cat-file", "blob", blob],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            text = decode_text(body)
            labels = scan_text(text, live_secret_values) if text is not None else []
            scanned_blobs[blob] = labels
        for label in labels:
            findings.append(f"history:{short_commit}:{path}: {label}")
    return findings


def print_findings(title: str, findings: list[str]) -> None:
    print(title)
    for finding in findings[:MAX_PRINTED_FINDINGS]:
        print(f"- {finding}")
    remaining = len(findings) - MAX_PRINTED_FINDINGS
    if remaining > 0:
        print(f"- ... and {remaining} more")


def main() -> int:
    findings: list[str] = []
    forbidden_current = [path for path in tracked_files() if is_forbidden_path(path)]
    findings.extend(f"tracked:{path}: forbidden tracked path" for path in forbidden_current)

    live_secret_values = local_live_secret_values()
    findings.extend(scan_worktree(live_secret_values))
    findings.extend(scan_history(live_secret_values))
    findings = sorted(set(findings))

    if findings:
        print_findings("Public release findings:", findings)
        return 1

    print(
        "Public repo check OK: current files and all reachable Git history contain "
        "no detected secrets or forbidden tracked paths."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
