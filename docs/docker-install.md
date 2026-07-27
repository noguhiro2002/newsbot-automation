# Docker Compose Install

Docker Compose is an additional installation option. It does not replace the existing venv, Conda, or systemd workflows.

## Prerequisites

- Docker Engine
- Docker Compose v2 or later (`docker compose`)
- Discord Bot token, channel IDs, and reviewer user IDs
- An OpenAI account that can sign in to Codex CLI

## 1. Configure

Create `.env` in the repository root:

```bash
install -m 600 .env.example .env
```

Set at least:

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

The container build uses the pinned Codex CLI version from `.env.example`. Change these only when needed:

```dotenv
NEWSBOT_CODEX_CLI_VERSION=0.145.0
NEWSBOT_UV_VERSION=0.11.9
NEWSBOT_DOCKER_UID=10001
NEWSBOT_DOCKER_GID=10001
NEWSBOT_IMAGE_TAG=local
```

## 2. Build the image

```bash
docker compose build
```

The image contains Python, Newsbot Automation, Node.js, and Codex CLI. `.env` and local runtime data are excluded from the build context.

Dependencies are installed from `uv.lock` with `--frozen`, and the Node base image is pinned by digest. Runtime services use a read-only root filesystem, drop Linux capabilities, limit resources and PIDs, and rotate container logs. Secrets and volumes are assigned per service rather than injected through a common `env_file`.

The generation service receives the API and Discord values it needs, but its
Codex child process receives a separate environment allowlist. Discord, X, and
NCBI credentials are not forwarded to Codex. Web search remains enabled and no
Codex sandbox mode is forced.

## 3. Sign in to Codex CLI

Run device authentication once:

```bash
docker compose run --rm codex-login
```

Open the displayed URL and enter the displayed code. Credentials are saved in the `codex-home` volume, so rebuilding or recreating a container normally does not require another login.

Check login status:

```bash
docker compose run --rm codex-login codex login status
```

`codex-home` contains authentication data. Do not publish, commit, or casually copy it to another environment.

## 4. Initialize and start the Discord Bot

```bash
docker compose run --rm init-db
docker compose up -d discord-bot
docker compose logs -f discord-bot
```

The Bot uses `restart: unless-stopped`. To stop it:

```bash
docker compose stop discord-bot
```

## 5. One-off jobs

Generate review candidates and submit them to Discord:

```bash
docker compose run --rm generate-review
```

Prepare final review:

```bash
docker compose run --rm prepare-weekly
```

The jobs use the existing topic and cadence settings:

```dotenv
NEWSBOT_GENERATE_REVIEW_TOPIC=lab_automation
NEWSBOT_GENERATE_REVIEW_CADENCE=weekly
```

## 6. Optional scheduler

To run the existing cron schedules inside a container:

```bash
docker compose --profile scheduler up -d
docker compose logs -f scheduler
```

It uses:

```dotenv
NEWSBOT_CRON_TZ=Asia/Tokyo
NEWSBOT_GENERATE_REVIEW_CRON="0 8 * * *"
NEWSBOT_GENERATE_REVIEW_EVERY_DAYS=2
NEWSBOT_PREPARE_WEEKLY_CRON="0 8 * * 1"
```

`generate-review` applies the existing every-days gate and file lock after each scheduled trigger. Do not enable both the host cron jobs and the Docker scheduler.

## 7. Start automatically when the computer boots

Both `discord-bot` and `scheduler` have `restart: unless-stopped` in `compose.yaml`. On Linux, first enable Docker Engine at OS boot:

```bash
sudo systemctl enable --now containerd.service docker.service
```

Create and start the required service once:

```bash
docker compose up -d discord-bot
```

If the scheduler should also remain running:

```bash
docker compose --profile scheduler up -d
```

The existing containers will then restart with Docker Engine after a computer reboot. Keep these conditions in mind:

- `docker compose down` removes the containers, so they cannot restart until `up -d` is run again.
- A container explicitly stopped with `docker compose stop` remains stopped until `up -d` is run again.
- Named volumes remain available independently of container automatic startup.
- Containers cannot start if Docker Engine itself is disabled.

With Docker Desktop, enable “Start Docker Desktop when you sign in to your computer” in its settings. Docker Desktop starts after user login, after which the Compose containers recover under the same restart policy.

References:

- [Start Docker Engine at boot with systemd](https://docs.docker.com/engine/install/linux-postinstall/#configure-docker-to-start-on-boot-with-systemd)
- [Container restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [Docker Desktop settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/)

## Persistence

Compose uses these named volumes:

| Volume | Contents |
| --- | --- |
| `newsbot-data` | SQLite DB, paper API cache, last-run state, and scheduler locks |
| `newsbot-payloads` | Payloads, prompts, and Codex run artifacts |
| `newsbot-logs` | Scheduler and operational logs |
| `codex-home` | Codex CLI configuration and credentials |

Do not scale `discord-bot` to multiple replicas because the services share SQLite.

Removing the containers normally keeps the volumes:

```bash
docker compose down
```

However, no container exists after `down`, so boot-time restart is inactive. Run `docker compose up -d` with the required profile to restore automatic startup.

`docker compose down --volumes` also deletes volumes containing SQLite data and Codex credentials. Do not run it unless permanent data removal is intended.

## Upgrade

```bash
git pull
docker compose build
docker compose up -d discord-bot
```

If the scheduler is enabled:

```bash
docker compose --profile scheduler up -d
```

Named-volume data remains available after image and container updates.

## Troubleshooting

Inspect services:

```bash
docker compose ps
docker compose logs --tail=200 discord-bot
docker compose logs --tail=200 scheduler
```

Check Codex CLI:

```bash
docker compose run --rm codex-login codex --version
docker compose run --rm codex-login codex login status
```

After changing runtime settings, recreate the Bot:

```bash
docker compose up -d --force-recreate discord-bot
```
