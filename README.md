# Newsbot Automation

<p align="center">
  <img src="docs/assets/newsbot-automation-logo.png" alt="Newsbot Automation logo" width="180">
</p>

Newsbot Automation is a Python + Discord workflow for collecting topic-based news with Codex CLI, reviewing drafts with a Discord bot, and publishing a weekly digest only after human approval.

Japanese documentation is available in [README.ja.md](README.ja.md).

## Architecture

![Newsbot Automation architecture](docs/assets/newsbot-architecture.png)

The pipeline starts with Codex CLI collecting topic-scoped news and producing a payload JSON. Newsbot validates and ranks those items, stores review drafts and reviewer actions in SQLite, posts interactive review cards to Discord, and publishes a final digest only after reviewer approval. Published messages can collect `Interested` feedback, which feeds future ranking.

## Quick Start

### 1. Create a Python environment

Use either `venv`:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Or Conda:

```bash
conda env create -f environment.yml
conda activate newsbot
python -m pip install -e .
```

### 2. Install and sign in to Codex CLI

Install Codex CLI and sign in:

```bash
npm install -g @openai/codex
codex --login
codex exec --ephemeral "Say OK"
```

See OpenAI's Codex CLI guides for the latest setup details:

- [Codex CLI getting started](https://help.openai.com/en/articles/11096431)
- [Codex CLI sign in with ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

### 3. Configure Discord

Copy `.env.example` to `.env` and set at least:

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
```

`DISCORD_REVIEWER_USER_IDS` is a comma-separated list of Discord user IDs allowed to operate reviewer buttons. `DISCORD_ALLOWED_GUILD_ID` is optional but recommended for public or shared bot deployments.

### 4. Initialize and run

```bash
python -m newsbot.cli init-db
python -m newsbot.cli run-discord-bot
```

Keep the bot running while reviewers use Discord buttons and modals.

### 5. Generate review candidates

In another terminal:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --submit-review
```

This asks Codex CLI to search recent news, writes a payload under `payloads/`, validates it, and posts up to 30 ranked drafts to the reviewer Discord channel.

### 6. Final weekly review and publish

After reviewers click `Approve Weekly` during first screening:

```bash
python -m newsbot.cli prepare-weekly-publish
```

Reviewers then choose `Publish`, `Edit Draft`, or `Cancel` for each final candidate. After all candidates are decided, the bot posts a final digest preview with `Reorder` and `Publish Digest`. Public publishing happens only when a reviewer clicks `Publish Digest`.

If all candidates have already been decided but the final digest preview is missing, run `prepare-weekly-publish` again. It will repost the `Reorder` / `Publish Digest` preview without moving selected items back to final review.

## Cron Setup

Install cron jobs into `/etc/cron.d/newsbot-automation` with the helper script:

```bash
sudo scripts/install_cron_jobs.sh --app-dir /opt/prd/newsbot-automation --user noguhiro
```

By default, review generation is triggered at 08:00 JST every day and runs only when the last successful run is at least 2 days old. Weekly final-review preparation runs every Monday at 08:00 JST.

Set the runtime user, Python binary, and schedules in `.env`:

```dotenv
NEWSBOT_CRON_USER=noguhiro
NEWSBOT_CRON_TZ=Asia/Tokyo
NEWSBOT_PYTHON_BIN=/opt/prd/newsbot-automation/.venv/bin/python
NEWSBOT_GENERATE_REVIEW_CRON="0 8 * * *"
NEWSBOT_GENERATE_REVIEW_EVERY_DAYS=2
NEWSBOT_GENERATE_REVIEW_TOPIC=lab_automation
NEWSBOT_PREPARE_WEEKLY_CRON="0 8 * * 1"
NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS=1
NEWSBOT_CODEX_BIN=/path/to/codex
```

If cron cannot find `codex`, set an absolute path in `NEWSBOT_CODEX_BIN`. When `NEWSBOT_LOOKBACK_DAYS` is blank, the generator searches from the previous successful run. If Discord rate limits still appear, raise `NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS` to `2` or higher.

## Documentation

- [Full HowTo](docs/howto.md)
- [Manual and automated testing guide](docs/testing-guide.md)
- [Discord review workflow](docs/discord-review-workflow.md)
- [Linux deployment and systemd setup](docs/linux-deployment.md)
- [Security policy](SECURITY.md)

## Verification

Run these before publishing changes:

```bash
python scripts/private_repo_check.py
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

## Advanced / Legacy Features

The legacy `notify` flow can still send webhook/X notifications with `DISCORD_WEBHOOK_URL` and X credentials from `.env.example`, but the recommended public workflow is the Discord Bot + Codex CLI review pipeline above.
