from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config, load_discord_settings, resolve_project_path
from .db import DEFAULT_DB_PATH, NewsbotStore
from .discord_bot import run_bot
from .discord_client import DiscordApiError, DiscordBotClient
from .feedback import feedback_components
from .missed_item import (
    format_missed_item_result,
    record_researched_missed_item,
    research_missed_item_with_codex,
)
from .models import NewsPayload
from .notifiers import NotificationError, send_discord, send_x_post
from .ranking import REVIEW_MAX_ITEMS, rank_news_items
from .render import (
    chunk_text,
    render_discord_payload,
    render_review_message,
    render_weekly_detail_message,
    render_weekly_digest_overview,
    order_weekly_drafts,
)
from .review import final_digest_components, final_weekly_components, review_components


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = resolve_project_path(path)
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


def load_payload(path: str | Path) -> NewsPayload:
    payload_path = resolve_project_path(path)
    with payload_path.open("r", encoding="utf-8") as handle:
        payload = NewsPayload.from_dict(json.load(handle))
    if not payload.topic:
        raise ValueError("Payload must include topic")
    if not payload.cadence:
        raise ValueError("Payload must include cadence")
    return payload


def validate_payload_against_config(args: argparse.Namespace) -> NewsPayload:
    config = load_config(args.config)
    payload = load_payload(args.input)
    if not config.has_topic_cadence(payload.topic, payload.cadence):
        raise ValueError(f"Unknown topic/cadence: {payload.topic}/{payload.cadence}")
    return payload


