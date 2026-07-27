# Codex実装指示書: DiscordニュースBotにReviewer承認・本文編集・特報配信・「興味あり」フィードバック機能を追加する

## 対象リポジトリ

- Repository: `noguhiro2002/newsbot-automation`
- 既存のニュース配信部分は実装済み。
- 今回は既存実装を壊さず、Discord向けに以下を追加する。
  - Reviewer用の承認フロー
  - Reviewerによる本文編集
  - 週次ニュース配信キュー
  - 重要ニュースの「特報」即時配信
  - 読者側の「興味あり」ボタン
  - 後続分析に備えたイベントログ保存
- BigQuery連携はStep 2とし、今回の実装対象外。ただし将来BigQueryへバッチ同期しやすいDB設計にする。

---

## 現在の前提

このリポジトリは、現時点で以下のような小さなPython CLI構成になっている想定で進める。

- Python 3.11以上。
- 既存の `newsbot.cli` に `init-db`、`validate-payload`、`notify` がある。
- 既存の通知ログ・送信ログ・重複除外キーはSQLiteに保存される。
- 既存のDB既定パスは `data/newsbot.sqlite`。
- 既存のDiscord配信は `DISCORD_WEBHOOK_URL` によるWebhook投稿。
- 既存の `notify` はPayload JSONを読み込み、重複除外後にDiscordまたはXへ送信する。
- `pyproject.toml` は現状、外部依存なしに近い構成である可能性がある。

まずCodexは必ずリポジトリ内を調査し、実際のファイル名・構成・既存テストに合わせて最小差分で実装すること。

---

## 実装方針の重要ポイント

### 1. 既存の `notify` を壊さない

既存の `python -m newsbot.cli notify --input ...` はそのまま動くようにする。

今回追加するReviewer承認フローは、既存の即時通知機能を置き換えるのではなく、別コマンドとして追加する。

推奨追加コマンド:

```bash
python -m newsbot.cli submit-review --input payloads/latest.json
python -m newsbot.cli submit-review --input payloads/latest.json --dry-run

python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly
python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly --dry-run

python -m newsbot.cli run-discord-bot
```

### 2. BigQueryはまだ実装しない

今回BigQuery連携は入れない。

ただし、以下のイベントログテーブルをSQLite側に作り、後でBigQueryへCSV/JSONLでエクスポートできるようにしておく。

- article draft lifecycle
- review actions
- delivery events
- feedback events

### 3. Webhook投稿とBot投稿を分ける

既存のWebhook投稿は維持する。

ただし、Reviewer画面、ボタン、Modal、読者フィードバックにはDiscord Botが必要になる。  
そのため、以下を追加する。

- `DISCORD_BOT_TOKEN`
- `DISCORD_REVIEW_CHANNEL_ID`
- `DISCORD_PUBLISH_CHANNEL_ID`
- `DISCORD_REVIEWER_USER_IDS`

Bot実装は、可能なら `discord.py` などの一般的なDiscordライブラリを使ってよい。  
ただし、既存コードが外部依存を避けている場合は、依存追加の理由をREADMEに明記すること。

推奨:

```toml
dependencies = [
  "discord.py>=2.4.0"
]
```

Raw Gateway/WebSocketを標準ライブラリだけで実装することは避ける。保守性が悪くなるため。

---

## 追加する環境変数

`.env.example` とREADMEに以下を追加する。

```bash
# Existing
DISCORD_WEBHOOK_URL=

# New: Discord Bot / Interactive Review
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=

# Comma-separated Discord user IDs allowed to operate reviewer buttons.
# Example: 123456789012345678,234567890123456789
DISCORD_REVIEWER_USER_IDS=

# Optional. If set, ignore interactions outside this guild.
DISCORD_ALLOWED_GUILD_ID=
```

注意:

- `.env` は絶対にコミットしない。
- Bot token、Webhook URL、X API tokenなどはログに出さない。
- エラーメッセージにtokenやWebhook URLが混入しないよう、既存のredaction処理があれば再利用・拡張する。

---

## 追加DBスキーマ

