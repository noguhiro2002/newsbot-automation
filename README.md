# Newsbot Automation

<p align="center">
  <img src="docs/assets/newsbot-automation-logo.png" alt="Newsbot Automation logo" width="180">
</p>

Newsbot Automation is a Python + Discord workflow for collecting topic-based news with Codex CLI, reviewing drafts with a Discord bot, and publishing a weekly digest only after human approval.

Japanese documentation is available in [README.ja.md](README.ja.md).

## Architecture

![Newsbot Automation architecture](docs/assets/newsbot-architecture.png)

The pipeline starts with Codex CLI collecting topic-scoped news and producing a payload JSON. Newsbot validates and ranks those items, stores review drafts and reviewer actions in SQLite, posts interactive review cards to Discord, and publishes a final digest only after reviewer approval. Published messages can collect `Interested` feedback, which feeds future ranking.

<details>
<summary><strong>Candidate collection, selection, and feedback details (click to expand)</strong></summary>

```mermaid
sequenceDiagram
    autonumber
    actor Trigger as Scheduled / manual run
    participant Newsbot as Newsbot Automation<br/>Python application
    participant DB as SQLite
    participant APIs as Paper APIs
    participant Codex as Codex CLI
    actor Expert as Expert Reviewer
    participant Discord as Discord
    participant App as Discord App<br/>Python bot
    actor Viewer as Viewer

    Trigger->>Newsbot: Start candidate generation
    Newsbot->>DB: Load Interested preference profile
    DB-->>Newsbot: category / tag / source

    loop Phases 1–3
        Newsbot->>Codex: Search prompt + preference profile
        Codex->>Codex: Web search and candidate selection
        Codex-->>Newsbot: Phase candidate JSON
    end

    Newsbot->>APIs: Fetch paper candidates in period
    Note over APIs: arXiv / PubMed / bioRxiv<br/>medRxiv / Crossref
    APIs-->>Newsbot: Paper list / per-API results
    Newsbot->>Newsbot: Period filter, normalize, remove duplicates
    Newsbot->>Codex: Phase 4 prompt + API candidates + profile
    alt API candidates available
        Codex->>Codex: Relevance selection and canonical URL checks
    else No candidates / API failure
        Codex->>Codex: Fallback Web search and URL checks
    end
    Codex-->>Newsbot: Phase 4 candidate JSON

    Newsbot->>Codex: All phases + Master prompt + profile
    Codex->>Codex: Merge, deduplicate, and assess importance
    Codex-->>Newsbot: Payload JSON
    Newsbot->>Newsbot: Validate schema/config, deterministically rerank, keep up to 30
    Newsbot->>DB: Save review drafts
    Newsbot->>Discord: Post Human Review candidates

    Expert->>Discord: Approve / Edit / Reject / Hold
    Discord->>App: Interaction event
    App->>DB: Update draft and save review action
    App-->>Discord: Update review display

    Trigger->>Newsbot: Prepare final review
    Newsbot->>DB: Load approved drafts
    Newsbot->>Discord: Post Publish / Cancel / Reorder
    Expert->>Discord: Submit final decision
    Discord->>App: Interaction event
    App->>DB: Save final decision and delivery
    App->>Discord: Publish weekly digest and article details

    Viewer->>Discord: Read and click Interested
    Discord->>App: Interested interaction
    App->>DB: Save feedback once per user/article
    Note over Newsbot,DB: On the next run, the profile boosts<br/>Codex selection and Python ranking
```

Newsbot Automation itself is a Python application. It orchestrates the workflow and implements paper API retrieval and preprocessing, validation and ranking, the Discord App, and SQLite integration. The Python application invokes Codex CLI for candidate-collection Web searches and for the Phase 1–4 and Master LLM selection steps.

