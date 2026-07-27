from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import PROJECT_ROOT, resolve_project_path
from .models import ArticleDraft, NewsItem, canonicalize_url, normalize_title, sha256_text, stable_json, title_similarity


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "newsbot.sqlite"
SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize_json(value: str | dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return stable_json(value)


def _event_hash_for_url_or_title(url: str, title: str) -> str:
    return sha256_text(canonicalize_url(url) or normalize_title(title))


class NewsbotStore:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = resolve_project_path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        conn = sqlite3.connect(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            conn.close()
            raise
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_runs (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    cadence TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS notified_items (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_notified_at TEXT NOT NULL,
                    last_run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(topic, canonical_url),
                    UNIQUE(topic, event_hash),
                    FOREIGN KEY(last_run_id) REFERENCES notification_runs(id)
                );

                CREATE TABLE IF NOT EXISTS notification_messages (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    item_id TEXT,
                    topic TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_code INTEGER,
                    response_body TEXT,
                    message_text TEXT NOT NULL,
                    error_message TEXT,
                    sent_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES notification_runs(id),
                    FOREIGN KEY(item_id) REFERENCES notified_items(id)
                );

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

                CREATE INDEX IF NOT EXISTS idx_notified_items_topic_seen
                    ON notified_items(topic, last_notified_at);
                CREATE INDEX IF NOT EXISTS idx_messages_run
                    ON notification_messages(run_id);
                CREATE INDEX IF NOT EXISTS idx_article_drafts_status
                    ON article_drafts(status, topic, cadence, created_at);
                CREATE INDEX IF NOT EXISTS idx_review_actions_article
                    ON review_actions(article_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_feedback_article
                    ON feedback_events(article_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_delivery_article
                    ON delivery_events(article_id, created_at);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )

    def _row_to_draft(self, row: sqlite3.Row) -> ArticleDraft:
        return ArticleDraft(
            id=str(row["id"]),
            topic=str(row["topic"]),
            cadence=str(row["cadence"]),
            source_name=str(row["source_name"] or ""),
            source_url=str(row["source_url"]),
            canonical_url=str(row["canonical_url"]),
            event_hash=str(row["event_hash"]),
            title_original=str(row["title_original"]),
            body_original=str(row["body_original"]),
            title_edited=str(row["title_edited"]),
            body_edited=str(row["body_edited"]),
            category_primary=str(row["category_primary"] or ""),
            tags_json=str(row["tags_json"] or "[]"),
            importance_score=float(row["importance_score"] or 0),
            status=str(row["status"]),
            publish_type=str(row["publish_type"] or ""),
            priority=str(row["priority"] or "normal"),
            review_channel_id=str(row["review_channel_id"] or ""),
            review_message_id=str(row["review_message_id"] or ""),
            published_channel_id=str(row["published_channel_id"] or ""),
            published_message_id=str(row["published_message_id"] or ""),
            source_payload_json=str(row["source_payload_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            scheduled_at=str(row["scheduled_at"] or ""),
            published_at=str(row["published_at"] or ""),
        )

    def _draft_for_item(self, topic: str, item: NewsItem) -> ArticleDraft | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM article_drafts
                WHERE topic = ? AND (canonical_url = ? OR event_hash = ?)
                LIMIT 1
                """,
                (topic, item.canonical_url, item.event_hash),
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def create_or_update_draft_from_item(self, topic: str, cadence: str, item: NewsItem) -> tuple[ArticleDraft, bool]:
        now = utc_now()
        source_name = item.source
        body_original = item.draft_body
        tags_json = stable_json(list(item.tags))
        source_payload_json = stable_json(item.raw or {})

        existing = self._draft_for_item(topic, item)
        if not existing:
            article_id = str(uuid.uuid4())
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO article_drafts(
                        id, topic, cadence, source_name, source_url, canonical_url, event_hash,
                        title_original, body_original, title_edited, body_edited,
                        category_primary, tags_json, importance_score, status, publish_type,
                        priority, source_payload_json, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?)
                    """,
                    (
                        article_id,
                        topic,
                        cadence,
                        source_name,
                        item.url,
                        item.canonical_url,
                        item.event_hash,
                        item.title,
                        body_original,
                        item.title,
                        body_original,
                        item.category_primary or None,
                        tags_json,
                        item.importance_score,
                        item.priority or "normal",
                        source_payload_json,
                        now,
                        now,
                    ),
                )
            draft = self.get_draft(article_id)
            if draft is None:
                raise RuntimeError("Failed to create article draft")
            return draft, True

        preserve_title = existing.title_edited if existing.title_edited != existing.title_original else item.title
        preserve_body = existing.body_edited if existing.body_edited != existing.body_original else body_original
        preserve_category = existing.category_primary or item.category_primary or None
        preserve_tags_json = existing.tags_json if existing.tags_json != "[]" else tags_json
        preserve_importance = existing.importance_score or item.importance_score
        preserve_priority = existing.priority if existing.priority != "normal" else item.priority or "normal"

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_drafts
                SET cadence = ?,
                    source_name = ?,
                    source_url = ?,
                    canonical_url = ?,
                    event_hash = ?,
                    title_original = ?,
                    body_original = ?,
                    title_edited = ?,
                    body_edited = ?,
                    category_primary = ?,
                    tags_json = ?,
                    importance_score = ?,
                    priority = ?,
                    source_payload_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    cadence,
                    source_name,
                    item.url,
                    item.canonical_url,
                    item.event_hash,
                    item.title,
                    body_original,
                    preserve_title,
                    preserve_body,
                    preserve_category,
                    preserve_tags_json,
                    preserve_importance,
                    preserve_priority,
                    source_payload_json,
                    now,
                    existing.id,
                ),
            )
        draft = self.get_draft(existing.id)
        if draft is None:
            raise RuntimeError("Failed to update article draft")
        return draft, False

    def list_pending_drafts(self, topic: str | None = None, cadence: str | None = None, limit: int | None = None) -> list[ArticleDraft]:
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if cadence:
            clauses.append("cadence = ?")
            params.append(cadence)
        sql = f"""
            SELECT * FROM article_drafts
            WHERE {' AND '.join(clauses)}
            ORDER BY importance_score DESC, created_at ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_review_message_drafts(self) -> list[ArticleDraft]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM article_drafts
                WHERE review_message_id IS NOT NULL
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_published_message_drafts(self) -> list[ArticleDraft]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM article_drafts
                WHERE status = 'published' AND published_message_id IS NOT NULL
                ORDER BY published_at DESC, created_at DESC
                """
            ).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def get_draft(self, article_id: str) -> ArticleDraft | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM article_drafts WHERE id = ?", (article_id,)).fetchone()
        return self._row_to_draft(row) if row else None

    def update_draft_text(
        self,
        article_id: str,
        title_edited: str,
        body_edited: str,
        reviewer_user_id: str,
        *,
        category_primary: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        reviewer_note: str | None = None,
        source_url: str | None = None,
    ) -> ArticleDraft:
        before = self.get_draft(article_id)
        if before is None:
            raise KeyError(f"Draft not found: {article_id}")

        after_tags_json = before.tags_json if tags is None else stable_json(list(tags))
        after_category = before.category_primary if category_primary is None else category_primary
        after_source_url = before.source_url if source_url is None else source_url.strip()
        after_canonical_url = canonicalize_url(after_source_url)
        after_event_hash = _event_hash_for_url_or_title(after_source_url, title_edited or before.title_original)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_drafts
                SET title_edited = ?,
                    body_edited = ?,
                    category_primary = ?,
                    tags_json = ?,
                    source_url = ?,
                    canonical_url = ?,
                    event_hash = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    title_edited,
                    body_edited,
                    after_category,
                    after_tags_json,
                    after_source_url,
                    after_canonical_url,
                    after_event_hash,
                    now,
                    article_id,
                ),
            )
        after = self.get_draft(article_id)
        if after is None:
            raise RuntimeError("Failed to update draft text")
        after_payload: dict[str, Any] = after.to_dict()
        if reviewer_note:
            after_payload["reviewer_note"] = reviewer_note
        self.record_review_action(
            article_id,
            reviewer_user_id,
            "edit",
            before_json=before.to_dict(),
            after_json=after_payload,
        )
        return after

    def update_draft_source_url(
        self,
        article_id: str,
        source_url: str,
        reviewer_user_id: str,
        *,
        reason: str | None = None,
    ) -> ArticleDraft:
        before = self.get_draft(article_id)
        if before is None:
            raise KeyError(f"Draft not found: {article_id}")

        next_source_url = source_url.strip()
        if not next_source_url:
            raise ValueError("Source URL is required")
        next_canonical_url = canonicalize_url(next_source_url)
        next_event_hash = _event_hash_for_url_or_title(next_source_url, before.title_edited or before.title_original)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_drafts
                SET source_url = ?, canonical_url = ?, event_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_source_url, next_canonical_url, next_event_hash, utc_now(), article_id),
            )
        after = self.get_draft(article_id)
        if after is None:
            raise RuntimeError("Failed to update draft source URL")
        after_payload = after.to_dict()
        if reason:
            after_payload["source_check_reason"] = reason
        self.record_review_action(
            article_id,
            reviewer_user_id,
            "check_source",
            before_json=before.to_dict(),
            after_json=after_payload,
        )
        return after

    def set_draft_status(
        self,
        article_id: str,
        status: str,
        reviewer_user_id: str,
        publish_type: str | None = None,
        scheduled_at: str | None = None,
    ) -> ArticleDraft:
        before = self.get_draft(article_id)
        if before is None:
            raise KeyError(f"Draft not found: {article_id}")

        next_publish_type = before.publish_type
        if publish_type is not None:
            next_publish_type = publish_type
        elif status in {"ready_weekly", "final_ready"}:
            next_publish_type = publish_type or before.publish_type or "weekly"
        elif status in {"held", "rejected", "pending"}:
            next_publish_type = ""

        published_at = before.published_at
        if status == "published":
            published_at = utc_now()

        next_scheduled_at = before.scheduled_at
        if scheduled_at is not None:
            next_scheduled_at = scheduled_at
        elif status in {"held", "pending", "rejected", "published"}:
            next_scheduled_at = ""

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_drafts
                SET status = ?, publish_type = ?, published_at = ?, scheduled_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_publish_type or None,
                    published_at or None,
                    next_scheduled_at or None,
                    utc_now(),
                    article_id,
                ),
            )
        after = self.get_draft(article_id)
        if after is None:
            raise RuntimeError("Failed to update draft status")

        action_type = {
            "approved_weekly": "approve_weekly",
            "ready_weekly": "prepare_weekly_publish",
            "final_ready": "ready_go",
            "held": "hold",
            "rejected": "reject",
        }.get(status)
        if status == "published":
            action_type = "publish_breaking" if after.publish_type == "breaking" else "publish_weekly"
        if action_type:
            self.record_review_action(
                article_id,
                reviewer_user_id,
                action_type,
                before_json=before.to_dict(),
                after_json=after.to_dict(),
            )
        return after

    def adjust_draft_priority(
        self,
        article_id: str,
        reviewer_user_id: str,
        action_type: str,
    ) -> ArticleDraft:
        if action_type not in {"priority_top", "priority_up", "priority_down"}:
            raise ValueError(f"Unsupported priority action: {action_type}")
        before = self.get_draft(article_id)
        if before is None:
            raise KeyError(f"Draft not found: {article_id}")

        with self.connect() as conn:
            if action_type == "priority_top":
                row = conn.execute(
                    """
                    SELECT MAX(importance_score) AS max_score
                    FROM article_drafts
                    WHERE topic = ? AND cadence = ? AND status IN ('approved_weekly', 'ready_weekly', 'final_ready')
                    """,
                    (before.topic, before.cadence),
                ).fetchone()
                next_score = float(row["max_score"] or before.importance_score or 0) + 0.05
            elif action_type == "priority_up":
                next_score = float(before.importance_score or 0) + 0.05
            else:
                next_score = max(0.0, float(before.importance_score or 0) - 0.05)
            conn.execute(
                """
                UPDATE article_drafts
                SET importance_score = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_score, utc_now(), article_id),
            )
        after = self.get_draft(article_id)
        if after is None:
            raise RuntimeError("Failed to update draft priority")
        self.record_review_action(
            article_id,
            reviewer_user_id,
            action_type,
            before_json=before.to_dict(),
            after_json=after.to_dict(),
        )
        return after

    def reorder_final_ready_weekly(
        self,
        topic: str,
        cadence: str,
        ordered_article_ids: list[str],
        reviewer_user_id: str,
        scheduled_at: str | None = None,
    ) -> list[ArticleDraft]:
        current = (
            self.list_final_ready_weekly_batch(topic, cadence, scheduled_at)
            if scheduled_at is not None
            else self.list_final_ready_weekly(topic, cadence)
        )
        current_ids = [draft.id for draft in current]
        if sorted(current_ids) != sorted(ordered_article_ids):
            raise ValueError("Reorder list must include every final-ready item exactly once")
        before_json = [draft.to_dict() for draft in current]
        total = len(ordered_article_ids)
        with self.connect() as conn:
            for index, article_id in enumerate(ordered_article_ids):
                score = float(total - index)
                conn.execute(
                    """
                    UPDATE article_drafts
                    SET importance_score = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (score, utc_now(), article_id),
                )
        after = (
            self.list_final_ready_weekly_batch(topic, cadence, scheduled_at)
            if scheduled_at is not None
            else self.list_final_ready_weekly(topic, cadence)
        )
        for draft in after:
            self.record_review_action(
                draft.id,
                reviewer_user_id,
                "reorder_digest",
                before_json=before_json,
                after_json=[item.to_dict() for item in after],
            )
        return after

    def mark_review_message(self, article_id: str, channel_id: str, message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_drafts
                SET review_channel_id = ?, review_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (channel_id, message_id, utc_now(), article_id),
            )

    def mark_published(self, article_id: str, channel_id: str, message_id: str, publish_type: str) -> ArticleDraft:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_drafts
                SET status = 'published',
                    publish_type = ?,
                    published_channel_id = ?,
                    published_message_id = ?,
                    scheduled_at = NULL,
                    published_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (publish_type, channel_id, message_id, utc_now(), utc_now(), article_id),
            )
        draft = self.get_draft(article_id)
        if draft is None:
            raise RuntimeError("Failed to mark draft as published")
        return draft

    def record_review_action(
        self,
        article_id: str,
        reviewer_user_id: str,
        action_type: str,
        before_json: str | dict[str, Any] | None = None,
        after_json: str | dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO review_actions(
                    id, article_id, reviewer_user_id, action_type, before_json, after_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    article_id,
                    reviewer_user_id,
                    action_type,
                    _normalize_json(before_json),
                    _normalize_json(after_json),
                    utc_now(),
                ),
            )

    def record_feedback(
        self,
        article_id: str,
        user_id: str,
        action_type: str = "interested",
        weight: int = 1,
        channel_id: str | None = None,
        message_id: str | None = None,
    ) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO feedback_events(
                    id, article_id, user_id, action_type, weight,
                    discord_channel_id, discord_message_id, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    article_id,
                    user_id,
                    action_type,
                    weight,
                    channel_id,
                    message_id,
                    utc_now(),
                ),
            )
        return cursor.rowcount > 0

    def feedback_count(self, article_id: str, action_type: str = "interested") -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM feedback_events
                WHERE article_id = ? AND action_type = ?
                """,
                (article_id, action_type),
            ).fetchone()
        return int(row["count"] if row else 0)

    def audience_preference_profile(
        self,
        *,
        topic: str | None = None,
        cadence: str | None = None,
        action_type: str = "interested",
        limit: int = 30,
    ) -> dict[str, Any]:
        clauses = ["feedback_events.action_type = ?"]
        params: list[Any] = [action_type]
        if topic:
            clauses.append("article_drafts.topic = ?")
            params.append(topic)
        if cadence:
            clauses.append("article_drafts.cadence = ?")
            params.append(cadence)
        sql = f"""
            SELECT article_drafts.*, COUNT(feedback_events.id) AS feedback_count
            FROM article_drafts
            JOIN feedback_events ON feedback_events.article_id = article_drafts.id
            WHERE {' AND '.join(clauses)}
            GROUP BY article_drafts.id
            ORDER BY feedback_count DESC, article_drafts.published_at DESC, article_drafts.created_at DESC
            LIMIT ?
        """
        params.append(limit)
        categories: dict[str, int] = {}
        tags: dict[str, int] = {}
        sources: dict[str, int] = {}
        examples: list[dict[str, Any]] = []
        total_feedback = 0
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        for row in rows:
            draft = self._row_to_draft(row)
            count = int(row["feedback_count"] or 0)
            total_feedback += count
            if draft.category_primary:
                categories[draft.category_primary] = categories.get(draft.category_primary, 0) + count
            for tag in draft.tags:
                tags[tag] = tags.get(tag, 0) + count
            if draft.source_name:
                sources[draft.source_name] = sources.get(draft.source_name, 0) + count
            examples.append(
                {
                    "title": draft.title_edited or draft.title_original,
                    "category": draft.category_primary,
                    "tags": draft.tags,
                    "source": draft.source_name,
                    "interested_count": count,
                }
            )

        def top_values(values: dict[str, int]) -> dict[str, int]:
            return dict(sorted(values.items(), key=lambda item: (-item[1], item[0].casefold()))[:10])

        return {
            "total_feedback": total_feedback,
            "categories": top_values(categories),
            "tags": top_values(tags),
            "sources": top_values(sources),
            "examples": examples[:10],
        }

    def list_drafts_by_status(
        self,
        status: str,
        *,
        topic: str | None = None,
        cadence: str | None = None,
        limit: int | None = None,
    ) -> list[ArticleDraft]:
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if cadence:
            clauses.append("cadence = ?")
            params.append(cadence)
        sql = f"""
            SELECT * FROM article_drafts
            WHERE {' AND '.join(clauses)}
            ORDER BY importance_score DESC, created_at ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_recent_cancelled_weekly(self, limit: int | None = None) -> list[ArticleDraft]:
        sql = """
            SELECT DISTINCT article_drafts.*
            FROM article_drafts
            JOIN review_actions ON review_actions.article_id = article_drafts.id
            WHERE review_actions.action_type = 'cancel_weekly'
            ORDER BY review_actions.created_at DESC
        """
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def table_counts(self) -> dict[str, int]:
        tables = [
            "article_drafts",
            "review_actions",
            "feedback_events",
            "delivery_events",
            "notification_runs",
            "notification_messages",
        ]
        with self.connect() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
                for table in tables
            }

    def list_approved_weekly(self, topic: str, cadence: str, limit: int | None = None) -> list[ArticleDraft]:
        sql = """
            SELECT * FROM article_drafts
            WHERE topic = ? AND cadence = ? AND status = 'approved_weekly'
            ORDER BY importance_score DESC, created_at ASC
        """
        params: list[Any] = [topic, cadence]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_ready_weekly(self, topic: str, cadence: str, limit: int | None = None) -> list[ArticleDraft]:
        sql = """
            SELECT * FROM article_drafts
            WHERE topic = ? AND cadence = ? AND status = 'ready_weekly'
            ORDER BY importance_score DESC, created_at ASC
        """
        params: list[Any] = [topic, cadence]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_ready_weekly_batch(self, topic: str, cadence: str, scheduled_at: str, limit: int | None = None) -> list[ArticleDraft]:
        sql = """
            SELECT * FROM article_drafts
            WHERE topic = ? AND cadence = ? AND status = 'ready_weekly'
              AND COALESCE(scheduled_at, '') = ?
            ORDER BY importance_score DESC, created_at ASC
        """
        params: list[Any] = [topic, cadence, scheduled_at or ""]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_final_ready_weekly(self, topic: str, cadence: str, limit: int | None = None) -> list[ArticleDraft]:
        sql = """
            SELECT * FROM article_drafts
            WHERE topic = ? AND cadence = ? AND status = 'final_ready'
            ORDER BY importance_score DESC, created_at ASC
        """
        params: list[Any] = [topic, cadence]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def list_final_ready_weekly_batch(
        self,
        topic: str,
        cadence: str,
        scheduled_at: str,
        limit: int | None = None,
    ) -> list[ArticleDraft]:
        sql = """
            SELECT * FROM article_drafts
            WHERE topic = ? AND cadence = ? AND status = 'final_ready'
              AND COALESCE(scheduled_at, '') = ?
            ORDER BY importance_score DESC, created_at ASC
        """
        params: list[Any] = [topic, cadence, scheduled_at or ""]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def record_delivery_event(
        self,
        *,
        article_id: str,
        topic: str,
        cadence: str,
        publish_type: str,
        destination: str,
        status: str,
        message_text: str,
        discord_channel_id: str | None = None,
        discord_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events(
                    id, article_id, topic, cadence, publish_type, destination,
                    status, discord_channel_id, discord_message_id, message_text,
                    error_message, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    article_id,
                    topic,
                    cadence,
                    publish_type,
                    destination,
                    status,
                    discord_channel_id,
                    discord_message_id,
                    message_text,
                    error_message,
                    utc_now(),
                ),
            )

    def start_run(
        self,
        *,
        topic: str,
        cadence: str,
        destination: str,
        input_hash: str,
        item_count: int,
        dry_run: bool,
    ) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_runs(
                    id, topic, cadence, destination, status, input_hash, item_count, dry_run, started_at
                )
                VALUES(?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (run_id, topic, cadence, destination, input_hash, item_count, int(dry_run), utc_now()),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        sent_count: int,
        skipped_count: int,
        error_message: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE notification_runs
                SET status = ?, sent_count = ?, skipped_count = ?, error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, sent_count, skipped_count, error_message, utc_now(), run_id),
            )

    def find_duplicate_reason(self, topic: str, item: NewsItem, similarity_threshold: float = 0.8) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT title FROM notified_items
                WHERE topic = ? AND (canonical_url = ? OR event_hash = ?)
                LIMIT 1
                """,
                (topic, item.canonical_url, item.event_hash),
            ).fetchone()
            if row:
                return "same_url_or_event"

            rows = conn.execute(
                """
                SELECT title FROM notified_items
                WHERE topic = ?
                ORDER BY last_notified_at DESC
                LIMIT 250
                """,
                (topic,),
            ).fetchall()
        for row in rows:
            if title_similarity(item.title, row["title"]) >= similarity_threshold:
                return "near_duplicate_headline"
        return None

    def record_item(self, run_id: str, topic: str, item: NewsItem) -> str:
        item_id = str(uuid.uuid4())
        now = utc_now()
        payload_json = stable_json(item.raw or {})
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notified_items(
                    id, topic, canonical_url, event_hash, title, source,
                    first_seen_at, last_notified_at, last_run_id, payload_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    topic,
                    item.canonical_url,
                    item.event_hash,
                    item.title,
                    item.source,
                    now,
                    now,
                    run_id,
                    payload_json,
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM notified_items
                WHERE topic = ? AND (canonical_url = ? OR event_hash = ?)
                LIMIT 1
                """,
                (topic, item.canonical_url, item.event_hash),
            ).fetchone()
            return str(row["id"])

    def record_message(
        self,
        *,
        run_id: str,
        item_id: str | None,
        topic: str,
        destination: str,
        status: str,
        message_text: str,
        response_code: int | None = None,
        response_body: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_messages(
                    id, run_id, item_id, topic, destination, status,
                    response_code, response_body, message_text, error_message, sent_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    run_id,
                    item_id,
                    topic,
                    destination,
                    status,
                    response_code,
                    response_body,
                    message_text,
                    error_message,
                    utc_now(),
                ),
            )
