# Linux Deployment

## Prerequisites

- Ubuntu Server or similar Linux host
- Python 3.11+
- Codex CLI installed and authenticated for the runtime user
- A checked-out copy of this repository
- `.env` configured with Discord Bot credentials

## Initial Setup

### Option A: `venv`

```bash
cd /opt/newsbot-automation
python -m venv .venv
. .venv/bin/activate
pip install -e .
python -m newsbot.cli init-db
```

### Option B: `conda`

```bash
cd /opt/newsbot-automation
conda env create -f environment.yml
conda activate newsbot
python -m pip install -e .
python -m newsbot.cli init-db
```

## Runtime Commands

```bash
python -m newsbot.cli run-discord-bot
python scripts/generate_payload_openai.py --topic lab_automation --submit-review
python -m newsbot.cli prepare-weekly-publish
```

Keep `run-discord-bot` running as a service. Run `generate_payload_openai.py` and `prepare-weekly-publish` from cron or another scheduler. Do not automate final public publishing; reviewers should click `Publish Digest` in Discord.

## Install the systemd Service

This repository includes a helper script that generates `/etc/systemd/system/newsbot-discord-bot.service`, reloads systemd, enables the service, and starts it.

Create the service user if needed:

```bash
sudo useradd --system --home /opt/newsbot-automation --shell /usr/sbin/nologin newsbot
sudo chown -R newsbot:newsbot /opt/newsbot-automation
```

### For `venv`

```bash
cd /opt/newsbot-automation
chmod +x scripts/install_systemd_service.sh
sudo scripts/install_systemd_service.sh --env venv --app-dir /opt/newsbot-automation
```

The default Python path is:

```text
/opt/newsbot-automation/.venv/bin/python
```

### For `conda`

```bash
cd /opt/newsbot-automation
chmod +x scripts/install_systemd_service.sh
sudo scripts/install_systemd_service.sh \
  --env conda \
  --app-dir /opt/newsbot-automation \
  --python-bin /opt/miniconda3/envs/newsbot/bin/python
```

Adjust `--python-bin` if your Conda installation path is different.

### Service Commands

```bash
sudo systemctl status newsbot-discord-bot
sudo systemctl restart newsbot-discord-bot
sudo systemctl stop newsbot-discord-bot
sudo journalctl -u newsbot-discord-bot -f
```

## Manual systemd Unit Templates

Templates are also available if you prefer to install manually:

- `deploy/systemd/newsbot-discord-bot.venv.service`
- `deploy/systemd/newsbot-discord-bot.conda.service`

Copy the matching file to `/etc/systemd/system/newsbot-discord-bot.service`, edit paths if needed, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot-discord-bot
```

## Example systemd Unit

### For `venv`

```ini
[Unit]
Description=Newsbot Discord Interaction Service
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/newsbot-automation
EnvironmentFile=/opt/newsbot-automation/.env
ExecStart=/opt/newsbot-automation/.venv/bin/python -m newsbot.cli run-discord-bot
Restart=always
RestartSec=10
User=newsbot
Group=newsbot

[Install]
WantedBy=multi-user.target
```

### For `conda`

Replace `ExecStart` with the Python binary inside your Conda environment. A typical example is:

```ini
[Unit]
Description=Newsbot Discord Interaction Service
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/newsbot-automation
EnvironmentFile=/opt/newsbot-automation/.env
ExecStart=/opt/miniconda3/envs/newsbot/bin/python -m newsbot.cli run-discord-bot
Restart=always
RestartSec=10
User=newsbot
Group=newsbot

[Install]
WantedBy=multi-user.target
```

If your Conda installation path is different, adjust the `ExecStart` path accordingly.

## Example cron Jobs

Install cron jobs with the helper script:

```bash
cd /opt/newsbot-automation
chmod +x scripts/install_cron_jobs.sh scripts/run_cron_task.sh
sudo scripts/install_cron_jobs.sh --app-dir /opt/newsbot-automation --user newsbot
```

The installer writes `/etc/cron.d/newsbot-automation`. The default behavior is:

- trigger review candidate generation daily at 08:00 JST, then run only when the last successful run is at least 2 days old
- run `prepare-weekly-publish` every Monday at 08:00 JST

Configure these values in `.env` before rerunning the installer:

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
NEWSBOT_CRON_STATE_DIR=
NEWSBOT_CODEX_LOG=
NEWSBOT_WEEKLY_LOG=
```

If cron cannot find `codex`, set `NEWSBOT_CODEX_BIN` in `.env` to the absolute path returned by `which codex`. The cron user must have Codex CLI installed and authenticated. If Discord rate limits still appear, raise `NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS` to `2` or higher.
