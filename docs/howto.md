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
codex login --device-auth
codex exec --ephemeral "Say OK"
```

Official setup references:

- [OpenAI Codex CLI getting started](https://help.openai.com/en/articles/11096431)
- [Codex CLI sign in with ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

## 2. Install the Python App Locally

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

For Docker Compose, use the [Docker Compose Install guide](docker-install.md) instead of this local installation procedure.

## 3. Configure `.env`

Copy `.env.example` to `.env`:

```bash
install -m 600 .env.example .env
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
NEWSBOT_CODEX_REASONING_EFFORT=
NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL=
NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT=
NEWSBOT_LAB_AUTOMATION_MASTER_MODEL=
NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT=
NEWSBOT_PAPER_API_RETMAX=
NEWSBOT_NCBI_TOOL=newsbot_automation
NEWSBOT_NCBI_EMAIL=
NCBI_API_KEY=
NEWSBOT_CROSSREF_MAILTO=
NEWSBOT_API_CACHE_TTL_SECONDS=21600
NEWSBOT_LOOKBACK_DAYS=
```

Notes:

- `DISCORD_REVIEWER_USER_IDS` is a comma-separated list.
- `DISCORD_ALLOWED_GUILD_ID` is optional but recommended.
- Leave `NEWSBOT_CODEX_BIN` blank to use `codex` from `PATH`.
- Leave `NEWSBOT_CODEX_MODEL` blank to use the Codex CLI default.
- `NEWSBOT_CODEX_REASONING_EFFORT` accepts `low`, `medium`, `high`, or `xhigh`.
- Phase-specific model and reasoning values override the common Codex settings for the `lab_automation` multi-phase workflow.
- `NEWSBOT_PAPER_API_RETMAX` controls Phase 4 candidates per paper API and defaults to 50.
- Register `NEWSBOT_NCBI_TOOL` and the developer `NEWSBOT_NCBI_EMAIL` with NCBI before enabling PubMed. `NCBI_API_KEY` is optional.
- NCBI requests are limited to 3/s without an API key and 10/s with one. `NEWSBOT_CROSSREF_MAILTO` selects Crossref's polite pool.
- API responses are cached under `data/api-cache/` for `NEWSBOT_API_CACHE_TTL_SECONDS`.
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

1. The script loads the historical `Interested` preference profile from SQLite.
2. For `lab_automation`, Codex CLI runs Phases 1–3 for Web discovery while Python collects paper API candidates for Phase 4.
3. Codex CLI selects Phase 4 papers, and the Master step merges all phase outputs.
4. Python validates and deterministically reranks the payload with the same feedback profile.
5. Artifacts are saved under `payloads/`, and up to 30 drafts are posted to the Discord review channel.

Phase 4 retrieves candidates from arXiv, PubMed, bioRxiv, medRxiv, and Crossref, applies period filtering and duplicate removal, and passes the resulting list to Codex CLI for relevance selection. Codex can use Web search as a fallback if the APIs fail or return no candidates.

The paper API client applies rate limits and retries retryable failures with backoff. Query metadata stored in coverage and prompts is redacted so that `NCBI_API_KEY` is never persisted or passed to Codex. Review the [Legal Notices](legal-notices.md) before operating PubMed retrieval.

`prompts/lab_automation/` is both the active Lab Automation template set and a detailed reference implementation for other domains. When adapting it, replace its scope and exclusions, phase responsibilities, source policy, paper-selection rules, feedback guidance, and JSON examples while preserving the runtime placeholders and output contract.

Adding `prompts/<topic>.md` normally enables the single-prompt workflow for that topic. Copying the directory alone does not activate the multi-phase workflow; another multi-phase topic also requires extending the phase definitions and topic routing in `scripts/generate_payload_openai.py`.

To test prompt generation without calling Codex:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --skip-codex
```

To force a fixed lookback window:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --lookback-days 3 --submit-review
```

Reviewers can start the same candidate workflow from Discord with `/newsbot_collect_reviews`.

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

## 10. Persistent Bot and Scheduling

Register the local Discord Bot with systemd. The installer starts the service immediately and enables it at OS boot:

```bash
sudo scripts/install_systemd_service.sh \
  --env venv \
  --app-dir /opt/newsbot-automation \
  --user newsbot \
  --group newsbot
```

Register review generation and final-review preparation with the included cron installer:

```bash
sudo scripts/install_cron_jobs.sh \
  --app-dir /opt/newsbot-automation \
  --user newsbot
```

The current defaults trigger candidate generation every day at 08:00 JST but run it only after at least two days since the previous success. Final-review preparation runs every Monday at 08:00 JST. Change schedules, topic, cadence, Python, and Codex CLI paths through the `NEWSBOT_*` values in `.env`, then rerun the installer.

See [Linux Deployment](linux-deployment.md) for the complete systemd and cron procedure. For Docker, do not also enable host cron; use the container scheduler from the [Docker Compose Install guide](docker-install.md).

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
- `/newsbot_collect_reviews`

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
