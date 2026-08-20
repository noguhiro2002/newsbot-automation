# Newsbot Automation HowTo

このガイドでは、公開版で推奨する運用を説明します。Codex CLI がcronなどでニュースを収集し、Discord上でReviewerが確認・編集し、最終承認後にBotが週次ダイジェストを公開します。

## 1. 必要なもの

- Python 3.11+
- Discord Bot token
- Discord review channel ID
- Discord publish channel ID
- Reviewerとして許可するDiscord user ID
- インストール・ログイン済みのCodex CLI

Codex CLI:

```bash
npm install -g @openai/codex
codex login --device-auth
codex exec --ephemeral "Say OK"
```

OpenAI公式ドキュメント:

- [OpenAI Codex CLI getting started](https://help.openai.com/en/articles/11096431)
- [Codex CLI sign in with ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

## 2. Pythonアプリをローカルへインストールする

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

Docker Composeを使う場合は、このローカル手順の代わりに[Docker Compose Install](docker-install.ja.md)を参照してください。

## 3. `.env` を設定する

`.env.example` を `.env` にコピーします。

```bash
install -m 600 .env.example .env
```

設定項目:

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
NEWSBOT_PHASE_4_LLM_BATCH_SIZE=50
NEWSBOT_NCBI_TOOL=newsbot_automation
NEWSBOT_NCBI_EMAIL=your-email@example.com
NCBI_API_KEY=
NEWSBOT_CROSSREF_MAILTO=
NEWSBOT_API_CACHE_TTL_SECONDS=21600
NEWSBOT_LOOKBACK_DAYS=
```

補足:

- `DISCORD_REVIEWER_USER_IDS` はカンマ区切りです。
- `DISCORD_ALLOWED_GUILD_ID` は任意ですが、公開・共有環境では設定を推奨します。
- `NEWSBOT_CODEX_BIN` が空の場合は `PATH` 上の `codex` を使います。
- `NEWSBOT_CODEX_MODEL` が空の場合は Codex CLI 側のデフォルトモデルを使います。
- `NEWSBOT_CODEX_REASONING_EFFORT` は `low`, `medium`, `high`, `xhigh`, `max` を指定できます。空の場合は Codex CLI 側のデフォルトです。
- `NEWSBOT_LAB_AUTOMATION_PHASE_<N>_MODEL` と `NEWSBOT_LAB_AUTOMATION_PHASE_<N>_REASONING_EFFORT` で、lab_automation multi-agent のPhase別デフォルトを指定できます。
- `NEWSBOT_LAB_AUTOMATION_MASTER_MODEL` と `NEWSBOT_LAB_AUTOMATION_MASTER_REASONING_EFFORT` で、Master統合のデフォルトを指定できます。
- `NEWSBOT_PAPER_API_RETMAX` は旧名称を維持したPubMed/Crossrefの走査budgetです。空の場合は50ですが、意味判定通過後の候補を切り捨てる上限ではありません。
- `NEWSBOT_PHASE_4_LLM_BATCH_SIZE` は1回のPhase 4 LLM呼び出しで判定するAPI候補数です。空の場合は50で、候補が多い場合は全候補を複数batchに分けて判定・統合します。
- `NEWSBOT_NCBI_TOOL` は空白を含まない、このアプリを識別する名前です。通常は `newsbot_automation` のままで構いません。
- `NEWSBOT_NCBI_EMAIL` は運用者・開発者本人の有効な連絡先メールアドレスです。第三者利用者のメールアドレスは指定しません。
- PubMedを継続利用する場合は、使用するtool名、email、開発者または組織名を `eutilities@ncbi.nlm.nih.gov` に連絡して登録してください。
- `NCBI_API_KEY` は任意です。このクライアントはキー設定時も3リクエスト/秒以下に制限します。PubMed IDはまとめてEFetchし、API呼び出し数を抑えます。
- `NEWSBOT_CROSSREF_MAILTO`を設定するとCrossref polite poolを使います。
- API responseは`data/api-cache/`へ`NEWSBOT_API_CACHE_TTL_SECONDS`の期間cacheします。
- arXiv legacy APIは3秒に1回以下、同時接続1本に制限します。レート制御は`data/api-cache/`のlockを介して同一ホスト上の複数実行で共有します。
- `NEWSBOT_LOOKBACK_DAYS` が空の場合は、前回成功実行時から今回実行時までを検索します。

## 4. DBを初期化する

```bash
python -m newsbot.cli init-db
```

SQLite DB は `data/` 配下に作成されます。公開リポジトリにはコミットしないでください。

## 5. Discord Botを起動する

```bash
python -m newsbot.cli run-discord-bot
```

Reviewerのボタン操作、編集Modal、Source確認、最終順位変更、公開、読者feedbackを処理するため、Botは起動したままにします。

## 6. Codex CLIでReview候補を作る

```bash
python scripts/generate_payload_openai.py --topic lab_automation --submit-review
```

流れ:

1. スクリプトがニュース検索promptを作る。
2. Codex CLI が`codex --search exec`で最近のニュースを検索する。
3. JSON payload を `payloads/` に保存する。
4. payload を検証する。
5. 最大30件の候補をReviewer Discordチャンネルへ投稿する。

単一prompt方式のニュース検索promptは`prompts/<topic>.md`を編集します。
例: `--topic stock_news`では`prompts/stock_news.md`を使います。該当するpromptファイルがないtopicでは、スクリプト内蔵の最小promptを使います。

`lab_automation`は既定でPhase別multi-agent workflowを使います。
Phase別promptは`prompts/lab_automation/`配下を編集します。
`prompts/lab_automation.md`は`--topic lab_automation --single-agent`指定時だけ使うfallback promptです。

`prompts/lab_automation/`は、現在のLab Automation運用に使うテンプレートであると同時に、別分野向けのpromptを設計する際の詳細な参考実装です。探索範囲と除外条件、Phase分割、source方針、論文API候補の選別、Feedbackの扱い、JSON出力契約を置き換えて利用してください。

新しいtopicの`prompts/<topic>.md`を追加すると、通常は単一prompt方式で動作します。directoryをコピーするだけではmulti-phase workflowは有効になりません。別topicで同じPhase構成を使うには、`scripts/generate_payload_openai.py`のPhase定義とtopic分岐も拡張する必要があります。

Codexを呼ばずにpromptだけ確認する場合:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --skip-codex
```

Phase 1〜5とMasterを本番DB・成功Run時刻・Discordから完全に分離して実行する場合:

```bash
python scripts/generate_payload_openai.py \
  --topic lab_automation \
  --cadence weekly \
  --lookback-days 7 \
  --codex-timeout 5400 \
  --isolated-dry-run
```

デフォルトのmulti-phase workflowでは、Phase 1〜3は`.phase_N.structured.prompt.txt`と`.phase_N.broad.prompt.txt`に分かれ、別Codexセッションで並列実行されます。Phase 4も`.phase_4.api*.prompt.txt`と`.phase_4.broad.prompt.txt`に分かれ、API取得・batch判定とAPI非参照の独立論文検索を並列実行します。各試行は`.attempt_1` / `.attempt_2`付きのprompt・log・events・usageとして保存され、成功試行は従来の`.codex.log`等にも複製されます。Broad出力のquery一覧が誤って数値になった場合は、実際のCodex Web検索eventから文字列配列を復元して監査へ残します。Phase 5は`.phase_5.prompt.txt`、統合は`.master.prompt.txt`です。feed取得監査は`.feeds.audit.json`、レーン別時間・候補・Phase 5評価・feedback windowは`.run.audit.json`に保存されます。Phase 5は他Phaseの結果を受け取らずにPhase横断検索を行います。
同じ日に複数回実行しても、実行時刻入りの別ファイルとして残ります。通常実行ではPhase別に`.json`、`.codex.log`、`.codex.events.jsonl`、`.codex.usage.json`も保存されます。
Phase 4では、arXiv / PubMed / bioRxiv / medRxiv / ChemRxiv / Crossref のAPI候補を共通の高再現率な一次意味判定へ通し、通過候補を件数で切り捨てず全件batch判定します。同時に、API候補・API query・watchlist・Phase 1〜3出力を渡さないBroadレーンが論文・preprintを白紙から検索します。両レーンはDOI、canonical URL、正規化titleで統合し、`api` / `broad` / 両方の来歴を保存します。API監査は`.phase_4.paper_api.json`、統合結果は`.phase_4.json`です。bioRxiv / medRxivは30件単位の全ページを取得後に判定します。ChemRxivはCrossref `posted-content`を唯一の機械取得経路とし、現行の`10.26434/chemrxiv.`と旧形式の`10.26434/chemrxiv-`をローカル判定したうえで、`/v2`、`-v2`、`.v2`を除いた論文単位で統合します。`--disable-paper-api`指定時もBroadレーンは実行します。
Phase 1〜5には固定の候補出力上限を設けません。検証済みの適格候補はすべてMasterへ渡し、既定30件の制限は最終Master・Reviewer選定段階だけで適用します。APIページ走査の安全上限とPhase 4 LLM batch sizeは運用制御として維持しますが、意味判定通過候補を捨てる制限ではありません。
論文API clientはrate limitとretry/backoffを適用します。ChemRxiv取得を含むCrossrefアクセスはPublic 5 request/秒・同時1接続または`mailto`付きPolite 10 request/秒（実装上は同時1接続）以下に制限し、429と`Retry-After`へ従います。coverageやpromptへ保存するquery metadataはredactされるため、`NCBI_API_KEY`は成果物やCodexへ渡りません。PubMed利用前に[Legal Notices](legal-notices.md)を確認してください。

固定日数分だけ検索する場合:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --lookback-days 3 --submit-review
```

同じReview候補作成はDiscordからも手動実行できます。Reviewer権限のあるユーザーが `/newsbot_collect_reviews` を実行すると、検索範囲の確認画面が表示されます。`Edit` で `topic`、`cadence`、`lookback days`、明示的な `period` を修正し、`Start` を押すと `generate_payload_openai.py --submit-review` が実行されます。処理中はReviewチャンネルにStatusメッセージが投稿され、進行状況と直近ログが更新されます。

`lab_automation`で従来の単一prompt方式を使う場合:

```bash
python scripts/generate_payload_openai.py --topic lab_automation --single-agent --validate-only
```

## 7. Discordで1st screeningを行う

Reviewerは各draftに対して以下を選べます。

- `Approve Weekly`: 次回週次ダイジェスト候補にする。
- `Edit Draft`: タイトル、本文、カテゴリ、タグ、メモ、source URLを修正する。
- `Check Source`: Codex CLIでsource URLを確認・修正する。
- `Publish Breaking`: 確認後に速報として公開する。
- `Hold`: 保留にする。
- `Reject`: 却下する。

Reviewer操作は `DISCORD_REVIEWER_USER_IDS` に含まれるユーザーだけが実行できます。

見逃し記事は、Reviewer権限のある管理者が `/newsbot_record_missed url:<URL>` を実行すると、URLだけから登録できます。BotはCodexでページと関連する一次情報を調査し、正規URL、タイトル、組織、Phase、公開日、関連性の根拠を取得します。Lab Automationとの具体的な関連を確認できた場合だけ、`search_miss` として編集判断DBへ保存します。調査結果は実行者だけにephemeral表示され、通常チャンネルには投稿されません。調査モデル、reasoning、timeoutは `NEWSBOT_MISSED_ITEM_MODEL`、`NEWSBOT_MISSED_ITEM_REASONING_EFFORT`、`NEWSBOT_MISSED_ITEM_TIMEOUT_SECONDS` で設定できます。

DiscordやDBへ書き込まず、同じURL調査だけを確認する場合:

```bash
python -m newsbot.cli research-missed \
  --url 'https://example.com/news-item' \
  --reviewer 'dry-run' \
  --dry-run
```

## 8. Final weekly reviewを行う

週次候補を公開前に再確認する場合:

```bash
python -m newsbot.cli prepare-weekly-publish
```

Botがfinal reviewメッセージを投稿します。各記事に対して以下を選びます。

- `Publish`: 最終ダイジェストに入れる。
- `Edit Draft`: 判断前に本文やsourceを修正する。
- `Cancel`: pending reviewへ戻す。

全件の判断が揃うと、Botが最終一覧を投稿します。

- `Reorder`: Discord Modalを開き、`3,1,2,4` のように現在の番号を希望順で入力します。
- `Publish Digest`: 表示順のまま公開します。

実際の公開は `Publish Digest` を押したときだけ行われます。

全件判断済みなのに最終一覧が出ていない場合は、`prepare-weekly-publish` をもう一度実行してください。選択済みの記事を戻さず、最終一覧だけを再投稿します。

同じ操作はDiscordからも実行できます。Reviewer権限のあるユーザーが `/newsbot_prepare_weekly` を実行すると、`prepare-weekly-publish` と同じfinal reviewメッセージをReviewチャンネルに投稿します。

## 9. 公開されるダイジェスト

Botは以下の順に投稿します。

1. 日付範囲とタイトル一覧を含む概要投稿。
2. 各ニュースの詳細投稿。
3. 各詳細投稿の `👍 Interested (n)` ボタン。

Discordのリンクプレビューカードは抑制され、source URLだけが表示されます。

## 10. Botの常駐と定期実行

ローカル版のDiscord Botはsystemdへ登録できます。installerはserviceをその場で起動し、OS起動時の自動起動も有効化します。

```bash
sudo scripts/install_systemd_service.sh \
  --env venv \
  --app-dir /opt/newsbot-automation \
  --user newsbot \
  --group newsbot
```

Review候補生成とfinal review準備は、付属のcron installerで登録します。

```bash
sudo scripts/install_cron_jobs.sh \
  --app-dir /opt/newsbot-automation \
  --user newsbot
```

現在のデフォルトは、候補生成を毎朝08:00 JSTに起動して前回成功から2日以上経過した場合だけ実行し、final reviewを毎週月曜08:00 JSTに準備する設定です。schedule、topic、cadence、Python、Codex CLIのパスは`.env`の`NEWSBOT_*`設定で変更し、変更後にinstallerを再実行してください。

systemdとcronの完全な手順は[Linux Deployment](linux-deployment.md)を参照してください。Docker版ではhost側cronを併用せず、[Docker Compose Install](docker-install.ja.md)のcontainer schedulerを使用してください。

## 11. 管理用Discordコマンド

Discordで使えます。

- `/newsbot_next_weekly`
- `/newsbot_weekly_queue`
- `/newsbot_held`
- `/newsbot_rejected`
- `/newsbot_cancelled`
- `/newsbot_report`
- `/newsbot_refresh_reviews`
- `/newsbot_prepare_weekly`
- `/newsbot_collect_reviews`
- `/newsbot_record_missed url:<URL>`

## 12. 動作確認

```bash
python scripts/private_repo_check.py
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
python -m newsbot.cli validate-payload --input samples/lab_automation_weekly.sample.json
python -m newsbot.cli submit-review --input samples/lab_automation_weekly.sample.json --dry-run
python -m newsbot.cli prepare-weekly-publish --dry-run
```

## 13. Advanced / Legacy Delivery

旧 `notify` コマンドでは、`DISCORD_WEBHOOK_URL` や X credential を使った通知も残っています。ただし、通常運用では Discord Bot のレビューパイプラインを推奨します。
