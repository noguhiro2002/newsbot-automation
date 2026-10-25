# Codex CLI 自動記事検索メモ

Linuxやサーバー上でCodex Desktop Appを使わずに運用する場合は、Codex CLIの非対話モード `codex exec` を使います。

このリポジトリでは、`scripts/generate_payload_openai.py` が以下を担当します。

1. Codex CLIへ記事検索promptを渡す
2. Codexの最終回答からJSON payloadを抽出する
3. `payloads/<topic>_<cadence>_<YYYYMMDD_HHMMSS_microseconds>.json`へ保存する
4. `python -m newsbot.cli validate-payload`で検証する
5. 任意で`submit-review`まで実行し、Reviewerチャンネルへ投稿する

単一prompt方式の記事検索promptは、topicに対応する`prompts/<topic>.md`を編集します。
たとえば`--topic stock_news`では`prompts/stock_news.md`が使われます。該当ファイルがないtopicでは、スクリプト内蔵の最小promptに戻ります。

`lab_automation`は既定でPhase別multi-agent workflowを使います。
Phase別promptは`prompts/lab_automation/`配下にあります。
`prompts/lab_automation.md`は`--topic lab_automation --single-agent`指定時だけ使うfallback promptです。

`prompts/lab_automation/`は現在の運用テンプレートであると同時に、別分野向けpromptの参考実装です。探索範囲、除外条件、source方針、Phase分割、論文API候補のLLM選別、Feedbackの扱い、JSON出力契約を具体例として参照できます。

新しいtopicへ`prompts/<topic>.md`を追加した場合は単一prompt方式で動作します。directoryをコピーしただけではmulti-agent workflowや論文API連携は有効にならず、`scripts/generate_payload_openai.py`のPhase定義とtopic分岐も拡張する必要があります。

## 事前準備

Codex CLIをLinuxサーバーにインストールし、ログインしておきます。

```bash
npm install -g @openai/codex
codex login --device-auth
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

デフォルトのmulti-agent workflowでは、`prompts/lab_automation/`の各テンプレートに実行時の期間、保存先、設定パス、Interested feedback profileを差し込んだ、Phase別とMasterの実promptが保存されます。`prompts/lab_automation.md`とtop-levelの`.prompt.txt`を使うのは、`--single-agent`を指定した場合だけです。

各Codex実行では、従来の`*.codex.log`に加えて次の構造化artifactも保存されます。

- `*.codex.events.jsonl`: `codex exec --json`の生イベント
- `*.codex.usage.json`: `turn.completed`から抽出したinput、cached input、output、reasoning output token集計

`*.codex.usage.json`の`usage`がnullの場合は、Codexが`turn.completed`まで到達しなかった失敗runです。

```text
payloads/lab_automation_weekly_<timestamp>.phase_1.structured.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_1.broad.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_2.structured.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_2.broad.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_3.structured.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_3.broad.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_4.paper_api.json
payloads/lab_automation_weekly_<timestamp>.phase_4.api.prompt.txt
payloads/lab_automation_weekly_<timestamp>.phase_4.broad.prompt.txt
payloads/lab_automation_weekly_<timestamp>.master.prompt.txt
```

## モデル監査用に各構成を3回ずつ実行する

dev版では、4つの比較構成を同一期間で3回ずつ連続実行するランナーを利用できます。
誤って有料実行を開始しないよう、`--execute`を付けない場合は計画表示だけで終了します。
Discordには投稿せず、各payloadのローカル検証まで行います。

```bash
scripts/run_llm_model_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)"
```

内容を確認後、実行します。

```bash
scripts/run_llm_model_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --execute
```

対象はGPT-5.5/high、GPT-5.6 Luna/high＋MasterのみTerra/medium、
GPT-5.6 Terra/high、GPT-5.6 Sol/highです。
途中のrunが失敗しても既定では残りを継続し、最後に成功・失敗を集計します。
payloadは`payloads/`、実行ログとrun一覧TSVは`logs/model-audit/<session-id>/`に保存されます。

### Lunaのtargeted tuning構成

既存のLuna hybridを基準に、変更点を一つだけにした次の2構成と、
複数項目を同時に変更した2構成を個別実行できます。

- `luna-master-high`: Phase 1〜4はLuna/high、MasterだけTerra/high
- `luna-phase1-xhigh`: Phase 1だけLuna/xhigh、Phase 2〜4はLuna/high、MasterはTerra/medium
- `luna-phase1-xhigh-master-high`: Phase 1はLuna/xhigh、Phase 2〜4はLuna/high、MasterはTerra/high
- `luna-all-phases-xhigh-master-high`: Phase 1〜4はLuna/xhigh、MasterはTerra/high

同時変更構成を、rate limitを考慮してまず1runだけ実行する例です。

```bash
scripts/run_llm_model_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --only luna-phase1-xhigh-master-high \
  --repeats 1 \
  --execute
