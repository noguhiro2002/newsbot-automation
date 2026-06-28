from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "data",
    "payloads",
    "reports",
    "security-scans",
}
SECRET_PATTERNS = [
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+", re.IGNORECASE),
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|api[_-]?secret|access[_-]?token|bearer[_-]?token|bot[_-]?token)[ \t]*=[ \t]*['\"]?[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)(discord_webhook_url|discord_bot_token|openai_api_key)[ \t]*=[ \t]*['\"]?\S{16,}"),
]
FORBIDDEN_TRACKED_PATH_PATTERNS = [
    re.compile(r"^(COMMIT_EDITMSG|FETCH_HEAD|HEAD|ORIG_HEAD|index|description)$"),
    re.compile(r"^(objects|refs|logs|hooks|info)/"),
    re.compile(r"^\.env$"),
    re.compile(r"^(data|payloads|reports|security-scans)/"),
    re.compile(r".*\.bak(?:[./_].*)?$"),
    re.compile(r".*\.(?:sqlite|sqlite3|db)$"),
]


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)


def is_text_candidate(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {".env.example", ".gitignore"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def forbidden_tracked_paths(paths: list[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if any(pattern.fullmatch(path) or pattern.match(path) for pattern in FORBIDDEN_TRACKED_PATH_PATTERNS):
            findings.append(path)
    return findings


def main() -> int:
    forbidden_paths = forbidden_tracked_paths(tracked_files())
    if forbidden_paths:
        print("Forbidden tracked paths found:")
        for finding in forbidden_paths:
            print(f"- {finding}")
        return 1

    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or should_skip(path) or not is_text_candidate(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(str(path.relative_to(ROOT)))
                break

    if findings:
        print("Potential secrets found:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Private repo check OK: no obvious secrets or forbidden tracked paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
