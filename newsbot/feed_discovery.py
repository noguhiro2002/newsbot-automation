from __future__ import annotations

import email.utils
import concurrent.futures
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .models import canonicalize_url, normalize_title, sha256_text


USER_AGENT = "newsbot-automation/1.0 (feed discovery; respectful conditional fetching)"
MAX_FEED_CANDIDATES_PER_SOURCE = 50


@dataclass(frozen=True)
class FeedSource:
    source_id: str
    name: str
    url: str
    phase: str
    kind: str = "feed"
    enabled: bool = True


def load_feed_sources(path: str | Path) -> list[FeedSource]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    sources: list[FeedSource] = []
    for item in raw.get("sources", []):
        urls: list[tuple[str, str]] = []
        if item.get("url"):
            urls.append((str(item["id"]), str(item["url"])))
        elif item.get("url_template") and item.get("ids_env"):
            ids = [value.strip() for value in os.getenv(str(item["ids_env"]), "").split(",") if value.strip()]
            urls.extend((f"{item['id']}-{value}", str(item["url_template"]).replace("{id}", value)) for value in ids)
        for source_id, url in urls:
            sources.append(FeedSource(source_id=source_id, name=str(item["name"]), url=url, phase=str(item["phase"]), kind=str(item.get("kind") or "feed"), enabled=bool(item.get("enabled", True))))
    return sources


def _parse_date(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def period_bounds(period: str) -> tuple[datetime | None, datetime | None]:
    values = re.findall(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", period)
    if len(values) < 2:
        return None, None
    start = _parse_date(values[0])
    end = _parse_date(values[1])
    if end and len(values[1]) == 10:
        end = end.replace(hour=23, minute=59, second=59)
    return start, end


def _text(element: ET.Element | None, names: tuple[str, ...]) -> str:
    if element is None:
        return ""
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1].lower() in names and child.text:
            return child.text.strip()
    return ""


def parse_feed_document(data: bytes, base_url: str, kind: str = "feed") -> list[dict[str, str]]:
    root = ET.fromstring(data)
    local_root = root.tag.rsplit("}", 1)[-1].lower()
    entries: list[dict[str, str]] = []
    if kind == "sitemap" or local_root in {"urlset", "sitemapindex"}:
        for node in root:
            if node.tag.rsplit("}", 1)[-1].lower() not in {"url", "sitemap"}:
                continue
            url = _text(node, ("loc",))
            if url:
                entries.append({"title": url.rsplit("/", 1)[-1] or url, "url": url, "published_date": _text(node, ("lastmod",))})
        return entries

    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
            continue
        link = _text(node, ("link",))
        if not link:
            for child in node:
                if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        entries.append(
            {
                "title": _text(node, ("title",)),
                "url": urljoin(base_url, link),
                "published_date": _text(node, ("published", "updated", "pubdate", "date")),
                "summary": _text(node, ("summary", "description", "content")),
            }
        )
    return entries


def _cache_path(cache_dir: Path, source: FeedSource) -> Path:
    return cache_dir / f"feed-{sha256_text(source.source_id)[:16]}.json"


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_source(source: FeedSource, cache_dir: Path, timeout: int = 30) -> dict[str, Any]:
    cache_path = _cache_path(cache_dir, source)
    cached = _read_cache(cache_path)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5"}
    if cached.get("etag"):
        headers["If-None-Match"] = str(cached["etag"])
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = str(cached["last_modified"])
    request = urllib.request.Request(source.url, headers=headers)
    status = 200
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            entries = parse_feed_document(body, source.url, source.kind)
            try:
                root = ET.fromstring(body)
                is_sitemap_index = root.tag.rsplit("}", 1)[-1].lower() == "sitemapindex"
            except ET.ParseError:
                is_sitemap_index = False
            if source.kind == "sitemap" and is_sitemap_index:
                child_urls = [entry["url"] for entry in entries[:10]]

                def fetch_child(url: str) -> list[dict[str, str]]:
                    child_request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml"})
                    with urllib.request.urlopen(child_request, timeout=timeout) as child_response:
                        return parse_feed_document(child_response.read(), url, "sitemap")

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(child_urls))) as executor:
                    child_results = list(executor.map(fetch_child, child_urls)) if child_urls else []
                entries = [entry for child_entries in child_results for entry in child_entries]
            etag = response.headers.get("ETag", "")
            last_modified = response.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as exc:
        if exc.code != 304:
            raise
        status = 304
        entries = list(cached.get("entries") or [])
        etag = str(cached.get("etag") or "")
        last_modified = str(cached.get("last_modified") or "")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    value = {"etag": etag, "last_modified": last_modified, "fetched_at": now, "http_status": status, "entries": entries}
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def collect_feed_candidates(
    *, period: str, sources: list[FeedSource], cache_dir: str | Path, timeout: int = 30
) -> dict[str, Any]:
    start, end = period_bounds(period)
    candidates: list[dict[str, Any]] = []
    source_audit: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    active = [source for source in sources if source.enabled]

    def fetch_one(source: FeedSource) -> tuple[FeedSource, dict[str, Any] | Exception]:
        try:
            return source, fetch_source(source, Path(cache_dir), timeout)
        except Exception as exc:
            return source, exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(active)))) as executor:
        results = list(executor.map(fetch_one, active))

    for source, result in results:
        if isinstance(result, Exception):
            source_audit.append({"id": source.source_id, "url": source.url, "kind": source.kind, "status": "failed", "error": str(result), "fetched_count": 0, "primary_accepted_count": 0})
            continue
        try:
            fetched = result
            entries = list(fetched.get("entries") or [])
            accepted = 0
            sorted_entries = sorted(entries, key=lambda entry: _parse_date(str(entry.get("published_date") or "")) or datetime.min.replace(tzinfo=UTC), reverse=True)
            for entry in sorted_entries:
                if accepted >= MAX_FEED_CANDIDATES_PER_SOURCE:
                    break
                published = _parse_date(str(entry.get("published_date") or ""))
                if published and ((start and published < start) or (end and published > end)):
                    continue
                # Undated sitemap/list entries are retained for structured semantic verification.
                url = canonicalize_url(str(entry.get("url") or ""))
                title = str(entry.get("title") or "").strip()
                if not url or not title:
                    continue
                identity = (url, normalize_title(title))
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(
                    {
                        "feed_source_id": source.source_id,
                        "source": source.name,
                        "phase": source.phase,
                        "title": title,
                        "url": url,
                        "published_date": str(entry.get("published_date") or ""),
                        "summary": str(entry.get("summary") or ""),
                        "discovery_mode": "feed",
                        "discovery_modes": ["feed"],
                    }
                )
                accepted += 1
            source_audit.append(
                {"id": source.source_id, "url": source.url, "kind": source.kind, "status": "ok", "http_status": fetched.get("http_status"), "etag": fetched.get("etag"), "last_modified": fetched.get("last_modified"), "fetched_at": fetched.get("fetched_at"), "fetched_count": len(entries), "primary_accepted_count": accepted}
            )
        except Exception as exc:  # Parsing/audit failure must never stop the pipeline.
            source_audit.append({"id": source.source_id, "url": source.url, "kind": source.kind, "status": "failed", "error": str(exc), "fetched_count": 0, "primary_accepted_count": 0})
    return {"period": period, "candidates": candidates, "sources": source_audit, "candidate_count": len(candidates), "failure_count": sum(item["status"] == "failed" for item in source_audit)}