既存の `NewsbotStore.init()` もしくは既存のmigration方針に合わせて、SQLiteに以下のテーブルを追加する。

既存DBを破壊しないこと。`CREATE TABLE IF NOT EXISTS` と `ALTER TABLE` の安全なmigrationを使うこと。

### `article_drafts`

ニュース候補、編集後本文、配信状態を管理する。

```sql
CREATE TABLE IF NOT EXISTS article_drafts (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    cadence TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    event_hash TEXT NOT NULL,

    title_original TEXT NOT NULL,
    body_original TEXT NOT NULL,
    title_edited TEXT NOT NULL,
    body_edited TEXT NOT NULL,

    category_primary TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    importance_score REAL NOT NULL DEFAULT 0,

    status TEXT NOT NULL,
    publish_type TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',

    review_channel_id TEXT,
    review_message_id TEXT,
    published_channel_id TEXT,
    published_message_id TEXT,

    source_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scheduled_at TEXT,
    published_at TEXT,

    UNIQUE(topic, canonical_url),
    UNIQUE(topic, event_hash)
);
```

`status` は最低限以下を使う。

```text
pending
approved_weekly
held
rejected
published
failed
```

`publish_type` は以下。

```text
weekly
breaking
```

### `review_actions`

Reviewer操作履歴を保存する。

```sql
CREATE TABLE IF NOT EXISTS review_actions (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    reviewer_user_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES article_drafts(id)
);
```

`action_type` 例:

```text
submit_review
approve_weekly
edit
hold
reject
breaking_request
breaking_confirm
publish_breaking
publish_weekly
```

### `feedback_events`

読者側の「興味あり」を保存する。

```sql
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    discord_channel_id TEXT,
    discord_message_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES article_drafts(id),
    UNIQUE(article_id, user_id, action_type)
);
```

今回のUIでは `action_type = 'interested'` のみ使う。

将来的に以下を増やせる設計にする。

```text
interested
want_deep_dive
not_relevant
useful_for_work
want_implementation
```

### `delivery_events`

配信ログを記事単位で保存する。

```sql
CREATE TABLE IF NOT EXISTS delivery_events (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    cadence TEXT NOT NULL,
    publish_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    discord_channel_id TEXT,
    discord_message_id TEXT,
    message_text TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES article_drafts(id)
);
```

---

## 追加するStoreメソッド

既存の `NewsbotStore` に以下のようなメソッドを追加する。  
実際の命名は既存コードのスタイルに合わせる。

```python
create_or_update_draft_from_item(...)
list_pending_drafts(...)
get_draft(article_id)
update_draft_text(article_id, title_edited, body_edited, reviewer_user_id)
set_draft_status(article_id, status, reviewer_user_id, publish_type=None)
mark_review_message(article_id, channel_id, message_id)
mark_published(article_id, channel_id, message_id, publish_type)
record_review_action(article_id, reviewer_user_id, action_type, before_json=None, after_json=None)
record_feedback(article_id, user_id, action_type="interested", weight=1, channel_id=None, message_id=None)
list_approved_weekly(topic, cadence, limit=None)
record_delivery_event(...)
```

`record_feedback` は冪等にする。

- 同じ `article_id`、`user_id`、`action_type` が既に存在する場合は二重加算しない。
- Discord側には「記録済みです」またはephemeral messageで返す。
- 実装しやすければ `INSERT OR IGNORE` を使ってよい。

---

## Reviewer UI

Reviewer投稿先は `DISCORD_REVIEW_CHANNEL_ID`。

`submit-review` コマンドの役割:

1. Payload JSONを読み込む。
2. 既存の重複除外ロジックを可能な限り使う。
3. 新規候補を `article_drafts` に `status='pending'` として保存する。
4. 各候補をReviewerチャンネルへ投稿する。
5. 投稿したDiscord message IDを `article_drafts.review_message_id` に保存する。
6. `--dry-run` ではDiscord投稿せず、投稿予定内容を標準出力する。

Reviewer投稿の表示例:

```text
📝 配信候補 #<short_id>

カテゴリ: <category_primary or unknown>
重要度: <importance_score or normal>
想定: 週次ニュース

タイトル:
<title_edited>

本文案:
<body_edited>

出典:
<source_url>
```

