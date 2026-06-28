# Codex CLI 自動記事検索メモ

Linuxやサーバー上でCodex Desktop Appを使わずに運用する場合は、Codex CLIの非対話モード `codex exec` を使います。

このリポジトリでは、`scripts/generate_payload_openai.py` が以下を担当します。

1. Codex CLIへ記事検索promptを渡す
2. Codexの最終回答からJSON payloadを抽出する
3. `payloads/<topic>_<cadence>_<YYYYMMDD_HHMMSS_microseconds>.json`へ保存する
4. `python -m newsbot.cli validate-payload`で検証する
5. 任意で`submit-review`まで実行し、Reviewerチャンネルへ投稿する

記事検索promptは、topicに対応する`prompts/<topic>.md`を編集します。
たとえば`--topic lab_automation`では`prompts/lab_automation.md`、`--topic stock_news`では`prompts/stock_news.md`が使われます。
該当ファイルがないtopicでは、スクリプト内蔵の最小promptに戻ります。

`lab_automation`は既定でPhase別multi-agent workflowを使います。
Phase別promptは`prompts/lab_automation/`配下にあります。
`prompts/lab_automation.md`は`--single-agent`指定時のfallback promptです。

## 事前準備

Codex CLIをLinuxサーバーにインストールし、ログインしておきます。

```bash
npm install -g @openai/codex
codex --login
codex exec --ephemeral "Say OK"
```

DiscordへReviewer投稿まで行う場合は、`.env`に以下も必要です。

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
```

## まずpromptだけ確認する

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --skip-codex
```

生成されたpromptは`payloads/lab_automation_weekly_<YYYYMMDD_HHMMSS_microseconds>.prompt.txt`に保存されます。
このファイルは`prompts/lab_automation.md`に実行時の期間、保存先、設定パス、Interested feedback profileを差し込んだ後の、Codexへ実際に渡されるpromptです。

`lab_automation`のmulti-agent workflowでは、以下のようなPhase別promptも保存されます。

```text
payloads/lab_automation_weekly_<timestamp>.phase_1.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_2.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_3.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_4.prompt.txt
payloads/lab_automation_weekly_<timestamp>.master.prompt.txt
```

## Codexでpayloadを生成し、検証だけ行う

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --validate-only
```

成功すると、以下のようなファイルができます。

```text
payloads/lab_automation_weekly_20260606_100000_123456.json
payloads/lab_automation_weekly_20260606_100000_123456.phase_1.json
payloads/lab_automation_weekly_20260606_100000_123456.phase_1.codex.log
payloads/lab_automation_weekly_20260606_100000_123456.master.json
payloads/lab_automation_weekly_20260606_100000_123456.master.codex.log
```

## Reviewer投稿まで自動で進める

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --submit-review
```

安全確認したい場合は、Reviewer投稿をdry-runにします。

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --submit-dry-run
```

## cron例

毎週月曜日08:00 JSTに実行する例です。

```cron
0 8 * * 1 cd /path/to/newsbot-automation && /path/to/conda/envs/newsbot/bin/python scripts/generate_payload_openai.py --topic lab_automation --cadence weekly --submit-review >> logs/codex-weekly.log 2>&1
```

cronは環境変数やPATHが通常のターミナルと違います。うまく動かない場合は、`python`と`codex`を絶対パスで指定してください。

```bash
which python
which codex
```

Codex CLIのパスが特殊な場合は、`--codex-bin`で指定できます。

```bash
python scripts/generate_payload_openai.py \
  --codex-bin /home/newsbot/.codex/bin/codex \
  --topic lab_automation \
  --cadence weekly \
  --submit-review
```

## Codex CLIのモデルとreasoningを指定する

Codex CLI のモデルを明示したい場合は、`--codex-model` を指定します。
reasoning effortを明示したい場合は、`--codex-reasoning-effort`を指定します。

```bash
python scripts/generate_payload_openai.py \
  --codex-model gpt-5 \
  --codex-reasoning-effort high \
  --topic lab_automation \
  --cadence weekly \
  --submit-review
```

`lab_automation`のmulti-agent workflowでは、Phase別・Master別にモデルとreasoning effortを指定できます。

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --phase-model phase_1=gpt-5 \
  --phase-reasoning phase_1=xhigh \
  --phase-model phase_4=gpt-5-mini \
  --phase-reasoning phase_4=high \
  --master-model gpt-5 \
  --master-reasoning-effort xhigh \
  --validate-only
```

`lab_automation`で従来の単一prompt方式に戻したい場合は、`--single-agent`を指定します。

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --single-agent \
  --validate-only
```

`.env` の `NEWSBOT_CODEX_MODEL` に設定すると、payload 生成と Discord の `Check Source` ボタンの両方で同じモデルを使います。未設定の場合は Codex CLI 側のデフォルトモデルを使います。

payload生成のreasoning effortは、`.env` の `NEWSBOT_CODEX_REASONING_EFFORT` に `low`, `medium`, `high`, `xhigh` のいずれかを指定できます。`lab_automation`のmulti-agent workflowでは、Phase別に `NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL`, `NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT` のように設定できます。Masterは `NEWSBOT_LAB_AUTOMATION_MASTER_MODEL`, `NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT` です。

生成時には、SQLite に保存された過去の `👍 Interested (n)` feedback からカテゴリ・タグ・ソースの傾向を抽出し、Codex へのプロンプトに含めます。Codex の出力後も同じ傾向を使って候補を並び替え、Reviewer に回す記事は優先度順の最大30本に絞ります。

cron で固定期間を検索したい場合は、`--lookback-days` を指定します。

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --lookback-days 3 \
  --submit-review
```

`.env` の `NEWSBOT_LOOKBACK_DAYS` でも同じ指定ができます。どちらも未指定の場合は、前回成功実行時から今回実行時までを検索します。初回だけ cadence のデフォルト期間を使います。

1st screening 後の final review 通知は、公開予定時刻に以下を cron などで実行します。

```bash
python -m newsbot.cli prepare-weekly-publish
```

実際の公開は、Reviewer が各 final review item を `Publish` または `Cancel` で判定し、必要に応じて最終一覧の `Reorder` Modal で順位を調整し、最後に Discord 上で `Publish Digest` を押したときだけ行われます。

## Codex CLIに追加オプションを渡す

`codex exec`へ追加オプションを渡したい場合は、`--codex-arg`を使います。
Newsbotのpayload生成では、Web検索を有効にするため`codex --search exec`で起動します。
そのため通常は`--codex-arg=--search`を指定する必要はありません。

```bash
python scripts/generate_payload_openai.py \
  --codex-arg=--json \
  --topic lab_automation \
  --cadence weekly \
  --validate-only
```

## 注意点

- Pythonスクリプトは`stdin=subprocess.DEVNULL`でCodex CLIを起動します。cronや非TTY環境で`codex exec`がstdin待ちになる事故を避けるためです。
- Codexの出力がJSONとして解釈できない場合、payloadは保存されずエラーになります。
- 自動で行うのはReviewer投稿またはfinal review通知までにするのがおすすめです。公開配信はDiscord上でReviewerが`Publish Digest`を押したときだけ行ってください。
- `payloads/`は`.gitignore`対象です。生成payloadはGitHubへは上げません。
