# Newsbot Test Guide

This guide covers automated tests and manual Discord checks for the Codex CLI + Discord Bot review workflow.

## 1. Test Scope

Automated tests cover:

- Payload parsing and URL normalization
- SQLite schema and store methods
- Review draft lifecycle helpers
- Feedback de-duplication
- Discord custom ID generation and parsing
- Message rendering
- CLI flows for `validate-payload`, `submit-review`, `prepare-weekly-publish`, Codex payload generation, and legacy `notify`
- Mocked Discord REST posting without real network calls
- Codex standard-input invocation and child-process credential isolation
- Paper API redaction, cache permissions, and retry behavior
- Docker scheduler and container health-check helpers

Manual Discord tests cover:

- Bot startup
- Review message posting
- `Approve Weekly`
- `Edit Draft`
- `Publish Breaking` with Cancel
- `Publish Breaking` with Confirm
- `Hold`
- `Reject`
- Published-message `Interested` feedback
- Admin slash commands such as `/newsbot_weekly_queue` and `/newsbot_report`

## 2. Run Automated Tests

From the repository root:

```bash
uv sync --frozen --all-extras
python -m compileall newsbot tests scripts
python -m unittest discover -s tests
```

Run the same public-release checks used by CI:

```bash
python scripts/private_repo_check.py
python scripts/check_markdown_links.py
bash -n scripts/*.sh
docker compose config --quiet
docker compose build
```

The repository checker intentionally scans all reachable Git history as well as
the current tracked and untracked release files.

You can also test the basic CLI path:

```bash
python -m newsbot.cli init-db
python -m newsbot.cli validate-payload --input samples/lab_automation_weekly.sample.json
python -m newsbot.cli submit-review --input samples/lab_automation_weekly.sample.json --dry-run
python -m newsbot.cli prepare-weekly-publish --dry-run
```

## 3. Prepare Manual Discord Test

Use a test Discord server or temporary test channels if possible.

