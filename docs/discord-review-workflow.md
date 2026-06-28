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
- `Check Source`
- `Publish Breaking`
- `Hold`
- `Reject`

Only users listed in `DISCORD_REVIEWER_USER_IDS` can use reviewer controls. If `DISCORD_ALLOWED_GUILD_ID` is set, interactions from other guilds are ignored.

`Edit Draft` can update title, body, category, tags, reviewer note, and source URL.

`Check Source` runs Codex CLI to verify the current source URL. If Codex finds a better replacement URL for the same article or event, the draft source URL is updated and the review message is refreshed.

## Final Weekly Review

Run:

```bash
python -m newsbot.cli prepare-weekly-publish
```

Each final review candidate includes:

- `Publish`
- `Edit Draft`
- `Cancel`

After every candidate has been judged, the bot posts a final digest preview:

- `Reorder`: opens a Discord Modal. Enter the current item numbers in the desired order, for example `3,1,2,4`.
- `Publish Digest`: publishes the overview and detail messages in the displayed order.

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
