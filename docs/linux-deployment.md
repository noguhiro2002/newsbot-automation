# Linux Deployment

## Host Timezone

Set the VM host timezone to Japan Standard Time before installing schedules:

```bash
sudo timedatectl set-timezone Asia/Tokyo
timedatectl status
```

Keep `TZ=Asia/Tokyo` and `NEWSBOT_CRON_TZ=Asia/Tokyo` in `.env`. Newsbot systemd timers also include `Asia/Tokyo` explicitly, so their intended wall-clock schedule remains stable even if the host timezone is changed accidentally. Database timestamps and backup filename timestamps remain UTC intentionally for unambiguous storage and compatibility.

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

The runtime user must also have access to the Codex CLI binary and its authentication state. Authenticate the dedicated user before installing the service:

```bash
sudo -u newsbot -H codex login --device-auth
sudo -u newsbot -H codex exec --ephemeral "Say OK"
```

Alternatively, install the service under an existing Linux user that already has working Codex CLI authentication.

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
sudo systemctl is-enabled newsbot-discord-bot
sudo systemctl restart newsbot-discord-bot
sudo systemctl stop newsbot-discord-bot
sudo journalctl -u newsbot-discord-bot -f
```

The installer runs `systemctl enable --now`, so the Bot starts immediately and starts automatically at every OS boot. `systemctl stop` only stops the current run; because the unit remains enabled, it starts again at the next boot. To disable boot-time startup explicitly:

```bash
sudo systemctl disable --now newsbot-discord-bot
```

The systemd service keeps only the Discord Bot process running. Candidate generation and final-review preparation are separate scheduled jobs; install the cron jobs described below if they should continue automatically after reboot.

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
UMask=0077
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
UMask=0077
User=newsbot
Group=newsbot

[Install]
WantedBy=multi-user.target
```

If your Conda installation path is different, adjust the `ExecStart` path accordingly.

## Recommended systemd Timers

For always-on Linux hosts and VMs, use systemd timers instead of cron. The timer installer uses timezone-aware `OnCalendar` expressions, enables `Persistent=true` so a missed run is started after the VM returns, creates verified compressed SQLite backups, and installs log rotation.

```bash
cd /opt/newsbot-automation
sudo scripts/install_systemd_timers.sh \
  --app-dir /opt/newsbot-automation \
  --user newsbot \
  --group newsbot \
  --python-bin /absolute/path/to/python
```

The defaults are:

- generate review candidates every Monday at 06:00 Asia/Tokyo
- prepare weekly review every Monday at 09:00 Asia/Tokyo
- back up `data/newsbot.sqlite` every day at 03:30 Asia/Tokyo
- retain 14 compressed backups under `backups/sqlite`
- rotate `logs/*.log` weekly, retaining 12 compressed rotations and rotating early at 10 MiB

The installer validates every calendar expression and unit, enables the timers, runs the first SQLite backup immediately, and moves `/etc/cron.d/newsbot-automation` to a timestamped `.disabled` filename so jobs cannot run twice. Configure overrides in `.env`:

```dotenv
NEWSBOT_GENERATE_REVIEW_ON_CALENDAR="Mon *-*-* 06:00:00 Asia/Tokyo"
NEWSBOT_PREPARE_WEEKLY_ON_CALENDAR="Mon *-*-* 09:00:00 Asia/Tokyo"
NEWSBOT_SQLITE_BACKUP_ON_CALENDAR="*-*-* 03:30:00 Asia/Tokyo"
NEWSBOT_SQLITE_BACKUP_KEEP=14
NEWSBOT_SQLITE_BACKUP_DIR=
```

Inspect the schedule and logs with:

```bash
systemctl list-timers --all "newsbot-*"
sudo journalctl -u newsbot-generate-review.service -n 100 --no-pager
sudo journalctl -u newsbot-prepare-weekly.service -n 100 --no-pager
sudo journalctl -u newsbot-sqlite-backup.service -n 100 --no-pager
```

The SQLite backup uses the SQLite Online Backup API, runs an integrity check before compression, writes atomically, and keeps permissions private. A local backup protects against application-level corruption or accidental deletion, but it does not replace an off-host Proxmox or PBS backup.

### Generation retries and operational notifications

`run_cron_task.sh generate-review` calls `scripts/run_scheduled_job.py` while holding the existing job lock. Collection and Discord submission each have at most three attempts, with 5- and 10-minute waits. Authentication, permission, unsupported-model, and missing-executable errors stop immediately. Collection attempts have a three-hour timeout; submission attempts have a ten-minute timeout.

The job posts operational notices using `DISCORD_BOT_TOKEN` to **`DISCORD_REVIEW_CHANNEL_ID`**: first failure with retry plans, final failure when retries are exhausted or manual intervention is required, and recovery after a successful retry. Notices contain the stage, attempt count, JST timestamp, run ID, and diagnostic log locations. Raw exception bodies, credentials, and automatic mentions are excluded.

Collection and review submission are separate stages. Submission retries reuse the same payload, and `submit-review` skips articles with recorded review messages. The search period is fixed across collection retries. The successful-run timestamp advances only after submission succeeds (or collection produces a valid empty payload), using the collection cutoff so a slow run does not create gaps in future searches.

If Discord cannot be reached, notices remain under `data/cron/job-notifications/` (or the configured `NEWSBOT_CRON_STATE_DIR`). The installer also enables `newsbot-job-notifications.timer`, which retries delivery every five minutes by default and after boot. Rerun the timer installer when upgrading an existing deployment to add this timer. Unsent alerts are also retried when the next collection starts. Details are streamed to the systemd journal and appended to `logs/newsbot-scheduled-job.log`, covered by log rotation.

```dotenv
NEWSBOT_JOB_MAX_ATTEMPTS=3
NEWSBOT_JOB_RETRY_SECONDS=300
NEWSBOT_JOB_RETRY_BACKOFF=2
NEWSBOT_JOB_RETRY_MAX_SECONDS=1800
NEWSBOT_JOB_ATTEMPT_TIMEOUT_SECONDS=10800
NEWSBOT_JOB_NOTIFICATIONS_ENABLED=true
NEWSBOT_JOB_NOTIFICATION_EVENTS=retry,failed,recovered
NEWSBOT_JOB_NOTIFICATION_RETRY_SECONDS=300
```

Attempts can be set to 1–3, the initial wait to 0–600 seconds, the backoff multiplier to 1–4, the wait cap to 0–1800 seconds, and the collection timeout to 1–10800 seconds. Each wait is `min(cap, initial_wait * multiplier ** (attempt - 1))`. Settings are read from `.env` for each job invocation.

Set `NEWSBOT_JOB_NOTIFICATIONS_ENABLED=false` to disable both queuing and delivery (existing queued alerts are preserved). `NEWSBOT_JOB_NOTIFICATION_EVENTS` selects a comma-separated subset of `retry` (first failure), `failed` (final failure), and `recovered` (successful retry). Set it to `failed` for final failures only. The destination is the existing `DISCORD_REVIEW_CHANNEL_ID`.

`NEWSBOT_JOB_NOTIFICATION_RETRY_SECONDS` sets the queued-alert delivery interval (60–3600 seconds). Rerun `scripts/install_systemd_timers.sh` after changing this value because it is written into a systemd timer; other settings take effect on the next invocation without reinstalling. To verify delivery without starting collection, run this as the configured service user:

```bash
/absolute/path/to/python scripts/run_scheduled_job.py test-notification
systemctl list-timers --all 'newsbot-*'
sudo journalctl -u newsbot-job-notifications.service -n 30 --no-pager
```

## Legacy cron Jobs

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