Confirm `.env` contains:

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_REVIEW_CHANNEL_ID=
DISCORD_PUBLISH_CHANNEL_ID=
DISCORD_REVIEWER_USER_IDS=
DISCORD_ALLOWED_GUILD_ID=
```

Initialize the database:

```bash
python -m newsbot.cli init-db
```

Validate the end-to-end test payload:

```bash
python -m newsbot.cli validate-payload --input samples/discord_review_e2e.sample.json
```

## 4. Start the Bot

Open terminal A:

```bash
python -m newsbot.cli run-discord-bot
```

Expected result:

- The terminal prints `Discord bot connected as ...`
- No traceback appears after startup

Keep this terminal running during the manual test.

## 5. Submit Review Drafts

Open terminal B:

```bash
python -m newsbot.cli submit-review --input samples/discord_review_e2e.sample.json
```

Expected result:

- The reviewer channel receives 6 draft messages.
- Each message has reviewer buttons.
- The command prints `Submitted review drafts: created=6, skipped=0, failed=0` on a fresh database.

If you have already run the test once, existing review messages may be skipped. Use a fresh database or change the sample URLs if you need a completely clean repeat.

## 6. Manual Reviewer Actions

Use the draft title prefixes to choose the right message.

For `[E2E-EDIT]`:

1. Click `Edit Draft`.
2. Change the title, body, and source URL.
3. Add or change category/tags.
4. Submit the modal.
5. Confirm Discord shows the updated draft again with the same action buttons.
6. Confirm the original reviewer message also updates.

For source URL verification:

1. Make sure Codex CLI is installed and available as `codex`, or set `NEWSBOT_CODEX_BIN`.
2. Click `Check Source` on any review draft.
3. Confirm the response says `Source URL checked.` or `Source URL updated.`.
4. If Codex finds a replacement URL, confirm the updated draft shows the new `Source:` value.

If older reviewer messages do not show `Check Source`, run `/newsbot_refresh_reviews` in Discord to redraw stored review messages with the latest controls.

For `[E2E-WEEKLY]`:

1. Click `Approve Weekly`.
2. Confirm the response includes the scheduled publish time.
3. Confirm the response includes `Cancel Weekly` and `Edit Draft`.
4. Confirm the reviewer message status changes to approved weekly.

For `[E2E-HOLD]`:

1. Click `Hold`.
2. Confirm the response shows the held draft and offers other actions.
3. Confirm the reviewer message status changes to held.

For `[E2E-REJECT]`:

1. Click `Reject`.
2. Confirm the response shows the rejected draft and offers other actions.
3. Confirm the reviewer message status changes to rejected.

For `[E2E-BREAKING-CANCEL]`:

1. Click `Publish Breaking`.
2. Click `Cancel`.
3. Confirm nothing is posted to the publish channel.

For `[E2E-BREAKING-CONFIRM]`:

1. Click `Publish Breaking`.
2. Click `Confirm Breaking Publish`.
3. Confirm a breaking post appears in the publish channel.
4. Confirm the reviewer message status changes to published.

## 7. Final Weekly Review and Publish

After approving `[E2E-WEEKLY]`, run:

```bash
python -m newsbot.cli prepare-weekly-publish --dry-run
python -m newsbot.cli prepare-weekly-publish
```

Expected result:

- The dry run prints the final review text.
- The live command posts final review messages to the reviewer channel.
- The final review message includes `Publish`, `Edit Draft`, and `Cancel`.
- For each item, click `Publish` or `Cancel`, using `Edit Draft` when text needs work.
- After every item has been judged, confirm the bot posts a final digest preview with `Reorder` and `Publish Digest`.
- If the displayed order is fine, click `Publish Digest`. To change it, click `Reorder`, enter the current item numbers in the desired order such as `3,1,2,4`, then click `Publish Digest`.
- The live button action posts a weekly overview to the publish channel, followed by one detailed post per news item.
- Each detailed published message includes an English `👍 Interested (0)` button.
- Discord link preview cards are suppressed; the source URL remains visible as a link.
- The command prints `Prepared weekly final reviews: prepared=1, failed=0` if only the E2E weekly item is approved.

## 8. Test Reader Feedback

On the published weekly message:

1. Click `👍 Interested (0)`.
2. Confirm only that reader sees a small ephemeral confirmation.
3. Confirm the button counter increments, for example `👍 Interested (1)`.
4. Click `👍 Interested` again with the same user.
5. Confirm only that reader sees the already-recorded response and the counter does not increase.

Optional:

- Ask another user to click `Interested`.
- Confirm the second user creates another feedback event.

## 9. Admin Slash Commands

While the bot is running, use these commands in Discord as a reviewer:

- `/newsbot_next_weekly`: shows the next weekly publish time.
- `/newsbot_weekly_queue`: lists drafts approved for the next weekly publish.
- `/newsbot_held`: lists held drafts.
- `/newsbot_rejected`: lists rejected drafts.
- `/newsbot_cancelled`: lists drafts whose weekly approval was cancelled.
- `/newsbot_report`: shows a compact database report similar to `scripts/newsbot_test_report.py`.

## 10. Inspect Test Database

Run:

```bash
python scripts/newsbot_test_report.py
```

Expected database state after the full manual test:

- `article_drafts`: at least 6 rows
- `review_actions`: includes `submit_review`, `edit`, `approve_weekly`, `hold`, `reject`, `breaking_request`, `breaking_confirm`, `publish_breaking`, and `publish_weekly`
- `delivery_events`: includes one `breaking` delivery and one `weekly` delivery
- `feedback_events`: includes one `interested` event per user per article

## 11. Clean Repeat

For a completely clean manual test, use a separate database:

```bash
python -m newsbot.cli --db data/newsbot-e2e.sqlite init-db
python -m newsbot.cli --db data/newsbot-e2e.sqlite submit-review --input samples/discord_review_e2e.sample.json
python scripts/newsbot_test_report.py --db data/newsbot-e2e.sqlite
```

When using a separate database, start the bot with the same database:

```bash
python -m newsbot.cli --db data/newsbot-e2e.sqlite run-discord-bot
```

## 12. Pass Criteria

The full workflow passes when:

- Automated tests pass.
- Review drafts are posted to the reviewer channel.
- All reviewer buttons respond without tracebacks.
- Edit modal changes persist and update the review message.
- Weekly approval goes through final review, then publishes only after every item is judged and `Publish Digest` is clicked.
- Breaking confirm publishes immediately.
- Breaking cancel does not publish.
- Hold and Reject update status only.
- Hold and Reject responses allow changing to another reviewer action.
- Published feedback is stored once per user per article and updates the `👍 Interested (n)` counter.
- Admin slash commands return useful ephemeral status output.
- `scripts/newsbot_test_report.py` shows the expected actions and delivery events.
