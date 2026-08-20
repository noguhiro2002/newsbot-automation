from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_NAMES = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_QUERY_NAMES:
            continue
        if any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key, value))
    netloc = parts.netloc.lower()
    path = re.sub(r"/+$", "", parts.path) or parts.path
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query_pairs), ""))


def normalize_title(title: str) -> str:
    value = (title or "").casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def title_ngrams(title: str, n: int = 3) -> set[str]:
    normalized = normalize_title(title).replace(" ", "")
    if not normalized:
        return set()
    if len(normalized) <= n:
        return {normalized}
    return {normalized[index : index + n] for index in range(len(normalized) - n + 1)}


def title_similarity(left: str, right: str) -> float:
    left_grams = title_ngrams(left)
    right_grams = title_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def coerce_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        tags = [part.strip() for part in value.split(",")]
        return tuple(tag for tag in tags if tag)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return ()


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    summary: str = ""
    source: str = ""
    why_it_matters: str = ""
    x_text: str = ""
    company: str = ""
    ticker: str = ""
    material: str = ""
    impact_direction: str = ""
    confidence: str = ""
    category_primary: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    importance_score: float = 0.0
    priority: str = "normal"
    content_kind: str = "news"
    published_date: str = ""
    event_date_start: str = ""
    event_date_end: str = ""
    date_basis: str = "publication"
    original_publication_date: str = ""
    raw: dict[str, Any] | None = None

    @property
    def canonical_url(self) -> str:
        return canonicalize_url(self.url)

    @property
    def event_hash(self) -> str:
        seed = self.canonical_url or normalize_title(self.title)
        return sha256_text(seed)

    @property
    def draft_body(self) -> str:
        return self.material or self.summary or self.why_it_matters or self.x_text or self.title

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NewsItem":
        title = str(value.get("title") or value.get("headline") or "").strip()
        url = str(value.get("url") or value.get("source_url") or "").strip()
        if not title:
            raise ValueError("Each item must include a title")
        if not url:
            raise ValueError(f"Item must include a url: {title}")

        importance_value = value.get("importance_score", 0) or 0
        try:
            importance_score = float(importance_value)
        except (TypeError, ValueError):
            importance_score = 0.0

        return cls(
            title=title,
            url=url,
            summary=str(value.get("summary") or value.get("one_sentence_summary") or "").strip(),
            source=str(value.get("source") or value.get("source_name") or "").strip(),
            why_it_matters=str(value.get("why_it_matters") or value.get("important_reason") or "").strip(),
            x_text=str(value.get("x_text") or value.get("x_post") or "").strip(),
            company=str(value.get("company") or value.get("company_name") or "").strip(),
            ticker=str(value.get("ticker") or "").strip(),
            material=str(value.get("material") or "").strip(),
            impact_direction=str(value.get("impact_direction") or "").strip(),
            confidence=str(value.get("confidence") or "").strip(),
            category_primary=str(value.get("category_primary") or value.get("category") or "").strip(),
            tags=coerce_tags(value.get("tags")),
            importance_score=importance_score,
            priority=str(value.get("priority") or "normal").strip() or "normal",
            content_kind=str(value.get("content_kind") or "news").strip() or "news",
            published_date=str(value.get("published_date") or "").strip(),
            event_date_start=str(value.get("event_date_start") or "").strip(),
            event_date_end=str(value.get("event_date_end") or "").strip(),
            date_basis=str(value.get("date_basis") or "publication").strip() or "publication",
            original_publication_date=str(value.get("original_publication_date") or "").strip(),
            raw=value,
        )


@dataclass(frozen=True)
class NewsPayload:
    topic: str
    cadence: str
    period: str
    items: list[NewsItem]

    @property
    def input_hash(self) -> str:
        return sha256_text(
            stable_json(
                {
                    "topic": self.topic,
                    "cadence": self.cadence,
                    "period": self.period,
                    "items": [item.raw or {} for item in self.items],
                }
            )
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NewsPayload":
        items = [NewsItem.from_dict(item) for item in value.get("items", [])]
        if not items:
            raise ValueError("Payload must include at least one item")
        return cls(
            topic=str(value.get("topic") or "").strip(),
            cadence=str(value.get("cadence") or "").strip(),
            period=str(value.get("period") or "").strip(),
            items=items,
        )


@dataclass(frozen=True)
class ArticleDraft:
    id: str
    topic: str
    cadence: str
    source_name: str
    source_url: str
    canonical_url: str
    event_hash: str
    title_original: str
    body_original: str
    title_edited: str
    body_edited: str
    category_primary: str
    tags_json: str
    importance_score: float
    status: str
    publish_type: str
    priority: str
    review_channel_id: str
    review_message_id: str
    published_channel_id: str
    published_message_id: str
    source_payload_json: str
    created_at: str
    updated_at: str
    scheduled_at: str
    published_at: str

    @property
    def short_id(self) -> str:
        return self.id.split("-", 1)[0]

    @property
    def tags(self) -> list[str]:
        try:
            value = json.loads(self.tags_json or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @property
    def source_payload(self) -> dict[str, Any]:
        try:
            value = json.loads(self.source_payload_json or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def content_kind(self) -> str:
        return str(self.source_payload.get("content_kind") or "news")

    @property
    def event_date_start(self) -> str:
        return str(self.source_payload.get("event_date_start") or "")

    @property
    def event_date_end(self) -> str:
        return str(self.source_payload.get("event_date_end") or "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