Reviewer投稿に付けるボタン:

```text
[週次配信に追加]
[本文編集]
[特報として配信]
[保留]
[却下]
```

### ボタン動作

#### 週次配信に追加

- `status = approved_weekly`
- `publish_type = weekly`
- Reviewer投稿を編集して状態表示を更新する。
- `review_actions` に `approve_weekly` を記録する。
- Reviewer以外が押した場合はephemeralで拒否する。

#### 本文編集

- Discord Modalを開く。
- 入力欄:
  1. タイトル
  2. 本文
  3. カテゴリ/タグ、任意
  4. Reviewerメモ、任意

Modalの制約により本文が長すぎる場合は、タイトルと本文だけを優先する。  
DiscordのModal制約に入らない長文は、エラーにせず、短縮表示や再入力案内で処理する。

Modal送信後:

- `title_edited`
- `body_edited`
- `tags_json` または `category_primary`
- `updated_at`

を更新する。

さらにReviewer投稿の本文を編集後内容に更新する。  
`review_actions` に `edit` を記録する。

#### 特報として配信

誤爆防止のため、1段階確認を入れる。

1. `[特報として配信]` を押す。
2. ephemeral または確認用メッセージで以下を出す。

```text
この記事を特報として今すぐ配信しますか？
[特報配信を確定]
[キャンセル]
```

3. `[特報配信を確定]` で本配信チャンネルへ投稿する。

処理:

- `publish_type = breaking`
- `status = published`
- `published_at` を保存
- `delivery_events` に保存
- Reviewer投稿を「🚨 特報として配信済み」に更新
- `review_actions` に `publish_breaking` を記録

#### 保留

- `status = held`
- Reviewer投稿の状態を更新
- `review_actions` に `hold` を記録

#### 却下

- `status = rejected`
- Reviewer投稿の状態を更新
- `review_actions` に `reject` を記録

---

## 本配信UI

本配信先は `DISCORD_PUBLISH_CHANNEL_ID`。

### 週次配信

`publish-weekly` コマンドは、`status='approved_weekly'` の記事を配信する。

推奨:

- 1記事につき1メッセージで投稿する。
- 各記事メッセージに `[興味あり]` ボタンを付ける。
- なぜなら、記事単位でフィードバックを取りやすいため。

週次配信の投稿例:

```text
🧪 Lab Automation Weekly News

**<title_edited>**

<body_edited>

出典: <source_url>
```

### 特報配信

特報はReviewerのボタンから即時配信する。

投稿例:

```text
🚨 特報: Lab Automation News

**<title_edited>**

<body_edited>

出典: <source_url>
```

### 読者側ボタン

本配信メッセージには、まず1つだけボタンを付ける。

```text
[興味あり]
```

内部では以下のように記録する。

```text
action_type = interested
weight = 1
```

将来の拡張を想定して、ボタンcustom_idは以下のような構造にする。

```text
nb:feedback:interested:<article_id>
```

---

## Discord custom_id設計

Discordの `custom_id` は短く、判定しやすくする。  
UUIDを使う場合も100文字以内に収める。

推奨:

```text
nb:review:weekly:<article_id>
nb:review:edit:<article_id>
nb:review:breaking:<article_id>
nb:review:breaking_confirm:<article_id>
nb:review:hold:<article_id>
nb:review:reject:<article_id>
nb:feedback:interested:<article_id>
```

必ずparse関数を作り、テストする。

```python
parse_custom_id("nb:review:weekly:<id>")
```

想定外のcustom_idは無視またはephemeralでエラーにする。

---

## 権限管理

Reviewer操作は `DISCORD_REVIEWER_USER_IDS` に含まれるユーザーだけ許可する。

- 週次配信に追加
- 本文編集
- 特報として配信
- 保留
- 却下

上記はReviewer以外が押しても拒否する。

読者側の `[興味あり]` は誰でも押せる。

`DISCORD_ALLOWED_GUILD_ID` が設定されている場合、そのGuild外のInteractionは拒否する。