For paper discovery, Python retrieves API candidates, filters them to the requested period, and removes duplicates. Duplicate removal consolidates the same paper retrieved from multiple APIs, a preprint, or a publisher version by using identifiers such as DOI and URL. The Codex CLI Phase 4 LLM uses that list as its primary input. It can use Web search as a fallback when APIs fail or return no candidates. The Codex CLI Master LLM merges all phase outputs, after which Python reranks the result with the same feedback profile.

`Interested` events from previously published articles are aggregated by category, tag, and source for the same topic and cadence. The resulting profile is passed to both LLM selection and the final Python ranking on the next run. Feedback is currently a positive-only ranking boost—not probabilistic sampling or a negative-feedback penalty.

</details>

## Prompt Templates

The runtime supports multiple topics, but the detailed multi-phase workflow currently specializes in `lab_automation`. [prompts/lab_automation/](prompts/lab_automation/) is a practical reference implementation: it demonstrates how to define search scope, exclusions, source policy, phase responsibilities, LLM selection over paper API candidates, deduplication, feedback handling, and the JSON output contract. Use this structure as a prompt-design example when adapting Newsbot to another field.

The templates are selected as follows:

- `prompts/lab_automation/phase_1_*.md` through `phase_4_*.md` plus `master_builder.md`: the normal four-phase and Master workflow for `lab_automation`.
- `prompts/lab_automation.md`: the single-prompt fallback used with `--topic lab_automation --single-agent`.
- `prompts/stock_news.md`: an example of the normal top-level single-prompt workflow.
- A topic without `prompts/<topic>.md`: the generic prompt built into Python.

To add a topic with the single-prompt workflow, add its topic, cadence, and destination to `config/newsbot.config.json`, then create `prompts/<topic>.md`. Preserve runtime placeholders such as `{{TOPIC}}`, `{{CADENCE}}`, and `{{PERIOD}}`, together with the Newsbot payload JSON contract.

Copying and renaming the directory alone does not enable the multi-phase workflow or paper API integration for another topic. The execution branch in `scripts/generate_payload_openai.py` explicitly limits that workflow to `lab_automation`; supporting another multi-phase topic also requires extending the phase definitions and execution routing in Python.

## Install

<details>
<summary><strong>Local Install (click to expand)</strong></summary>

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
codex login --device-auth
codex exec --ephemeral "Say OK"
```

See OpenAI's Codex CLI guides for the latest setup details:

- [Codex CLI getting started](https://help.openai.com/en/articles/11096431)
- [Codex CLI sign in with ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

### 3. Configure Discord

Copy `.env.example` to `.env` and set at least:

```bash
install -m 600 .env.example .env
```

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
NEWSBOT_NCBI_TOOL=newsbot_automation
NEWSBOT_NCBI_EMAIL=
NEWSBOT_CROSSREF_MAILTO=
```

`DISCORD_REVIEWER_USER_IDS` is a comma-separated list of Discord user IDs allowed to operate reviewer buttons. `DISCORD_ALLOWED_GUILD_ID` is optional but recommended for public or shared bot deployments.

Set registered NCBI tool/email values before using PubMed. `NCBI_API_KEY` is
optional and is never passed to Codex; Crossref recommends a contact email for
its polite API pool.

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

### 7. Keep the Bot running with systemd

On Linux, the included installer registers the Discord Bot with systemd, starts it immediately, and enables it automatically at OS boot.

Example using `venv` and a dedicated `newsbot` user:

```bash
sudo scripts/install_systemd_service.sh \
  --env venv \
  --app-dir /opt/newsbot-automation \
  --user newsbot \
  --group newsbot
sudo systemctl status newsbot-discord-bot
```

For Conda, specify `--env conda --python-bin /absolute/path/to/python`. The runtime user must be able to access the repository and Python installation and must have authenticated Codex CLI. The installer runs `systemctl enable --now` internally. See [Linux deployment and systemd setup](docs/linux-deployment.md) for the complete procedure.

</details>