def cmd_init_db(args: argparse.Namespace) -> int:
    store = NewsbotStore(args.db)
    store.init()
    print(f"Initialized SQLite database: {store.path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    payload = validate_payload_against_config(args)
    print(f"Payload OK: {payload.topic}/{payload.cadence}, {len(payload.items)} items")
    return 0


def cmd_submit_review(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    payload = validate_payload_against_config(args)
    settings = load_discord_settings()
    store = NewsbotStore(args.db)
    store.init()

    if not args.dry_run:
        if not settings.bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is not set")
        if not settings.review_channel_id:
            raise ValueError("DISCORD_REVIEW_CHANNEL_ID is not set")

    client = DiscordBotClient(settings.bot_token) if not args.dry_run else None
    created = 0
    skipped = 0
    failed = 0

    preference_profile = store.audience_preference_profile(topic=payload.topic, cadence=payload.cadence)
    review_limit = min(config.max_items, REVIEW_MAX_ITEMS)
    selected_items = rank_news_items(payload.items, preference_profile=preference_profile, limit=review_limit)

    for item in selected_items:
        try:
            draft, is_new = store.create_or_update_draft_from_item(payload.topic, payload.cadence, item)
            should_submit = is_new or not draft.review_message_id
            if not should_submit:
                skipped += 1
                continue

            rendered = render_review_message(draft)
            if args.dry_run:
                print(rendered)
                print("")
            else:
                response = client.create_message(
                    settings.review_channel_id,
                    rendered,
                    components=review_components(draft.id),
                )
                store.mark_review_message(draft.id, settings.review_channel_id, str(response["id"]))
            latest = store.get_draft(draft.id)
            if latest is not None:
                store.record_review_action(
                    draft.id,
                    "system",
                    "submit_review",
                    after_json=latest.to_dict(),
                )
            created += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"submit-review: failed for item '{item.title}': {exc}", file=sys.stderr)

    print(f"Submitted review drafts: created={created}, skipped={skipped}, failed={failed}")
    print(f"Review channel: {settings.review_channel_id or '(dry-run)'}")
    return 0 if failed == 0 else 1


def cmd_record_missed(args: argparse.Namespace) -> int:
    store = NewsbotStore(args.db)
    store.init()
    domain = args.domain.strip() or urlsplit(args.url).netloc.lower()
    judgment_id = store.record_editorial_judgment(
        decision="missed", reason_code=args.reason_code, note=args.note,
        reviewer_user_id=args.reviewer, topic=args.topic, cadence=args.cadence,
        canonical_url=args.url, title=args.title, phase=args.phase,
        organization=args.organization, domain=domain,
    )
    print(f"Recorded missed item: {judgment_id}")
    return 0


def cmd_research_missed(args: argparse.Namespace) -> int:
    result = research_missed_item_with_codex(args.url)
    if args.dry_run:
        print(format_missed_item_result(result, dry_run=True))
        print("")
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    store = NewsbotStore(args.db)
    store.init()
    judgment_id = record_researched_missed_item(
        store,
        result,
        reviewer_user_id=args.reviewer,
        topic=args.topic,
        cadence=args.cadence,
    )
    print(format_missed_item_result(result, judgment_id=judgment_id))
    return 0


def cmd_publish_weekly(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.has_topic_cadence(args.topic, args.cadence):
        raise ValueError(f"Unknown topic/cadence: {args.topic}/{args.cadence}")

    settings = load_discord_settings()
    store = NewsbotStore(args.db)
    store.init()
    drafts = store.list_approved_weekly(args.topic, args.cadence)

    if not args.dry_run:
        if not settings.bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is not set")
        if not settings.publish_channel_id:
            raise ValueError("DISCORD_PUBLISH_CHANNEL_ID is not set")
    client = DiscordBotClient(settings.bot_token) if not args.dry_run else None

    sent = 0
    failed = 0
    if drafts:
        overview_text = render_weekly_digest_overview(drafts)
        if args.dry_run:
            print(overview_text)
            print("")
        else:
            client.create_message(settings.publish_channel_id, overview_text)

    for index, draft in enumerate(order_weekly_drafts(drafts), start=1):
        message_text = render_weekly_detail_message(draft, index, leading_gap=index == 1)
        if args.dry_run:
            print(message_text)
            print("")
            sent += 1
            continue

        try:
            before = store.get_draft(draft.id)
            response = client.create_message(
                settings.publish_channel_id,
                message_text,
                components=feedback_components(draft.id),
            )
            store.mark_published(draft.id, settings.publish_channel_id, str(response["id"]), "weekly")
            after = store.get_draft(draft.id)
            if before is not None and after is not None:
                store.record_review_action(
                    draft.id,
                    "system",
                    "publish_weekly",
                    before_json=before.to_dict(),
                    after_json=after.to_dict(),
                )
            store.record_delivery_event(
                article_id=draft.id,
                topic=draft.topic,
                cadence=draft.cadence,
                publish_type="weekly",
                destination="discord",
                status="sent",
                discord_channel_id=settings.publish_channel_id,
                discord_message_id=str(response["id"]),
                message_text=message_text,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            store.record_delivery_event(
                article_id=draft.id,
                topic=draft.topic,
                cadence=draft.cadence,
                publish_type="weekly",
                destination="discord",
                status="failed",
                discord_channel_id=settings.publish_channel_id or None,
                message_text=message_text,
                error_message=str(exc),
            )
            print(f"publish-weekly: failed for draft '{draft.id}': {exc}", file=sys.stderr)

    print(f"Published weekly drafts: sent={sent}, failed={failed}")
    print(f"Publish channel: {settings.publish_channel_id or '(dry-run)'}")
    return 0 if failed == 0 else 1


def cmd_prepare_weekly_publish(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.has_topic_cadence(args.topic, args.cadence):
        raise ValueError(f"Unknown topic/cadence: {args.topic}/{args.cadence}")

    settings = load_discord_settings()
    store = NewsbotStore(args.db)
    store.init()
    final_ready_drafts = store.list_final_ready_weekly(args.topic, args.cadence)
    ready_drafts = store.list_ready_weekly(args.topic, args.cadence)
    approved_drafts = store.list_approved_weekly(args.topic, args.cadence)
    drafts = ready_drafts + approved_drafts

    if not args.dry_run:
        if not settings.bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is not set")
        if not settings.review_channel_id:
            raise ValueError("DISCORD_REVIEW_CHANNEL_ID is not set")
    client = DiscordBotClient(settings.bot_token) if not args.dry_run else None

    if final_ready_drafts and not drafts:
        message_text = (
            "Final digest is ready.\n"
            "Review the order below. Use Reorder if needed, then click Publish Digest.\n\n"
            + render_weekly_digest_overview(final_ready_drafts)
        )
        if args.dry_run:
            print(message_text)
            print("")
        else:
            client.create_message(
                settings.review_channel_id,
                message_text,
                components=final_digest_components(final_ready_drafts[0].id),
            )
        print("Prepared weekly final reviews: prepared=0, failed=0")
        print("Prepared final digest preview: prepared=1, failed=0")
        print(f"Review channel: {settings.review_channel_id or '(dry-run)'}")
        return 0

    prepared = 0
    failed = 0
    for index, draft in enumerate(drafts, start=1):
        try:
            updated = store.set_draft_status(
                draft.id,
                "ready_weekly",
                "system",
                publish_type="weekly",
                scheduled_at=draft.scheduled_at or None,
            )
            message_text = (
                f"Final Weekly Review #{index}\n"
                "Choose Publish, Edit Draft, or Cancel. After all items are judged, Reorder and Publish Digest will appear.\n\n"
                + render_review_message(updated)
            )
            if args.dry_run:
                print(message_text)
                print("")
            else:
                response = client.create_message(
                    settings.review_channel_id,
                    message_text,
                    components=final_weekly_components(updated.id),
                )
                store.mark_review_message(updated.id, settings.review_channel_id, str(response["id"]))
            prepared += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"prepare-weekly-publish: failed for draft '{draft.id}': {exc}", file=sys.stderr)

    print(f"Prepared weekly final reviews: prepared={prepared}, failed={failed}")
    print(f"Review channel: {settings.review_channel_id or '(dry-run)'}")
    return 0 if failed == 0 else 1


def cmd_run_discord_bot(args: argparse.Namespace) -> int:
    run_bot(args.db)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    payload = load_payload(args.input)
    if not config.has_topic_cadence(payload.topic, payload.cadence):
        raise ValueError(f"Unknown topic/cadence: {payload.topic}/{payload.cadence}")

    destination = config.destination_for(payload.topic)
    store = NewsbotStore(args.db)
    store.init()
    selected = payload.items[: config.max_items]
    run_id = store.start_run(
        topic=payload.topic,
        cadence=payload.cadence,
        destination=destination.type,
        input_hash=payload.input_hash,
        item_count=len(selected),
        dry_run=args.dry_run,
    )

    sent_count = 0
    skipped_count = 0
    try:
        fresh_items = []
        for item in selected:
            reason = store.find_duplicate_reason(payload.topic, item)
            if reason:
                skipped_count += 1
                store.record_message(
                    run_id=run_id,
                    item_id=None,
                    topic=payload.topic,
                    destination=destination.type,
                    status="skipped",
                    message_text=item.title,
                    error_message=reason,
                )
                continue
            fresh_items.append(item)

        if destination.type == "discord":
            if fresh_items:
                message = render_discord_payload(payload, fresh_items)
                chunks = chunk_text(message)
                result_status = "dry_run" if args.dry_run else "sent"
                results = []
                if not args.dry_run:
                    for chunk in chunks:
                        result = send_discord(chunk)
                        results.append(result)
                        if result.status != "sent":
                            raise NotificationError(
                                result.error_message or result.response_body or "Discord send failed"
                            )
                if not args.dry_run:
                    for item in fresh_items:
                        store.record_item(run_id, payload.topic, item)
                for index, chunk in enumerate(chunks):
                    result = results[index] if results else None
                    store.record_message(
                        run_id=run_id,
                        item_id=None,
                        topic=payload.topic,
                        destination=destination.type,
                        status=result_status,
                        message_text=chunk,
                        response_code=result.response_code if result else None,
                        response_body=result.response_body if result else None,
                    )
                sent_count += len(fresh_items)
                print(message)
        elif destination.type == "x":
            limit = destination.post_limit_chars or 280
            if fresh_items:
                message = render_discord_payload(payload, fresh_items)
                chunks = chunk_text(message, limit)
                result_status = "dry_run" if args.dry_run else "sent"
                results = []
                if not args.dry_run:
                    for chunk in chunks:
                        result = send_x_post(chunk)
                        results.append(result)
                        if result.status != "sent":
                            raise NotificationError(result.error_message or result.response_body or "X send failed")
                    for item in fresh_items:
                        store.record_item(run_id, payload.topic, item)
                for index, chunk in enumerate(chunks):
                    result = results[index] if results else None
                    store.record_message(
                        run_id=run_id,
                        item_id=None,
                        topic=payload.topic,
                        destination=destination.type,
                        status=result_status,
                        message_text=chunk,
                        response_code=result.response_code if result else None,
                        response_body=result.response_body if result else None,
                    )
                sent_count += len(fresh_items)
                print(message)
                print()
        else:
            raise ValueError(f"Unsupported destination type: {destination.type}")
    except Exception as exc:  # noqa: BLE001
        store.finish_run(
            run_id,
            status="failed",
            sent_count=sent_count,
            skipped_count=skipped_count,
            error_message=str(exc),
        )
        raise

    status = "dry_run" if args.dry_run else "sent"
    if sent_count == 0 and skipped_count > 0:
        status = "skipped"
    store.finish_run(run_id, status=status, sent_count=sent_count, skipped_count=skipped_count)
    print(f"Run {run_id}: {status}; sent={sent_count}; skipped={skipped_count}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsbot")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Create or migrate the SQLite database")
    init_db.set_defaults(func=cmd_init_db)

    validate = subparsers.add_parser("validate-payload", help="Validate a notification payload JSON file")
    validate.add_argument("--input", required=True)
    validate.set_defaults(func=cmd_validate)

    submit_review = subparsers.add_parser("submit-review", help="Create review drafts and post them to the reviewer channel")
    submit_review.add_argument("--input", required=True)
    submit_review.add_argument("--dry-run", action="store_true")
    submit_review.set_defaults(func=cmd_submit_review)

    missed = subparsers.add_parser("record-missed", help="Record an editor-supplied missed article for future search feedback")
    missed.add_argument("--title", required=True)
    missed.add_argument("--url", required=True)
    missed.add_argument("--topic", default="lab_automation")
    missed.add_argument("--cadence", default="weekly")
    missed.add_argument("--phase", required=True, choices=["phase_1", "phase_2", "phase_3", "phase_4", "phase_5"])
    missed.add_argument("--reason-code", required=True)
    missed.add_argument("--note", default="")
    missed.add_argument("--organization", default="")
    missed.add_argument("--domain", default="")
    missed.add_argument("--reviewer", required=True)
    missed.set_defaults(func=cmd_record_missed)

    research_missed = subparsers.add_parser(
        "research-missed",
        help="Investigate one URL with Codex and optionally register it as a missed article",
    )
    research_missed.add_argument("--url", required=True)
    research_missed.add_argument("--topic", default="lab_automation")
    research_missed.add_argument("--cadence", default="weekly")
    research_missed.add_argument("--reviewer", required=True)
    research_missed.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the URL investigation but do not write an editorial judgment to SQLite",
    )
    research_missed.set_defaults(func=cmd_research_missed)

    publish_weekly = subparsers.add_parser("publish-weekly", help="Publish approved weekly drafts to Discord")
    publish_weekly.add_argument("--topic", default="lab_automation")
    publish_weekly.add_argument("--cadence", default="weekly")
    publish_weekly.add_argument("--dry-run", action="store_true")
    publish_weekly.set_defaults(func=cmd_publish_weekly)

    prepare_weekly = subparsers.add_parser(
        "prepare-weekly-publish",
        help="Post final weekly review messages before publishing",
    )
    prepare_weekly.add_argument("--topic", default="lab_automation")
    prepare_weekly.add_argument("--cadence", default="weekly")
    prepare_weekly.add_argument("--dry-run", action="store_true")
    prepare_weekly.set_defaults(func=cmd_prepare_weekly_publish)

    run_discord_bot = subparsers.add_parser("run-discord-bot", help="Run the interactive Discord reviewer bot")
    run_discord_bot.set_defaults(func=cmd_run_discord_bot)

    notify = subparsers.add_parser("notify", help="Legacy webhook/X delivery command")
    notify.add_argument("--input", required=True)
    notify.add_argument("--dry-run", action="store_true")
    notify.set_defaults(func=cmd_notify)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (DiscordApiError, NotificationError, ValueError, KeyError) as exc:
        print(f"newsbot: error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"newsbot: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
