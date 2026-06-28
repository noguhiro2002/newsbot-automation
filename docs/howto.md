# Newsbot Automation HowTo

This guide explains the recommended public workflow: Codex CLI collects news on a schedule, Discord reviewers screen and edit drafts, and the bot publishes only after final approval.

## 1. Requirements

- Python 3.11+
- Discord Bot token
- Discord review channel ID
- Discord publish channel ID
- Discord user IDs for reviewers
- Codex CLI installed and authenticated

Install Codex CLI with:

```bash
npm install -g @openai/codex
codex --login
codex exec --ephemeral "Say OK"
```

Official setup references:

- [OpenAI Codex CLI getting started](https://help.openai.com/en/articles/11096431)
- [Codex CLI sign in with ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

## 2. Install the Python App

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

## 3. Configure `.env`

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Set:

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
NEWSBOT_CODEX_BIN=
NEWSBOT_CODEX_MODEL=
NEWSBOT_LOOKBACK_DAYS=
```

Notes:

- `DISCORD_REVIEWER_USER_IDS` is a comma-separated list.
- `DISCORD_ALLOWED_GUILD_ID` is optional but recommended.
- Leave `NEWSBOT_CODEX_BIN` blank to use `codex` from `PATH`.
- Leave `NEWSBOT_CODEX_MODEL` blank to use the Codex CLI default.
- Leave `NEWSBOT_LOOKBACK_DAYS` blank to search since the previous successful run.

## 4. Initialize the Database

```bash
python -m newsbot.cli init-db
```

The local SQLite database is created under `data/`. Do not commit it.

## 5. Start the Discord Bot

```bash
python -m newsbot.cli run-discord-bot
```

Keep this process running. It handles reviewer buttons, edit modals, source checks, final ordering, publishing, and reader feedback.

## 6. Generate Drafts with Codex CLI

Run:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --submit-review
```

What happens:

1. The script builds a news-search prompt.
2. Codex CLI searches recent news.
3. The JSON payload is saved under `payloads/`.
4. The payload is validated.
5. Up to 30 ranked drafts are posted to the Discord review channel.

To test prompt generation without calling Codex:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --skip-codex
```

To force a fixed lookback window:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --lookback-days 3 --submit-review
```

## 7. First Screening in Discord

For each review draft, reviewers can choose:

- `Approve Weekly`: keep it for the next weekly digest.
- `Edit Draft`: edit title, body, category, tags, note, and source URL.
- `Check Source`: ask Codex CLI to verify or repair the source URL.
- `Publish Breaking`: publish immediately after confirmation.
- `Hold`: move it to hold.
- `Reject`: reject it.

Only users listed in `DISCORD_REVIEWER_USER_IDS` can operate reviewer controls.

## 8. Final Weekly Review

When weekly-approved drafts are ready for final review:

```bash
python -m newsbot.cli prepare-weekly-publish
```

The bot posts final review messages. For each item, choose:

- `Publish`: select it for the final digest.
- `Edit Draft`: revise text/source before deciding.
- `Cancel`: return it to pending review.

After every item has been judged, the bot posts a final digest preview with:

- `Reorder`: open a Discord Modal and enter the current item numbers in the desired order, such as `3,1,2,4`.
- `Publish Digest`: publish immediately in the displayed order.

Public publishing happens only after `Publish Digest`.

If every item has already been judged but the final digest preview is missing, run `prepare-weekly-publish` again. The command will repost the digest preview instead of resetting selected items.

Reviewers can also run the same final-review preparation from Discord with `/newsbot_prepare_weekly`.

## 9. Published Digest Format

The bot publishes:

1. One overview message with the date range and numbered title list.
2. One detailed message per news item.
3. A `👍 Interested (n)` button on each detailed message.

Discord link preview cards are suppressed so the source URL remains visible without a large preview block.

## 10. cron Scheduling

Example: generate review candidates every Monday at 08:00:

```cron
0 8 * * 1 cd /path/to/newsbot-automation && /path/to/venv/bin/python scripts/generate_payload_openai.py --topic lab_automation --submit-review >> logs/newsbot-codex.log 2>&1
```

Example: notify reviewers for final weekly review every Friday at 16:00:

```cron
0 16 * * 5 cd /path/to/newsbot-automation && /path/to/venv/bin/python -m newsbot.cli prepare-weekly-publish >> logs/newsbot-weekly.log 2>&1
```

Cron often has a limited `PATH`. Use absolute paths for Python and set `NEWSBOT_CODEX_BIN` if needed.

## 11. Useful Admin Commands

Run these in Discord:

- `/newsbot_next_weekly`
- `/newsbot_weekly_queue`
- `/newsbot_held`
- `/newsbot_rejected`
- `/newsbot_cancelled`
- `/newsbot_report`
- `/newsbot_refresh_reviews`
- `/newsbot_prepare_weekly`

## 12. Verification

```bash
python scripts/private_repo_check.py
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
python -m newsbot.cli validate-payload --input samples/lab_automation_weekly.sample.json
python -m newsbot.cli submit-review --input samples/lab_automation_weekly.sample.json --dry-run
python -m newsbot.cli prepare-weekly-publish --dry-run
```

## 13. Advanced / Legacy Delivery

The old `notify` command still supports Discord webhook and X delivery through `DISCORD_WEBHOOK_URL` and X credentials. This is not the recommended public workflow. Prefer the Discord Bot review pipeline for normal use.
