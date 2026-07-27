# Security Policy

## Supported Versions

Security fixes are provided for the latest release and the current `main`
branch. Older releases and private deployment forks are not supported unless
explicitly stated.

## Reporting a Vulnerability

Do not disclose credentials or vulnerability details in a public issue.

After this repository becomes public, use GitHub Security Advisories and the
repository’s **Report a vulnerability** button for a private report. If private
reporting is temporarily unavailable, open a public issue containing no
technical or secret details and ask the maintainer for a private contact method.

Include the affected version or commit, deployment method, reproducible impact,
and a minimal proof of concept. Remove all live credentials and personal data.

## Secrets

Never commit live credentials. Use `.env` locally and deployment secret stores
in hosted environments. Keep `.env` owner-readable only (`0600`).

Sensitive values include:

- `DISCORD_BOT_TOKEN`
- `DISCORD_WEBHOOK_URL`
- `NCBI_API_KEY`
- `X_BEARER_TOKEN`
- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`
- `OPENAI_API_KEY` when API-key authentication is used
- OpenAI/Codex credentials stored by the Codex CLI

Codex child processes receive an allowlisted environment and must not inherit
Discord, X, or NCBI credentials.

This repository should contain `.env.example` only. Do not commit `.env`,
SQLite databases, generated payloads, reports, Codex logs, API caches, or
security scan outputs.

## Data Stored Locally

SQLite stores draft text, source URLs, canonical dedupe keys, reviewer actions,
delivery events, Discord identifiers, and reader feedback counts. Treat the
database and all generated artifacts as operational data. Runtime directories
should be mode `0700`, and files should be mode `0600`.

## Public Release Checklist

Before making the repository public:

```bash
python scripts/private_repo_check.py
python scripts/check_markdown_links.py
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
docker compose config --quiet
```

Also run the dependency and container scans configured in GitHub Actions, and
inspect every remote branch and tag. Push only the intended public branch; do
not use `git push --all` or `git push --mirror`.

If a live secret was ever committed or included in a generated Codex prompt,
artifact, process argument, or log, revoke and rotate it. Removing it from a
later commit is not sufficient.

## Operational Guidance

- Keep public publishing human-approved through Discord `Publish Digest`.
- Treat Codex output and Web/API content as untrusted until validation and
  human review.
- Restrict reviewer controls with `DISCORD_REVIEWER_USER_IDS`.
- Set `DISCORD_ALLOWED_GUILD_ID` for a single-server deployment.
- Register NCBI tool/email values, observe API rate limits, and display the
  project’s [Legal Notices](docs/legal-notices.md) to operators.