```

原因を切り分けたい場合は、単独変更構成をそれぞれ実行します。

```bash
scripts/run_llm_model_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --only luna-master-high \
  --repeats 1 \
  --execute

scripts/run_llm_model_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --only luna-phase1-xhigh \
  --repeats 1 \
  --execute
```

同時変更構成は1runあたり5回のCodex実行で両方の変更を評価できますが、
結果が悪化した場合は、上記の単独変更構成で原因を切り分けます。
`--only`を指定しない従来の実行では、既存4構成だけを対象とする動作を維持します。

全PhaseをLuna/xhighにする構成を1runだけ試す場合は、次のように実行します。
token消費とrate limit負荷が大きくなる可能性があるため、まず`--repeats 1`を推奨します。

```bash
scripts/run_llm_model_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --only luna-all-phases-xhigh-master-high \
  --repeats 1 \
  --execute
```

Phase 4は、arXiv / PubMed / bioRxiv / medRxiv / ChemRxiv / Crossref APIの全通過候補をbatch判定するAPIレーンと、API候補・API query・watchlist・他Phase出力を受け取らないBroadレーンを別Codexセッションで並列実行します。両レーンはDOI、canonical URL、正規化titleで統合し、`api` / `broad` / 両方の来歴を保存します。bioRxiv / medRxivは30件単位の全ページを走査してから共通意味判定へ進みます。ChemRxivはCrossref `posted-content`だけを機械取得経路として使用し、現行・旧形式のChemRxiv DOIを判定してversion表記を除いた論文単位で統合します。`--disable-paper-api`指定時もBroadレーンは実行します。
PubMedを使うには、NCBIへ登録済みの`NEWSBOT_NCBI_TOOL`と`NEWSBOT_NCBI_EMAIL`を設定します。toolは空白を含まないアプリ識別名（例: `newsbot_automation`）、emailは運用者・開発者本人の有効な連絡先です。両方の値と開発者または組織名を`eutilities@ncbi.nlm.nih.gov`へ連絡して登録します。APIキーはHTTP requestだけに使われ、coverage・prompt・Codex logへ保存しません。clientはNCBI/Crossrefのrate limitとretry/backoff、および秘密を含まないAPI cacheを適用します。

NCBI E-utilitiesはAPI keyの有無にかかわらず3 request/秒以下に固定し、PubMed IDは1回のEFetchへまとめます。arXiv legacy APIは3秒に1回以下・同時接続1本に制限します。`data/api-cache/`配下のlockにより、同一ホスト上で複数runが重なっても間隔と単一接続を共有します。429/5xxと一時的な通信失敗にはrate limitを維持したままretry/backoffを適用します。

Phase 4は各API候補にIDを付け、採用候補の`paper_api_candidate_ids`または除外候補の`excluded_api_candidates`へ必ず振り分けます。未判定・二重判定・未知IDがあれば生成を失敗させます。除外結果は`payloads/lab_automation_weekly_<timestamp>.phase_4.rejections.json`にも保存されます。Masterには採用候補と集計だけを渡し、詳細な除外一覧は監査artifactに保持します。

PubMedとCrossrefも、正規化後にarXivと共通の高再現率な一次意味判定を通します。API別の取得・採用・除外・メタデータ不足件数と、一次除外理由・スコアは`.phase_4.paper_api.json`および`.phase_4.rejections.json`で確認できます。Phase 4は物理ロボット接続の有無だけで判断せず、ChemWorld型環境、科学Digital Twin、AI Scientist評価環境も、具体的な実験action・state・resource・replay・auditがあれば対象にします。

Phase 1〜3はstructuredとbroadを、Phase 4はAPIとbroadを、別々の`codex exec --ephemeral`として並列実行します。broadには他レーンの検索語・watchlist・候補・feed候補を渡しません。各レーンは失敗時に1回再試行し、片方だけ成功した場合も継続します。RSS/Atom/sitemap候補はPhase 1〜3のstructuredだけへ渡し、Pythonがcanonical URL、duplicate key、正規化タイトルで統合して`discovery_modes`を保存します。Phase 5はPhase 1〜4の出力を受け取らず、国内外・公式・PR・論文を横断して探索します。Phase 1〜5には固定候補上限を設けず、既定30件はMaster・Reviewer選定段階だけに適用します。Phase 5だけを確認する場合は`--only-phase phase_5`を使用できます。

Phase 4だけをテストする場合は、次を実行します。Master builder、最終payload生成、Discord投稿は行いません。

```bash
.venv/bin/python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --lookback-days 7 \
  --only-phase phase_4 \
  --phase-model phase_4=gpt-5.6-luna \
  --phase-reasoning phase_4=max \
  --codex-timeout 5400
