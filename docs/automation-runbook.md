# Automation Runbook

This runbook describes the recommended public operation model: cron starts Codex CLI jobs, reviewers approve drafts in Discord, and the bot publishes only after final approval.

## Recommended Schedule

- News search and review submission: run `scripts/generate_payload_openai.py` with cron.
- Final weekly review notification: run `python -m newsbot.cli prepare-weekly-publish` with cron or manually.
- Public publish: click `Publish Digest` in Discord after final review.

Example review-candidate cron:

```cron
0 8 * * 1 cd /path/to/newsbot-automation && /path/to/venv/bin/python scripts/generate_payload_openai.py --topic lab_automation --submit-review >> logs/newsbot-codex.log 2>&1
```

Example final-review cron:

```cron
0 16 * * 5 cd /path/to/newsbot-automation && /path/to/venv/bin/python -m newsbot.cli prepare-weekly-publish >> logs/newsbot-weekly.log 2>&1
```

Cron often has a limited `PATH`. Use absolute Python paths and set `NEWSBOT_CODEX_BIN` in `.env` if cron cannot find `codex`.

## Runtime Processes

Start and keep the Discord bot running:

```bash
python -m newsbot.cli run-discord-bot
```

The bot handles reviewer controls, edit modals, source checks, final ordering, publishing, and reader feedback.

## Payload Contract

Codex CLI generation writes JSON payloads under `payloads/`. Manual payloads can use the same shape:

```json
{
  "topic": "lab_automation",
  "cadence": "weekly",
  "period": "2026-06-01 to 2026-06-07",
  "items": [
    {
      "title": "News title",
      "url": "https://example.com/source",
      "summary": "One-sentence summary",
      "source": "Publisher",
      "why_it_matters": "Why reviewers should care"
    }
  ]
}
```

Validate a manual payload before review submission:

```bash
python -m newsbot.cli validate-payload --input samples/lab_automation_weekly.sample.json
python -m newsbot.cli submit-review --input samples/lab_automation_weekly.sample.json --dry-run
```

## Safety Rules

- Do not automate public publishing directly from cron.
- Keep human review in Discord before `Publish Digest`.
- Treat Codex-generated payloads as untrusted until validation and review.
- Keep `.env`, `data/`, `payloads/`, `reports/`, and SQLite files out of Git.

## Manual Test Checklist

- Codex CLI can run non-interactively with `codex exec --ephemeral "Say OK"`.
- `generate_payload_openai.py --skip-codex` writes a prompt without calling Codex.
- `generate_payload_openai.py --submit-review` posts review candidates.
- `prepare-weekly-publish` posts final review candidates.
- Final digest publishes only after all candidates are judged and `Publish Digest` is clicked.
- Published detail messages include `👍 Interested (0)` buttons.