---

## レンダリング方針

既存の `render.py` に近い形で、Discord用の本文生成関数を追加する。

例:

```python
render_review_message(draft) -> str
render_published_message(draft, publish_type: Literal["weekly", "breaking"]) -> str
render_status_line(draft) -> str
```

長文はDiscordの制約に合わせて安全にtruncateする。

記事本文は既存の優先順位を参考にする。

- `material`
- `summary`
- `why_it_matters`
- `x_text`
- `title`

ただし、Reviewer編集後は必ず `title_edited` と `body_edited` を優先する。

---

## CLI追加仕様

### `submit-review`

```bash
python -m newsbot.cli submit-review --input payloads/latest.json
python -m newsbot.cli submit-review --input payloads/latest.json --dry-run
```

処理:

- payloadをvalidateする。
- 重複を除外する。
- 新規記事を `article_drafts` に保存する。
- reviewer channelへ候補投稿する。
- 結果を標準出力する。

出力例:

```text
Submitted review drafts: created=5, skipped=2, failed=0
Review channel: <channel_id>
```

### `publish-weekly`

```bash
python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly
python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly --dry-run
```

処理:

- `approved_weekly` のdraftを取得する。
- 各記事をpublish channelへ投稿する。
- 各記事に `[興味あり]` ボタンを付ける。
- 投稿成功後、`status='published'`、`publish_type='weekly'` に更新する。
- `delivery_events` に保存する。

出力例:

```text
Published weekly drafts: sent=5, failed=0
Publish channel: <channel_id>
```

### `run-discord-bot`

```bash
python -m newsbot.cli run-discord-bot
```

処理:

- Discord Botとしてログインする。
- reviewerボタン、modal、feedbackボタンを処理する。
- systemdやDocker Composeで常駐させることを想定する。

---

## ファイル追加・変更の候補

Codexは実際の構成を確認した上で決めること。  
以下は提案であり、既存スタイルに合わせてよい。

```text
newsbot/
  cli.py                  # コマンド追加
  db.py                   # schema/method追加
  discord_bot.py          # Bot本体、Views、Modals、interaction handlers
  discord_client.py       # Bot tokenによるREST投稿 helper
  review.py               # draft作成・review投稿ロジック
  feedback.py             # feedback保存ロジック
  render.py               # review/publish render追加
  models.py               # Draft model等を追加するならここ

tests/
  test_db_review.py
  test_custom_id.py
  test_render_review.py
  test_feedback.py

docs/
  discord-review-workflow.md
  linux-deployment.md
```

---

## Linux運用メモも追加する

`docs/linux-deployment.md` を追加し、最低限以下を書く。

- Ubuntu Serverでの実行例
- `.env` の配置
- `python -m newsbot.cli init-db`
- `python -m newsbot.cli run-discord-bot`
- systemd unit例

systemd unit例:

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

---

## テスト要件

最低限、以下のテストを追加・更新する。

### DB migration

- `init-db` で既存テーブルと新規テーブルが作成される。
- 既存の `notification_runs`、`notified_items`、`notification_messages` を壊さない。
- `article_drafts` に新規draftを作れる。
- 同一URL/同一event_hashの重複が増えない。

### custom_id

- `nb:review:weekly:<id>` を正しくparseできる。
- `nb:feedback:interested:<id>` を正しくparseできる。
- 不正なcustom_idで例外暴走しない。

### Reviewer action

- `weekly` ボタン相当の処理で `status='approved_weekly'` になる。
- `hold` で `status='held'` になる。
- `reject` で `status='rejected'` になる。
- `edit` で `title_edited`、`body_edited` が更新される。

### Feedback

- 同一ユーザーが同じ記事に複数回 `[興味あり]` を押しても1件だけ記録される。
- 別ユーザーなら別イベントとして記録される。

### Render

- review messageにタイトル、本文、出典が含まれる。
- breaking messageに `🚨 特報` が含まれる。
- weekly messageに `Weekly News` またはそれに相当するヘッダが含まれる。

---

## 互換性要件

