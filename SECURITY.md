# Security Policy

## Secrets

Never commit live credentials. Use `.env` locally and GitHub Actions Secrets or deployment secrets in hosted environments.

Sensitive values include:

- `DISCORD_BOT_TOKEN`
- `DISCORD_WEBHOOK_URL`
- `X_BEARER_TOKEN`
- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`
- OpenAI/Codex credentials stored by the Codex CLI

This repository should contain `.env.example` only. Do not commit `.env`, SQLite databases, generated payloads, reports, Codex logs, or security scan outputs.

## Data Stored Locally

SQLite stores draft text, source URLs, canonical dedupe keys, reviewer actions, delivery events, and reader feedback counts. Treat the database as operational data.

## Public Release Checklist

Before pushing a public repository:

```bash
python scripts/private_repo_check.py
git ls-files .env data payloads reports security-scans "*.sqlite" "*.sqlite3" "*.db"
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

Expected:

- `private_repo_check.py` reports no obvious secrets.
- `git ls-files` does not list `.env`, generated data, payloads, reports, scans, or database files.
- Tests and compile checks pass.

## Operational Guidance

- Keep public publishing human-approved through Discord `Publish Digest`.
- Treat Codex-generated payloads as untrusted until validation and review.
- Restrict reviewer controls with `DISCORD_REVIEWER_USER_IDS`.
- Set `DISCORD_ALLOWED_GUILD_ID` when the bot should operate in only one server.
- Rotate credentials immediately if they are accidentally committed.

Removing a leaked secret from a later commit is not enough; revoke and rotate it at the provider.
