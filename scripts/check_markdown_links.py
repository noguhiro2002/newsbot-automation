#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)")
HTML_LINK = re.compile(r"(?:href|src)=['\"]([^'\"]+)['\"]", re.IGNORECASE)


def repository_markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def local_target(raw_target: str, source: Path) -> Path | None:
    target = raw_target.strip().strip("<>")
    if not target or target.startswith("#"):
        return None
    parts = urllib.parse.urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    path = urllib.parse.unquote(parts.path)
    if not path:
        return None
    return (source.parent / path).resolve()


def main() -> int:
    broken: list[str] = []
    root_resolved = ROOT.resolve()
    for source in repository_markdown_files():
        text = source.read_text(encoding="utf-8", errors="replace")
        targets = MARKDOWN_LINK.findall(text) + HTML_LINK.findall(text)
        for raw_target in targets:
            target = local_target(raw_target, source)
            if target is None:
                continue
            try:
                target.relative_to(root_resolved)
            except ValueError:
                broken.append(
                    f"{source.relative_to(ROOT)}: target escapes repository: {raw_target}"
                )
                continue
            if not target.exists():
                broken.append(
                    f"{source.relative_to(ROOT)}: missing target: {raw_target}"
                )
    if broken:
        print("Broken local Markdown links:")
        for finding in broken:
            print(f"- {finding}")
        return 1
    print("Markdown link check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