```

### 固定候補poolによるMaster paired audit

2つの完走済みrunのPhase候補を統合・URL重複排除し、候補順序だけを
3つのseedで変更します。各seedの候補pool JSONは一度だけ作られ、
同じファイルが次の6条件へ渡されます。

- Terra/medium、Terra/high
- Sol/medium、Sol/high
- GPT-5.5/high
- Luna/high

まずpaid run数と条件を確認します。

```bash
scripts/run_master_paired_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --source-run payloads/<LUNA_HIGH_RUN_R2>.json \
  --source-run payloads/<LUNA_HIGH_RUN_R3>.json
```

内容を確認後、実行します。

```bash
scripts/run_master_paired_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --source-run payloads/<LUNA_HIGH_RUN_R2>.json \
  --source-run payloads/<LUNA_HIGH_RUN_R3>.json \
  --execute
```

既定では候補順序3種 × Master 6条件の18 turnです。rate limitに合わせて
1条件または1 seedだけ実行しても、同じsource run・seedなら候補順序digestは
再現されます。

```bash
scripts/run_master_paired_audit.sh \
  --period "2026-07-18 to 2026-07-25 (JST)" \
  --source-run payloads/<LUNA_HIGH_RUN_R2>.json \
  --source-run payloads/<LUNA_HIGH_RUN_R3>.json \
  --only terra-high \
  --seed 101 \
  --execute
```

MasterのWeb検索は無効化され、固定pool外URLの採用はローカル検証で拒否されます。
候補pool、Masterのraw payload、token usage、候補順序digest、manifestは
`logs/master-paired-audit/<session-id>/`へ保存され、Discordには投稿されません。

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

本番DB、feed cache、成功Run時刻、Discordから完全に分離してPhase 1〜5とMasterを実行する場合は、`--isolated-dry-run`を使用します。本番DBは実行開始時にdry-run専用SQLiteへsnapshotされるため、既存の人判断フィードバックは判定に反映されますが、実行中のDB書き込みはsnapshotだけに保存されます。最終payload、Phase別artifact、監査情報、DB snapshotは`payloads/`へ残ります。

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --lookback-days 7 \
  --codex-timeout 5400 \
  --isolated-dry-run
```