<details>
<summary><strong>Docker Install (click to expand)</strong></summary>

Docker Compose can run Newsbot without changing the local venv or Conda workflow:

```bash
install -m 600 .env.example .env
docker compose build
docker compose run --rm codex-login
docker compose run --rm init-db
docker compose up -d discord-bot
```

Generate review candidates as a one-off job:

```bash
docker compose run --rm generate-review
```

Optionally run the schedules inside a container:

```bash
docker compose --profile scheduler up -d
```

SQLite, payloads, logs, and Codex CLI authentication are persisted in named volumes. See the [Docker Compose Install guide](docs/docker-install.md) for configuration, upgrade, and shutdown details.

### Start automatically when the computer boots

On Linux, enable Docker Engine at boot and create the Compose service once:

```bash
sudo systemctl enable --now containerd.service docker.service
docker compose up -d discord-bot
```

If the container scheduler is also required:

```bash
docker compose --profile scheduler up -d
```

Both `discord-bot` and `scheduler` use `restart: unless-stopped`, so existing containers restart after Docker Engine starts. Do not remove them with `docker compose down` if boot-time restart should remain active. After `stop` or `down`, run the appropriate `up -d` command again.

With Docker Desktop, also enable “Start Docker Desktop when you sign in to your computer.” See the [Docker Compose Install guide](docs/docker-install.md#7-start-automatically-when-the-computer-boots) for details and limitations.

</details>

## Cron Setup

Install cron jobs into `/etc/cron.d/newsbot-automation` with the helper script:

```bash
sudo scripts/install_cron_jobs.sh --app-dir /opt/newsbot-automation --user newsbot
```

By default, review generation is triggered at 08:00 JST every day and runs only when the last successful run is at least 2 days old. Weekly final-review preparation runs every Monday at 08:00 JST.

Set the runtime user, Python binary, and schedules in `.env`:

```dotenv
NEWSBOT_CRON_USER=newsbot
NEWSBOT_CRON_TZ=Asia/Tokyo
NEWSBOT_PYTHON_BIN=/opt/newsbot-automation/.venv/bin/python
NEWSBOT_GENERATE_REVIEW_CRON="0 8 * * *"
NEWSBOT_GENERATE_REVIEW_EVERY_DAYS=2
NEWSBOT_GENERATE_REVIEW_TOPIC=lab_automation
NEWSBOT_GENERATE_REVIEW_CADENCE=weekly
NEWSBOT_PREPARE_WEEKLY_CRON="0 8 * * 1"
NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS=1
NEWSBOT_CODEX_BIN=/path/to/codex
```

If cron cannot find `codex`, set an absolute path in `NEWSBOT_CODEX_BIN`. When `NEWSBOT_LOOKBACK_DAYS` is blank, the generator searches from the previous successful run. If Discord rate limits still appear, raise `NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS` to `2` or higher.

## Documentation

- [Full HowTo](docs/howto.md)
- [Manual and automated testing guide](docs/testing-guide.md)
- [Discord review workflow](docs/discord-review-workflow.md)
- [Docker Compose Install](docs/docker-install.md)
- [Linux deployment and systemd setup](docs/linux-deployment.md)
- [Security policy](SECURITY.md)
- [Public release checklist](docs/public-release.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Legal Notices](docs/legal-notices.md)

## Verification

Run these before publishing changes:

```bash
python scripts/private_repo_check.py
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

## Advanced / Legacy Features

The legacy `notify` flow can still send webhook/X notifications with `DISCORD_WEBHOOK_URL` and X credentials from `.env.example`, but the recommended public workflow is the Discord Bot + Codex CLI review pipeline above.

## License and Legal Notices

Newsbot Automation is available under the [MIT License](LICENSE). External API
data and trademarks remain subject to their respective terms. PubMed operators
must configure registered NCBI tool/email values and follow the applicable rate
limits. See [Legal Notices](docs/legal-notices.md).
