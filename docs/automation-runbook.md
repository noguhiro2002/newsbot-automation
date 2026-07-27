# Automation Runbook

This runbook describes the recommended public operation model: cron starts Codex CLI jobs, reviewers approve drafts in Discord, and the bot publishes only after final approval.

## Recommended Schedule

- News search and review submission: run the `generate-review` scheduled task.
- Final weekly review notification: run the `prepare-weekly` scheduled task or prepare it manually.
- Public publish: click `Publish Digest` in Discord after final review.

For a local installation, register the current schedules through the included installer:

```bash
sudo scripts/install_cron_jobs.sh \
  --app-dir /opt/newsbot-automation \
  --user newsbot
```

By default, candidate generation is triggered every day at 08:00 JST and runs only if the previous successful run was at least two days ago. Final-review preparation runs every Monday at 08:00 JST. Configure the `NEWSBOT_GENERATE_REVIEW_*` and `NEWSBOT_PREPARE_WEEKLY_*` values in `.env`, then rerun the installer.

For Docker Compose, use its scheduler instead of host cron:

```bash
docker compose --profile scheduler up -d
```

Do not run host cron and the Docker scheduler together.

## Runtime Processes

Start and keep the Discord bot running:

```bash
python -m newsbot.cli run-discord-bot
```

The bot handles reviewer controls, edit modals, source checks, final ordering, publishing, and reader feedback.

For a local Linux installation, keep it running and enable it at OS boot with:

```bash
sudo scripts/install_systemd_service.sh \
  --env venv \
  --app-dir /opt/newsbot-automation \
  --user newsbot \
  --group newsbot
```

For Docker Compose, `discord-bot` uses `restart: unless-stopped`. Enable Docker Engine at boot and create the service once:

```bash
sudo systemctl enable --now containerd.service docker.service
docker compose up -d discord-bot
```

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
- Keep `.env` and SQLite files mode `0600`, and runtime directories mode `0700`.
- Configure registered NCBI tool/email values before PubMed use. Keep
  `NCBI_API_KEY` private and follow the documented provider rate limits.
- Codex subprocesses intentionally receive only allowlisted runtime/OpenAI
  variables; Discord, X, and NCBI credentials are excluded.
- Codex Web search is required and the application does not force a sandbox.
  Run generation under a dedicated low-privilege account or hardened container,
  and retain human source review.

## Manual Test Checklist

- Codex CLI can run non-interactively with `codex exec --ephemeral "Say OK"`.
- `generate_payload_openai.py --skip-codex` writes a prompt without calling Codex.
- `generate_payload_openai.py --submit-review` posts review candidates.
- `prepare-weekly-publish` posts final review candidates.
- Final digest publishes only after all candidates are judged and `Publish Digest` is clicked.
- Published detail messages include `👍 Interested (0)` buttons.