## cronへ登録する

ローカル版では付属のinstallerを使用します。

```bash
sudo scripts/install_cron_jobs.sh \
  --app-dir /opt/newsbot-automation \
  --user newsbot
```

現在のデフォルトは、候補生成を毎朝08:00 JSTに起動し、前回成功から2日以上経過した場合だけ実行する設定です。final review準備は毎週月曜08:00 JSTです。`.env`の`NEWSBOT_GENERATE_REVIEW_*`、`NEWSBOT_PREPARE_WEEKLY_*`、`NEWSBOT_PYTHON_BIN`、`NEWSBOT_CODEX_BIN`を変更した場合はinstallerを再実行してください。

Docker版ではhost側cronを併用せず、`docker compose --profile scheduler up -d`でcontainer schedulerを起動します。

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

payload生成のreasoning effortは、`.env` の `NEWSBOT_CODEX_REASONING_EFFORT` に `low`, `medium`, `high`, `xhigh`, `max` のいずれかを指定できます。`lab_automation`のmulti-agent workflowでは、Phase別に `NEWSBOT_LAB_AUTOMATION_PHASE_1_MODEL`, `NEWSBOT_LAB_AUTOMATION_PHASE_1_REASONING_EFFORT` のように設定できます。Masterは `NEWSBOT_LAB_AUTOMATION_MASTER_MODEL`, `NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT` です。

`--paper-api-retmax` / `NEWSBOT_PAPER_API_RETMAX` は旧名称を維持したPubMed/Crossref走査budgetであり、意味判定通過後の候補上限ではありません。arXivは対象期間を日単位に分け、投稿日・対象カテゴリ・広い自律実験シグナルで取得した後、ローカルの高再現率filterを通します。LLM判定は `--phase-4-llm-batch-size` または `NEWSBOT_PHASE_4_LLM_BATCH_SIZE`（既定50件）ごとに分割され、通過候補を切り捨てず全batchを判定後に統合します。PubMed用の `NCBI_API_KEY` は任意ですが、登録済みtool/emailは必須です。Crossrefでは`NEWSBOT_CROSSREF_MAILTO`の設定を推奨します。

生成時には、SQLiteの`👍 Interested (n)`を読者信号として、編集者の採用・除外・配信取消・見逃しを別信号として扱います。編集判断は`NEWSBOT_FEEDBACK_LOOKBACK_WEEKS`（未設定時12）のrolling windowで再集計し、使用期間・件数・profile hashをRun監査へ保存します。不正な値は起動時に明示的エラーになります。見逃しはDiscordの管理者限定 `/newsbot_record_missed url:<URL>`、または `python -m newsbot.cli research-missed --url ... --reviewer ...` でURLだけから調査・登録できます。モデルは`NEWSBOT_MISSED_ITEM_MODEL`（既定`gpt-5.6-luna`）、reasoningは`NEWSBOT_MISSED_ITEM_REASONING_EFFORT`（既定`high`）で変更できます。

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

- PythonスクリプトはpromptをCodex CLIの標準入力へ渡します。prompt本文をprocessのcommand lineへ載せないため、`ps`などからの露出を避けられます。
- Codex子processへ渡す環境変数はallowlist方式です。Discord・X・NCBI credentialは継承しません。
- Web探索を維持するため`--search`を使いますが、sandbox modeは強制しません。専用の低権限userまたはhardening済みcontainerで実行し、人間によるsource確認を残してください。
- Codexの出力がJSONとして解釈できない場合、payloadは保存されずエラーになります。
- 自動で行うのはReviewer投稿またはfinal review通知までにするのがおすすめです。公開配信はDiscord上でReviewerが`Publish Digest`を押したときだけ行ってください。
- `payloads/`は`.gitignore`対象です。生成payloadはGitHubへは上げません。
