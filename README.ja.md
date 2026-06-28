# Newsbot Automation

<p align="center">
  <img src="docs/assets/newsbot-automation-logo.png" alt="Newsbot Automation logo" width="180">
</p>

Newsbot Automation は、Codex CLI でトピック別ニュースを収集し、Discord Bot 上で人間がレビューし、最終承認後に週次ダイジェストとして公開するための Python アプリです。

English documentation is available in [README.md](README.md).

## Architecture

![Newsbot Automation architecture](docs/assets/newsbot-architecture.png)

Codex CLI がトピックに沿ったニュースを収集して payload JSON を作成し、Newsbot が検証・ランキングしたうえで SQLite にレビュー下書きと操作履歴を保存します。Discord のレビューチャンネルではボタンやモーダルで人間が確認し、最終承認後だけ公開チャンネルへ週次ダイジェストを投稿します。公開後の `Interested` フィードバックは、次回以降のランキングに反映されます。

## Quick Start

### 1. Python環境を作る

`venv` の場合:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Conda の場合:

```bash
conda env create -f environment.yml
conda activate newsbot
python -m pip install -e .
```

### 2. Codex CLIをインストールしてログインする

```bash
npm install -g @openai/codex
codex --login
codex exec --ephemeral "Say OK"
```

最新の手順は OpenAI 公式ドキュメントも確認してください。

- [Codex CLI getting started](https://help.openai.com/en/articles/11096431)
- [Codex CLI sign in with ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

### 3. Discord設定を書く

`.env.example` を `.env` にコピーし、少なくとも以下を設定します。

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
```

`DISCORD_REVIEWER_USER_IDS` は、Reviewer 操作を許可する Discord user ID のカンマ区切りです。`DISCORD_ALLOWED_GUILD_ID` は任意ですが、公開・共有環境では設定を推奨します。

### 4. DB初期化とBot起動

```bash
python -m newsbot.cli init-db
python -m newsbot.cli run-discord-bot
```

Discord のボタンやModal操作を受け付けるため、Bot は起動したままにします。

### 5. Codex CLIでReview候補を作る

別ターミナルで実行します。

```bash
python scripts/generate_payload_openai.py --topic lab_automation --submit-review
```

Codex CLI が最近のニュースを検索し、`payloads/` にpayloadを保存し、検証後に最大30件の候補をReviewerチャンネルへ投稿します。

### 6. 最終レビューと公開

1st screening で `Approve Weekly` した記事がある場合:

```bash
python -m newsbot.cli prepare-weekly-publish
```

Reviewer は各候補に対して `Publish`、`Edit Draft`、`Cancel` を選びます。全件の判断が揃うと、Bot が `Reorder` と `Publish Digest` 付きの最終一覧を投稿します。実際の公開は Reviewer が `Publish Digest` を押したときだけ行われます。

全件判断済みなのに最終一覧が出ていない場合は、`prepare-weekly-publish` をもう一度実行してください。選択済みの記事をfinal reviewへ戻さず、`Reorder` / `Publish Digest` 付きの最終一覧だけを再投稿します。

## cron設定

cron は helper script で `/etc/cron.d/newsbot-automation` にインストールできます。

```bash
sudo scripts/install_cron_jobs.sh --app-dir /opt/prd/newsbot-automation --user noguhiro
```

デフォルトでは、Review候補生成は毎朝 08:00 JST に起動し、前回成功から2日以上経っている場合だけ実行します。週次の最終レビュー準備は毎週月曜 08:00 JST に実行します。

`.env` で実行ユーザー、Python、スケジュールを変更できます。

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

cron で `codex` が見つからない場合は、`NEWSBOT_CODEX_BIN` に絶対パスを指定します。`NEWSBOT_LOOKBACK_DAYS` が空の場合、前回成功実行時から今回実行時までを検索します。Discord の rate limit が出る場合は、`NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS` を `2` などに上げてください。

## Documentation

- [Full HowTo 日本語](docs/howto.ja.md)
- [テスト手順](docs/testing-guide.ja.md)
- [Discord review workflow](docs/discord-review-workflow.md)
- [Linux deployment and systemd setup](docs/linux-deployment.md)
- [Security policy](SECURITY.md)

## Verification

公開前に以下を実行してください。

```bash
python scripts/private_repo_check.py
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

## Advanced / Legacy Features

旧 `notify` フローでは、`.env.example` の `DISCORD_WEBHOOK_URL` や X 関連credentialを使った通知も残っています。ただし、公開版で推奨する主導線は Discord Bot + Codex CLI のレビューパイプラインです。
