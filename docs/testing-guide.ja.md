# Newsbot テスト手順書

この手順書では、Codex CLI + Discord Bot レビューワークフローの自動テストと、Discord 上で確認する手動E2Eテストをまとめます。

## 1. 自動テスト

リポジトリのルートで実行します。

自動testにはCodex標準入力呼び出しとcredential分離、論文APIのredaction・
cache権限・retry、Docker schedulerとhealthcheckも含まれます。

```bash
uv sync --frozen --all-extras
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

CIと同じ公開前checkも実行します。

```bash
python scripts/private_repo_check.py
python scripts/check_markdown_links.py
bash -n scripts/*.sh
docker compose config --quiet
docker compose build
```

repository checkerは現在のtracked/untracked公開対象だけでなく、到達可能な
Git履歴全体も検査します。

CLI の基本動作も確認できます。

```bash
python -m newsbot.cli init-db
python -m newsbot.cli validate-payload --input samples/lab_automation_weekly.sample.json
python -m newsbot.cli submit-review --input samples/lab_automation_weekly.sample.json --dry-run
python -m newsbot.cli prepare-weekly-publish --dry-run
```

## 2. Discord 手動テストの準備

`.env` に以下が入っていることを確認します。

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
```

DB を初期化し、E2E 用 payload を検証します。

```bash
python -m newsbot.cli init-db
python -m newsbot.cli validate-payload --input samples/discord_review_e2e.sample.json
```

## 3. Bot を起動する

ターミナルAで起動します。

```bash
python -m newsbot.cli run-discord-bot
```

`Discord bot connected as ...` が表示され、traceback が出ないことを確認します。このターミナルは手動テスト中ずっと起動したままにします。

## 4. Review Draft を投稿する

ターミナルBで実行します。

```bash
python -m newsbot.cli submit-review --input samples/discord_review_e2e.sample.json
```

Reviewer チャンネルに6件の draft が投稿され、各メッセージに英語の操作ボタンが表示されればOKです。

## 5. Reviewer 操作を確認する

`[E2E-EDIT]`:

1. `Edit Draft` を押します。
2. タイトル、本文、category/tags を変更して modal を送信します。
3. 修正完了通知だけでなく、修正後の draft と同じ操作メニューが再表示されることを確認します。
4. 元の reviewer メッセージも更新されることを確認します。

`Check Source`:

1. Codex CLI が `codex` として実行できることを確認します。必要な場合は `.env` の `NEWSBOT_CODEX_BIN` を設定します。
2. 任意の review draft で `Check Source` を押します。
3. `Source URL checked.` または `Source URL updated.` と表示されることを確認します。
4. replacement URL が見つかった場合、draft の `Source:` が新しい URL に更新されることを確認します。

`[E2E-WEEKLY]`:

1. `Approve Weekly` を押します。
2. 承認メッセージに次回の配信予定日時が表示されることを確認します。
3. `Cancel Weekly` と `Edit Draft` が表示されることを確認します。
4. 元の reviewer メッセージの status が weekly 承認済みに変わることを確認します。

`[E2E-HOLD]`:

1. `Hold` を押します。
2. held 状態の draft と、保留以外へ変更できる操作メニューが表示されることを確認します。
3. 元の reviewer メッセージの status が held に変わることを確認します。

`[E2E-REJECT]`:

1. `Reject` を押します。
2. rejected 状態の draft と、却下以外へ変更できる操作メニューが表示されることを確認します。
3. 元の reviewer メッセージの status が rejected に変わることを確認します。

`[E2E-BREAKING-CANCEL]`:

1. `Publish Breaking` を押します。
2. `Cancel` を押します。
3. Publish チャンネルに投稿されないことを確認します。

`[E2E-BREAKING-CONFIRM]`:

1. `Publish Breaking` を押します。
2. `Confirm Breaking Publish` を押します。
3. Publish チャンネルに breaking 投稿が出ることを確認します。
4. Reviewer メッセージの status が published に変わることを確認します。

## 6. 週次配信を確認する

`[E2E-WEEKLY]` を approve したあとに実行します。

```bash
python -m newsbot.cli prepare-weekly-publish --dry-run
python -m newsbot.cli prepare-weekly-publish
```

Reviewer チャンネルに final review が再投稿され、`Publish`、`Edit Draft`、`Cancel` が表示されればOKです。各記事を `Publish` または `Cancel` で判定し、すべての判定が揃うと `Reorder` と `Publish Digest` 付きの最終一覧が表示されます。順番を変えない場合はそのまま `Publish Digest`、変える場合は `Reorder` を押して `3,1,2,4` のように現在の番号を希望順で入力してから `Publish Digest` を押します。Publish チャンネルには週次概要が1件投稿され、その後に各ニュースの詳細投稿が続きます。各詳細投稿には英語の `👍 Interested (0)` ボタンが表示されます。Discord のリンクプレビューカードは抑制され、`Source:` のURLだけがリンクとして残ります。

## 7. 読者 feedback を確認する

週次配信されたメッセージで確認します。

1. `👍 Interested (0)` を押します。
2. 押した読者だけに ephemeral の短い確認が出ることを確認します。
3. ボタン表示が `👍 Interested (1)` のように増えることを確認します。
4. 同じユーザーでもう一度押します。
5. すでに記録済みの ephemeral 通知だけが出て、カウンターが増えないことを確認します。

別ユーザーで押すと、ユーザーごとに1回だけ記録され、カウンターが増えることも確認できます。

## 8. 管理者用 slash command

Bot 起動中に、reviewer ユーザーで以下を実行します。

- `/newsbot_next_weekly`: 次回の週次配信日時を表示します。
- `/newsbot_weekly_queue`: 週次配信予定の記事一覧を表示します。
- `/newsbot_held`: hold した記事一覧を表示します。
- `/newsbot_rejected`: reject した記事一覧を表示します。
- `/newsbot_cancelled`: weekly 承認を cancel した記事一覧を表示します。
- `/newsbot_report`: `scripts/newsbot_test_report.py` に近い簡易DBレポートを表示します。

## 9. DB を確認する

```bash
python scripts/newsbot_test_report.py
```

期待値:

- `article_drafts`: 6件以上
- `review_actions`: `submit_review`, `edit`, `approve_weekly`, `cancel_weekly`, `hold`, `reject`, `breaking_request`, `breaking_confirm`, `publish_breaking`, `publish_weekly` が確認できる
- `delivery_events`: breaking と weekly の配送履歴が確認できる
- `feedback_events`: article/user ごとに `interested` が1件だけ保存される

## 10. 合格条件

- 自動テストが通る。
- reviewer ボタンがすべて traceback なしで応答する。
- Edit 後に修正後 draft と操作メニューが再表示される。
- Weekly 承認後に配信予定日時、Cancel、Edit が表示される。
- Hold/Reject 後も別の操作へ変更できる。
- Published message の feedback は読者だけに通知され、`👍 Interested (n)` カウンターが更新される。
- slash command で管理用の状態確認ができる。
