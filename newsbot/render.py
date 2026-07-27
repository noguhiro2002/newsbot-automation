from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .models import ArticleDraft, NewsItem, NewsPayload


JST = ZoneInfo("Asia/Tokyo")


def truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."


def chunk_text(value: str, limit: int = 1900) -> list[str]:
    if len(value) <= limit:
        return [value]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in value.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current).strip())
            current = []
            current_len = 0
        if line_len > limit:
            chunks.append(truncate(line, limit))
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def item_body(item: NewsItem) -> str:
    return item.draft_body


def topic_label(topic: str) -> str:
    labels = {
        "lab_automation": "Lab Automation",
        "stock_news": "Stock News",
    }
    return labels.get(topic, topic.replace("_", " ").title())


def period_label(period: str) -> str:
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+to\s+(\d{4})-(\d{2})-(\d{2})", period)
    if not match:
        return period
    start = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    end = f"{match.group(4)}-{match.group(5)}-{match.group(6)}"
    return f"{start} to {end}"


def render_digest_header(payload: NewsPayload) -> str:
    return f"News Digest | {period_label(payload.period)} | {topic_label(payload.topic)}"


def render_news_item(item: NewsItem, index: int, limit: int | None = None) -> str:
    title_line = f"{index}. {item.title}"
    source_line = f"Source: {item.url}"
    body = item_body(item)

    if limit is None:
        return f"{title_line}\n\n{body}\n{source_line}".strip()

    full = f"{title_line}\n\n{body}\n{source_line}".strip()
    if len(full) <= limit:
        return full

    fixed_len = len(title_line) + len(source_line) + 3
    body_limit = max(0, limit - fixed_len)
    if body_limit > 0:
        return f"{title_line}\n\n{truncate(body, body_limit)}\n{source_line}".strip()

    title_limit = max(10, limit - len(source_line) - 1)
    return f"{truncate(title_line, title_limit)}\n{source_line}".strip()


def render_x_item(item: NewsItem, index: int = 1, limit: int = 280) -> str:
    return render_news_item(item, index, limit)


def render_discord_payload(payload: NewsPayload, items: list[NewsItem]) -> str:
    lines: list[str] = [render_digest_header(payload)]
    lines.extend(f"{index}. {item.title}" for index, item in enumerate(items, start=1))

    for index, item in enumerate(items, start=1):
        lines.append("")
        lines.append("==")
        lines.append("")
        lines.append(render_news_item(item, index))

    lines.append("")
    lines.append("==")
    return "\n".join(lines).strip()


def weekly_period_range(now: datetime | None = None) -> str:
    current = (now or datetime.now(JST)).astimezone(JST)
    start = current - timedelta(days=6)
    return f"{start:%Y/%m/%d} ~ {current:%Y/%m/%d}"


def render_weekly_digest_overview(drafts: list[ArticleDraft], now: datetime | None = None) -> str:
    topic = topic_label(drafts[0].topic) if drafts else "News"
    lines = [
        f"今週の {topic} の注目ニュース！",
        weekly_period_range(now),
        "",
    ]
    for index, draft in enumerate(drafts, start=1):
        lines.append(f"{index}. {draft.title_edited or draft.title_original}")
    lines.extend(
        [
            "",
            "※：気になった投稿にInterestedしてニュースを最適化しましょう！",
        ]
    )
    return "\n".join(lines).strip()


def render_weekly_detail_message(draft: ArticleDraft, index: int, *, leading_gap: bool = False) -> str:
    title = truncate(draft.title_edited or draft.title_original, 300)
    body = truncate(draft.body_edited or draft.body_original, 1400)
    message = "\n".join(
        [
            f"{index}. **{title}**",
            "",
            body,
            "",
            f"Source: {draft.source_url}",
        ]
    ).strip()
    if leading_gap:
        return "\u200b\n\n" + message
    return message


def render_status_line(draft: ArticleDraft) -> str:
    labels = {
        "pending": "Pending review",
        "approved_weekly": "Approved for weekly publish",
        "ready_weekly": "Ready for final weekly publish",
        "final_ready": "Selected for weekly digest",
        "held": "On hold",
        "rejected": "Rejected",
        "published": "Published",
        "failed": "Failed",
    }
    label = labels.get(draft.status, draft.status)
    if draft.publish_type:
        return f"{label} ({draft.publish_type})"
    return label


def render_review_message(draft: ArticleDraft) -> str:
    title = truncate(draft.title_edited or draft.title_original, 300)
    source = f"Source: {draft.source_url}"
    header_lines = [
        f"Review Draft #{draft.short_id}",
        "",
        f"Category: {draft.category_primary or 'unknown'}",
        f"Priority: {draft.priority or 'normal'}",
        f"Status: {render_status_line(draft)}",
    ]
    if draft.scheduled_at:
        header_lines.append(f"Scheduled: {draft.scheduled_at}")
    header_lines.extend(["", "Title:", title, "", "Body:"])
    fixed = "\n".join(header_lines + ["", source])
    body_limit = max(200, 1850 - len(fixed))
    body = truncate(draft.body_edited or draft.body_original, body_limit)
    return "\n".join(header_lines + [body, "", source]).strip()


def render_published_message(draft: ArticleDraft, publish_type: Literal["weekly", "breaking"]) -> str:
    if publish_type == "weekly":
        return render_weekly_detail_message(draft, 1)
    title = truncate(draft.title_edited or draft.title_original, 300)
    body = truncate(draft.body_edited or draft.body_original, 1200)
    heading = f"Breaking: {topic_label(draft.topic)} News"
    return "\n".join(
        [
            heading,
            "",
            f"**{title}**",
            "",
            body,
            "",
            f"Source: {draft.source_url}",
        ]
    ).strip()
