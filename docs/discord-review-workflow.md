# Discord Review Workflow

## Overview

Newsbot uses two Discord channels:

- Reviewer channel: receives draft review and final review messages.
- Publish channel: receives approved weekly digest posts and breaking-news posts.

The Discord bot must be running:

```bash
python -m newsbot.cli run-discord-bot
```

## First Screening

Each draft review message includes:

- `Approve Weekly`
- `Edit Draft`
- `Rewrite Draft`
- `Check Source`
- `Publish Breaking`
- `Hold`
- `Reject`

Only users listed in `DISCORD_REVIEWER_USER_IDS` can use reviewer controls. If `DISCORD_ALLOWED_GUILD_ID` is set, interactions from other guilds are ignored.

`Edit Draft` can update title, body, category, tags, reviewer note, and source URL.

Editorial decisions use checkboxes with reasons specific to the action. Approval reasons start with `high_impact`; rejection reasons such as `duplicate`, `commercial_signal`, and `already_covered` are not offered for approval. Select one or more reasons. Selecting `other` also requires a note. All selected reasons are stored as one editorial decision and included in feedback statistics. Existing single-reason records remain supported.

`Rewrite Draft` opens a form with a separate model setting and optional writing preferences. Set `NEWSBOT_REWRITE_MODEL` to the model you want to use; this does not inherit any collection or phase model setting. If it is blank, enter a model in the form. The bot generates five titles and five bodies from the existing article and source evidence using Codex CLI. Read the five options, select a title and a body independently, check the combined preview, and click `Apply`. `Cancel` leaves the draft unchanged. Changes to the draft during generation/selection require generating new options before applying. The selection expires after ten minutes.

Optional settings: `NEWSBOT_REWRITE_REASONING_EFFORT=medium` and `NEWSBOT_REWRITE_TIMEOUT_SECONDS=300` (1–600 seconds). Rewriting preserves source URL, category, tags, and approval status; it does not publish the article.

## One Review Before Publishing

Set `NEWSBOT_WEEKLY_REVIEW_MODE=single_stage` in `.env` and restart the bot (recreate the service when using Docker Compose). Each `Approve Weekly` then selects the article directly for the digest. After reviewing the articles, click `Preview Digest` on a selected article, optionally `Reorder`, then `Publish Digest` to publish them together.

`/newsbot_prepare_weekly` and `prepare-weekly-publish` also post digest previews in this mode. Approvals left over from the two-stage mode are included without another per-article review. Unreviewed, held, rejected, and published articles are excluded. Different scheduled batches get separate previews. Approval alone never publishes anything.

The default `NEWSBOT_WEEKLY_REVIEW_MODE=two_stage` keeps the final per-article review described below. Changing the setting back does not revoke articles already selected for a digest.

`Check Source` runs Codex CLI to verify the current source URL. If Codex finds a better replacement URL for the same article or event, the draft source URL is updated and the review message is refreshed.

## Final Weekly Review

Run:

```bash
python -m newsbot.cli prepare-weekly-publish
```

Each final review candidate includes:

- `Publish`
- `Edit Draft`
- `Rewrite Draft`
- `Cancel`

After every candidate has been judged, the bot posts a final digest preview:

- `Reorder`: opens a private editor with numbered article titles and up/down/first/last buttons. Articles stay within News or Events. Page through 10 items at a time; the selection follows a moved article. Long titles are shortened in the list and shown in full above it when selected.
- `Publish Digest`: publishes the overview and detail messages in the displayed order.

Use `保存` (save) to commit the order and refresh the shared preview. `キャンセル` (cancel), 10 minutes of inactivity, or a bot restart discards unsaved changes. Changes to the digest's membership, order, or sections invalidate an open editor, which must be reopened. Order changes and review history are committed together. A message refresh failure after saving is reported as a successful save; no second save is needed.

Public weekly publishing happens only after `Publish Digest`.

## Reader Feedback

Published detail messages include:

- `👍 Interested (n)`

Feedback is recorded once per `(article_id, user_id, action_type)` pair. The button count updates so reviewers can see which articles are attracting reader interest.

## Stored Events

SQLite stores:

- `article_drafts`
- `review_actions`
- `feedback_events`
- `delivery_events`

The database is local operational data and should not be committed.

## Refreshing Existing Messages

Use `/newsbot_refresh_reviews` after deploying button or copy changes if older Discord review messages need to be redrawn with the latest controls.