- 既存のREADME記載コマンドが引き続き動くこと。
- `python -m newsbot.cli validate-payload --input ...` が壊れないこと。
- `python -m newsbot.cli notify --input ... --dry-run` が壊れないこと。
- 認証情報がなくてもdry-runは動くこと。
- Discord Bot tokenが未設定の場合、interactive系コマンドは明確なエラーを出すこと。
- Webhookのみ設定されている既存運用は維持すること。

---

## 実装上の注意

### 1. Discord interaction処理は小さく保つ

Discordイベントハンドラ内にDB更新・render・投稿処理を直書きしすぎない。  
以下のように分ける。

```text
discord_bot.py
  Interaction受信、View、Modal

review.py
  状態遷移、DB更新、配信判断

render.py
  メッセージ本文生成

db.py
  永続化
```

### 2. 状態遷移を明示する

以下のような不正遷移を避ける。

- `rejected` の記事を誤って配信しない。
- `published` 済み記事を二重配信しない。
- `held` は明示的にweeklyへ戻す操作がない限り配信しない。

### 3. 特報は二重配信を防止する

`breaking_confirm` 処理時には、DBから最新状態を再読込する。  
すでに `published` の場合は配信せず、「すでに配信済み」と返す。

### 4. エラーはReviewerチャンネルまたはephemeralでわかりやすく返す

例:

```text
配信に失敗しました。Bot token、channel ID、権限を確認してください。
```

内部ログには詳細を残すが、secretを出さない。

### 5. dry-runを重視する

新機能もdry-run可能にする。

- `submit-review --dry-run`
- `publish-weekly --dry-run`

実際のDiscord投稿を行わず、投稿予定本文と対象件数を表示する。

---

## README更新

`README.ja.md` と必要に応じて `README.md` を更新する。

追加する内容:

1. Reviewer承認フローの説明
2. 追加環境変数
3. 起動方法
4. 週次配信の流れ
5. 特報配信の流れ
6. 読者の「興味あり」フィードバックの説明
7. BigQueryはStep 2であること

READMEに書く想定コマンド:

```bash
python -m newsbot.cli init-db
python -m newsbot.cli submit-review --input payloads/latest.json --dry-run
python -m newsbot.cli submit-review --input payloads/latest.json
python -m newsbot.cli run-discord-bot
python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly --dry-run
python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly
```

---

## 完了条件

以下を満たしたら実装完了。

- 既存CLIが壊れていない。
- `init-db` で新規テーブルが作成される。
- `submit-review --dry-run` が動く。
- `submit-review` がReviewerチャンネルに候補を投稿できる。
- Reviewer投稿に以下のボタンが付いている。
  - 週次配信に追加
  - 本文編集
  - 特報として配信
  - 保留
  - 却下
- 本文編集でModalが開き、編集内容がDBとReviewer投稿に反映される。
- 特報として配信ボタンで確認後にpublish channelへ即時配信できる。
- `publish-weekly` でapproved_weeklyの記事を配信できる。
- 本配信メッセージに `[興味あり]` ボタンが付く。
- `[興味あり]` のクリックが `feedback_events` に保存される。
- 同一ユーザーの同一記事への興味ありは二重記録されない。
- Reviewer以外はReviewer操作できない。
- テストが追加され、既存テストも通る。
- READMEと`.env.example`が更新されている。
- BigQueryは実装していないが、将来エクスポートしやすいイベントログ設計になっている。

---

## 最後に実行して報告すること

実装後、以下を実行する。

```bash
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
python -m newsbot.cli init-db
python -m newsbot.cli validate-payload --input samples/lab_automation_weekly.sample.json
python -m newsbot.cli submit-review --input samples/lab_automation_weekly.sample.json --dry-run
python -m newsbot.cli publish-weekly --topic lab_automation --cadence weekly --dry-run
```

もしサンプルファイル名が実際と異なる場合は、存在するサンプルPayloadを使う。

最終報告では以下をまとめる。

```text
変更したファイル
追加したコマンド
追加したDBテーブル
追加した環境変数
テスト結果
手動確認が必要なDiscord Developer Portal設定
既知の制限
次のStep 2: BigQuery連携案
```
