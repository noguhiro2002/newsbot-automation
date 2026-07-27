#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def process_command_lines(proc_root: Path = Path("/proc")) -> list[str]:
    commands: list[str] = []
    for path in proc_root.glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except (OSError, PermissionError):
            continue
        if command:
            commands.append(command)
    return commands


def main(argv: list[str] | None = None) -> int:
    terms = list(argv if argv is not None else sys.argv[1:])
    if not terms:
        print("usage: container_healthcheck.py TERM [TERM ...]", file=sys.stderr)
        return 2
    healthy = any(
        all(term in command for term in terms)
        for command in process_command_lines()
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
