# Contributing

Thank you for contributing to Newsbot Automation.

## Development

Use Python 3.11 or later. The locked development setup uses
[uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen --all-extras
uv run python -m unittest discover -s tests
```

Before opening a pull request, run:

```bash
uv run python scripts/private_repo_check.py
uv run python scripts/check_markdown_links.py
uv run python -m compileall -q newsbot tests scripts
uv run python -m unittest discover -s tests
bash -n scripts/*.sh
docker compose config --quiet
```

Do not commit `.env`, API keys, Discord identifiers from a real deployment,
SQLite data, generated prompts or payloads, Codex credentials/events/logs,
reports, or API caches. Use clearly fake placeholders in tests.

Keep changes focused, document user-visible behavior, and add tests for new
logic. Security vulnerabilities must be reported through the private process
in [SECURITY.md](SECURITY.md), not through an issue.
