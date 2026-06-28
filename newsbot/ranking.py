from __future__ import annotations

from typing import Any

from .models import NewsItem


REVIEW_MAX_ITEMS = 30

PRIORITY_WEIGHTS = {
    "urgent": 0.25,
    "breaking": 0.25,
    "high": 0.15,
    "normal": 0.0,
    "medium": 0.0,
    "low": -0.1,
}


def _count_map(profile: dict[str, Any] | None, key: str) -> dict[str, int]:
    values = (profile or {}).get(key) or {}
    if isinstance(values, dict):
        return {str(name).casefold(): int(count) for name, count in values.items() if name}
    return {}


def _normalized_score(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def score_news_item(item: NewsItem, preference_profile: dict[str, Any] | None = None) -> float:
    category_counts = _count_map(preference_profile, "categories")
    tag_counts = _count_map(preference_profile, "tags")
    source_counts = _count_map(preference_profile, "sources")

    score = _normalized_score(item.importance_score)
    score += PRIORITY_WEIGHTS.get((item.priority or "normal").casefold(), 0.0)

    category = item.category_primary.casefold()
    if category:
        score += min(category_counts.get(category, 0) * 0.03, 0.18)

    tag_boost = sum(tag_counts.get(tag.casefold(), 0) for tag in item.tags)
    score += min(tag_boost * 0.015, 0.24)

    source = item.source.casefold()
    if source:
        score += min(source_counts.get(source, 0) * 0.02, 0.12)

    return score


def rank_news_items(
    items: list[NewsItem],
    *,
    preference_profile: dict[str, Any] | None = None,
    limit: int = REVIEW_MAX_ITEMS,
) -> list[NewsItem]:
    ranked = sorted(
        items,
        key=lambda item: (
            score_news_item(item, preference_profile),
            item.importance_score,
            item.title.casefold(),
        ),
        reverse=True,
    )
    return ranked[:limit]
