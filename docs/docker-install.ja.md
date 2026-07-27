# Docker Compose Install

既存のvenv・Conda・systemd方式を変更せず、Newsbot AutomationをDocker Composeで実行できます。

## 前提条件

- Docker Engine
- Docker Compose v2以降（`docker compose`コマンド）
- Discord Botのtoken・channel ID・reviewer user ID
- Codex CLIへログインできるOpenAIアカウント

## 1. 設定

リポジトリのルートで`.env`を作成します。

```bash
install -m 600 .env.example .env
```

少なくとも以下を設定してください。

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

Docker buildでは、`.env.example`に記載されたCodex CLIバージョンを使用します。必要な場合だけ変更してください。

```dotenv
NEWSBOT_CODEX_CLI_VERSION=0.145.0
NEWSBOT_UV_VERSION=0.11.9
NEWSBOT_DOCKER_UID=10001
NEWSBOT_DOCKER_GID=10001
NEWSBOT_IMAGE_TAG=local
```

## 2. Imageをbuild

```bash
docker compose build
```

ImageにはPython、Newsbot Automation、Node.js、Codex CLIが含まれます。`.env`やローカルのruntimeデータはbuild contextから除外されます。

依存関係は`uv.lock`から`--frozen`でinstallし、Node base imageはdigest固定です。runtime serviceはread-only root filesystem、Linux capability削除、resource/PID制限、container log rotationを使います。秘密情報とvolumeは共通`env_file`ではなく、必要なserviceだけへ割り当てます。

生成serviceには必要なAPI・Discord値を渡しますが、そこから起動するCodex
子processには別の環境変数allowlistを適用します。Discord・X・NCBI
credentialはCodexへ渡しません。Web検索は有効のままで、Codex sandbox
modeは強制しません。

## 3. Codex CLIへログイン

初回だけdevice authを実行します。

```bash
docker compose run --rm codex-login
```

ターミナルに表示されるURLとcodeを使ってログインしてください。認証情報は`codex-home` volumeに保存されるため、通常はcontainerを作り直しても再ログイン不要です。

状態を確認する場合:

```bash
docker compose run --rm codex-login codex login status
```

`codex-home`には認証情報が含まれます。公開、commit、他環境への不用意なcopyをしないでください。

## 4. DB初期化とDiscord Bot起動

```bash
docker compose run --rm init-db
docker compose up -d discord-bot
docker compose logs -f discord-bot
```

Botは`restart: unless-stopped`で動作します。停止する場合:

```bash
docker compose stop discord-bot
```

## 5. 単発job

Review候補を生成してDiscordへ送信:

```bash
docker compose run --rm generate-review
```

最終Reviewを準備:

```bash
docker compose run --rm prepare-weekly
```

topicとcadenceは既存の設定を使用します。

```dotenv
NEWSBOT_GENERATE_REVIEW_TOPIC=lab_automation
NEWSBOT_GENERATE_REVIEW_CADENCE=weekly
```

## 6. 任意のscheduler

既存のcron scheduleをcontainer内で実行する場合:

```bash
docker compose --profile scheduler up -d
docker compose logs -f scheduler
```

使用する設定:

```dotenv
NEWSBOT_CRON_TZ=Asia/Tokyo
NEWSBOT_GENERATE_REVIEW_CRON="0 8 * * *"
NEWSBOT_GENERATE_REVIEW_EVERY_DAYS=2
NEWSBOT_PREPARE_WEEKLY_CRON="0 8 * * 1"
```

`generate-review`は指定時刻に起動し、既存のevery-days gateとfile lockを適用します。host側cronとDocker schedulerを同時に有効化しないでください。

## 7. PC起動時の自動起動

`compose.yaml`の`discord-bot`と`scheduler`には`restart: unless-stopped`が設定されています。Linuxでは、Docker EngineをOS起動時に開始するよう有効化します。

```bash
sudo systemctl enable --now containerd.service docker.service
```

そのうえで、必要なserviceを一度作成・起動します。

```bash
docker compose up -d discord-bot
```

schedulerも常駐させる場合:

```bash
docker compose --profile scheduler up -d
```

以後、作成済みcontainerはPC再起動後にDocker Engineとともに自動復帰します。次の点に注意してください。

- `docker compose down`はcontainerを削除するため、その後は自動起動しません。再度`up -d`を実行してください。
- `docker compose stop`で明示的に停止したcontainerも、再度`up -d`するまで停止状態を維持します。
- named volumeはcontainerの自動起動とは独立して保持されます。
- Docker Engine自体を無効化している環境ではcontainerも起動しません。

Docker Desktopを使う場合は、設定画面で「Start Docker Desktop when you sign in to your computer」を有効にします。これはユーザーのログイン後にDocker Desktopを起動する設定です。Compose containerはDocker Desktopが起動した後、同じrestart policyで復帰します。

参考:

- [Docker Engineをsystemdで自動起動する](https://docs.docker.com/engine/install/linux-postinstall/#configure-docker-to-start-on-boot-with-systemd)
- [Containerのrestart policy](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [Docker Desktop settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/)

## 永続化

Composeは以下のnamed volumeを使用します。

| Volume | 内容 |
| --- | --- |
| `newsbot-data` | SQLite DB、論文API cache、前回実行時刻、scheduler lock |
| `newsbot-payloads` | 生成payload、prompt、Codex実行artifact |
| `newsbot-logs` | scheduler・運用log |
| `codex-home` | Codex CLI設定・認証情報 |

SQLiteを共有するため、`discord-bot`を複数replicaへscaleしないでください。

通常のcontainer削除ではvolumeを残します。

```bash
docker compose down
```

ただし、`down`後はcontainerが存在しないためPC起動時に自動復帰しません。自動起動を再開するには、必要なprofileを付けて`docker compose up -d`を実行してください。

`docker compose down --volumes`はSQLiteとCodex認証を含むvolumeも削除します。データを完全に破棄する意図がある場合以外は実行しないでください。

## 更新

```bash
git pull
docker compose build
docker compose up -d discord-bot
```

schedulerを使用している場合:

```bash
docker compose --profile scheduler up -d
```

named volume内のデータはImageやcontainerを更新しても維持されます。

## Troubleshooting

サービス状態:

```bash
docker compose ps
docker compose logs --tail=200 discord-bot
docker compose logs --tail=200 scheduler
```

Codex CLI確認:

```bash
docker compose run --rm codex-login codex --version
docker compose run --rm codex-login codex login status
```

設定を変更した後は、Botを再作成してください。

```bash
docker compose up -d --force-recreate discord-bot
```
