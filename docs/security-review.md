# Security Review

Date: 2026-06-07

## Scope

Repository-wide review of the public-release `newsbot` codebase:

- Codex CLI payload generation
- Discord Bot review and publish workflow
- SQLite draft/review/feedback storage
- Legacy webhook/X notification helpers
- Local secret scanning and release-readiness files

## Threat Model Summary

Important assets:

- `DISCORD_BOT_TOKEN`
- `DISCORD_REVIEW_CHANNEL_ID`
- `DISCORD_PUBLISH_CHANNEL_ID`
- `DISCORD_REVIEWER_USER_IDS`
- Optional `DISCORD_WEBHOOK_URL`
- Optional X credentials
- Codex CLI local authentication state
- Local SQLite operational data

Trust boundaries:

- Codex-generated payloads are untrusted until validation and human review.
- Discord interactions are accepted only from configured reviewers, and optionally one guild.
- Public publishing requires a reviewer to click `Publish Digest`.
- SQLite data, generated payloads, Codex logs, and reports are local operational data and must not be committed.

## Checks Performed

- Confirmed SQL access uses parameterized queries.
- Confirmed reviewer controls check `DISCORD_REVIEWER_USER_IDS`.
- Confirmed optional guild restriction uses `DISCORD_ALLOWED_GUILD_ID`.
- Confirmed generated review/publish content suppresses Discord link previews.
- Confirmed source-check and payload-generation subprocess calls invoke Codex CLI without shell command interpolation.
- Confirmed `.gitignore` excludes `.env`, generated DBs, payloads, reports, scans, and backup files.
- Confirmed `scripts/private_repo_check.py` scans common text/config extensions for obvious credentials.

## Current Findings

No high- or critical-severity vulnerabilities were found in the reviewed code paths.

## Residual Risks

- Codex-generated article text can include inaccurate or adversarial web content. Keep human review before publishing.
- Codex CLI credentials live outside this repository and must be protected by the host OS user account.
- A compromised host or CI runner can read runtime environment variables.
- SQLite stores message text, URLs, review history, and feedback. Treat it as operational data, not source code.
- Legacy webhook/X delivery remains available. Prefer the Discord Bot review pipeline for public deployments.

## Public Release Checklist

Run before publishing:

```bash
python scripts/private_repo_check.py
git ls-files .env data payloads reports security-scans "*.sqlite" "*.db"
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

Expected:

- Secret scan prints no findings.
- `git ls-files` does not list `.env`, generated data, payloads, reports, scans, or database files.
- Compile and test checks pass.

If a live secret was ever committed, revoke and rotate it at the provider. Removing it from a later commit is not enough.
