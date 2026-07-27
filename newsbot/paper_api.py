from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError


DEFAULT_PAPER_QUERIES = [
    "self-driving laboratory",
    "self-driving lab",
    "autonomous experimentation",
    "closed-loop experimentation",
    "laboratory automation",
    "robot scientist",
    "liquid handler",
    "robotic experimentation",
    "automated synthesis",
    "high-throughput experimentation",
]

USER_AGENT = "newsbot-automation/0.2 (+https://github.com/noguhiro2002/newsbot-automation)"
UrlOpen = Callable[..., Any]
SENSITIVE_QUERY_PARAMETERS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "email",
        "key",
        "mailto",
        "secret",
        "token",
    }
)
RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_API_CACHE_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class PaperCandidate:
    title: str
    abstract: str
    authors: list[str]
    source: str
    url: str
    doi: str
    published_date: str
    source_type: str
    api_source: str
    matched_query: str
    raw_categories: list[str]


class RequestRateLimiter:
    def __init__(
        self,
        requests_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.minimum_interval = 1.0 / max(0.1, requests_per_second)
        self.clock = clock
        self.sleep = sleep
        self._last_request_at: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            remaining = self.minimum_interval - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last_request_at = now


@dataclass
class ApiRequestContext:
    cache_dir: Path | None
    cache_ttl_seconds: int
    max_retries: int
    ncbi_limiter: RequestRateLimiter
    crossref_limiter: RequestRateLimiter

    def limiter_for(self, url: str) -> RequestRateLimiter | None:
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        if hostname == "eutils.ncbi.nlm.nih.gov":
            return self.ncbi_limiter
        if hostname == "api.crossref.org":
            return self.crossref_limiter
        return None


def parse_period_dates(period: str) -> tuple[date, date]:
    matches = re.findall(r"\d{4}-\d{2}-\d{2}", period)
    if len(matches) < 2:
        raise ValueError(f"Period must include start and end dates: {period}")
    start = date.fromisoformat(matches[0])
    end = date.fromisoformat(matches[1])
    if end < start:
        raise ValueError(f"Period end date is before start date: {period}")
    return start, end


def collect_paper_api_candidates(
    *,
    period: str,
    retmax: int = 50,
    ncbi_api_key: str = "",
    ncbi_tool: str = "",
    ncbi_email: str = "",
    crossref_mailto: str = "",
    timeout: int = 20,
    opener: UrlOpen | None = None,
    cache_dir: str | Path | None = None,
    cache_ttl_seconds: int = DEFAULT_API_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    start_date, end_date = parse_period_dates(period)
    limit = max(1, retmax)
    fetcher = opener or urllib.request.urlopen
    production_requests = opener is None
    request_context = ApiRequestContext(
        cache_dir=Path(cache_dir) if cache_dir else None,
        cache_ttl_seconds=max(0, cache_ttl_seconds),
        max_retries=3 if production_requests else 0,
        ncbi_limiter=RequestRateLimiter(10 if ncbi_api_key else 3),
        crossref_limiter=RequestRateLimiter(10 if crossref_mailto else 5),
    )
    coverage: dict[str, Any] = {
        "period": period,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "retmax_per_api": limit,
        "api_queries_run": [],
        "api_failures": [],
        "limitations": "",
    }

    candidates: list[PaperCandidate] = []
    collectors = [
        ("arxiv", lambda: fetch_arxiv_candidates(start_date, end_date, limit, timeout, fetcher, coverage, request_context)),
        (
            "pubmed",
            lambda: fetch_pubmed_candidates(
                start_date,
                end_date,
                limit,
                timeout,
                fetcher,
                coverage,
                ncbi_api_key,
                ncbi_tool,
                ncbi_email,
                request_context,
            ),
        ),
        (
            "biorxiv",
            lambda: fetch_biorxiv_family_candidates(
                "biorxiv", start_date, end_date, limit, timeout, fetcher, coverage, request_context
            ),
        ),
        (
            "medrxiv",
            lambda: fetch_biorxiv_family_candidates(
                "medrxiv", start_date, end_date, limit, timeout, fetcher, coverage, request_context
            ),
        ),
        (
            "crossref",
            lambda: fetch_crossref_candidates(
                start_date, end_date, limit, timeout, fetcher, coverage, crossref_mailto, request_context
            ),
        ),
    ]
    for api_name, collect in collectors:
        try:
            candidates.extend(collect())
        except Exception as exc:  # noqa: BLE001 - API failures should not stop Phase 4.
            coverage["api_failures"].append(
                {"api_source": api_name, "error": redact_sensitive_text(str(exc))}
            )

    deduped = dedupe_candidates(filter_candidates_by_period(candidates, start_date, end_date))
    coverage["candidate_count_before_dedupe"] = len(candidates)
    coverage["candidate_count_after_dedupe"] = len(deduped)
    if coverage["api_failures"]:
        failures = ", ".join(f"{item['api_source']}: {item['error']}" for item in coverage["api_failures"])
        coverage["limitations"] = f"Some paper APIs failed and were skipped: {failures}"
    return {
        "period": period,
        "queries": DEFAULT_PAPER_QUERIES,
        "candidates": [asdict(candidate) for candidate in deduped],
        "coverage": coverage,
    }


def fetch_arxiv_candidates(
    start_date: date,
    end_date: date,
    retmax: int,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    request_context: ApiRequestContext,
) -> list[PaperCandidate]:
    candidates: list[PaperCandidate] = []
    per_query_limit = max(1, min(10, retmax))
    for query in DEFAULT_PAPER_QUERIES:
        if len(candidates) >= retmax:
            break
        search_query = f'all:"{query}"'
        params = {
            "search_query": search_query,
            "start": "0",
            "max_results": str(per_query_limit),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        record_api_query(coverage, api_source="arxiv", query=query, url=url)
        body = fetch_url(url, timeout, opener, context=request_context)
        candidates.extend(parse_arxiv_atom(body, query))
    return candidates[:retmax]


def parse_arxiv_atom(body: bytes | str, matched_query: str) -> list[PaperCandidate]:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    root = ET.fromstring(text)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    candidates: list[PaperCandidate] = []
    for entry in root.findall("atom:entry", ns):
        title = clean_text(find_text(entry, "atom:title", ns))
        abstract = clean_text(find_text(entry, "atom:summary", ns))
        url = clean_text(find_text(entry, "atom:id", ns))
        published_date = parse_date_string(find_text(entry, "atom:published", ns))
        authors = [clean_text(author.findtext("atom:name", default="", namespaces=ns)) for author in entry.findall("atom:author", ns)]
        categories = [category.attrib.get("term", "") for category in entry.findall("atom:category", ns) if category.attrib.get("term")]
        doi = clean_text(find_text(entry, "arxiv:doi", ns))
        if title and url and published_date:
            candidates.append(
                PaperCandidate(
                    title=title,
                    abstract=abstract,
                    authors=[author for author in authors if author],
                    source="arXiv",
                    url=url,
                    doi=doi,
                    published_date=published_date,
                    source_type="preprint",
                    api_source="arxiv",
                    matched_query=matched_query,
                    raw_categories=categories,
                )
            )
    return candidates


def fetch_pubmed_candidates(
    start_date: date,
    end_date: date,
    retmax: int,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    ncbi_api_key: str,
    ncbi_tool: str,
    ncbi_email: str,
    request_context: ApiRequestContext,
) -> list[PaperCandidate]:
    if not ncbi_tool.strip() or not ncbi_email.strip():
        raise ValueError(
            "PubMed requires registered NEWSBOT_NCBI_TOOL and NEWSBOT_NCBI_EMAIL values"
        )
    ids: list[str] = []
    per_query_limit = max(1, min(10, retmax))
    for query in DEFAULT_PAPER_QUERIES:
        if len(ids) >= retmax:
            break
        term = f'"{query}" AND ("{start_date:%Y/%m/%d}"[Date - Publication] : "{end_date:%Y/%m/%d}"[Date - Publication])'
        params = {
            "db": "pubmed",
            "email": ncbi_email.strip(),
            "term": term,
            "tool": ncbi_tool.strip(),
            "retmode": "json",
            "retmax": str(per_query_limit),
        }
        if ncbi_api_key:
            params["api_key"] = ncbi_api_key
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
        record_api_query(coverage, api_source="pubmed", query=query, url=url)
        data = json.loads(
            fetch_url(url, timeout, opener, context=request_context).decode(
                "utf-8", errors="replace"
            )
        )
        ids.extend(data.get("esearchresult", {}).get("idlist", []))
    ids = list(dict.fromkeys(ids))[:retmax]
    if not ids:
        return []
    params = {
        "db": "pubmed",
        "email": ncbi_email.strip(),
        "id": ",".join(ids),
        "retmode": "xml",
        "tool": ncbi_tool.strip(),
    }
    if ncbi_api_key:
        params["api_key"] = ncbi_api_key
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(params)
    record_api_query(coverage, api_source="pubmed", query="efetch", url=url)
    return parse_pubmed_xml(fetch_url(url, timeout, opener, context=request_context))


def parse_pubmed_xml(body: bytes | str) -> list[PaperCandidate]:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    root = ET.fromstring(text)
    candidates: list[PaperCandidate] = []
    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        pubmed = article.find("PubmedData")
        if medline is None:
            continue
        pmid = clean_text(medline.findtext("PMID", default=""))
        article_node = medline.find("Article")
        if article_node is None:
            continue
        title = clean_text("".join(article_node.findtext("ArticleTitle", default="")))
        abstract = clean_text(" ".join(node_text(node) for node in article_node.findall(".//AbstractText")))
        journal = clean_text(article_node.findtext("Journal/Title", default="PubMed"))
        published_date = pubmed_article_date(article_node)
        authors = pubmed_authors(article_node)
        doi = ""
        if pubmed is not None:
            for article_id in pubmed.findall(".//ArticleId"):
                if article_id.attrib.get("IdType") == "doi":
                    doi = clean_text(article_id.text or "")
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://doi.org/{doi}" if doi else "")
        if title and url and published_date:
            candidates.append(
                PaperCandidate(
                    title=title,
                    abstract=abstract,
                    authors=authors,
                    source=journal or "PubMed",
                    url=url,
                    doi=doi,
                    published_date=published_date,
                    source_type="paper",
                    api_source="pubmed",
                    matched_query="pubmed-esearch",
                    raw_categories=[],
                )
            )
    return candidates


def fetch_biorxiv_family_candidates(
    server: str,
    start_date: date,
    end_date: date,
    retmax: int,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    request_context: ApiRequestContext,
) -> list[PaperCandidate]:
    cursor = 0
    candidates: list[PaperCandidate] = []
    while len(candidates) < retmax:
        url = f"https://api.biorxiv.org/details/{server}/{start_date.isoformat()}/{end_date.isoformat()}/{cursor}"
        record_api_query(
            coverage, api_source=server, query=f"{server} details", url=url
        )
        data = json.loads(
            fetch_url(url, timeout, opener, context=request_context).decode(
                "utf-8", errors="replace"
            )
        )
        collection = data.get("collection") or []
        if not collection:
            break
        for item in collection:
            candidate = biorxiv_item_to_candidate(item, server)
            if candidate and candidate_matches_queries(candidate):
                candidates.append(candidate)
                if len(candidates) >= retmax:
                    break
        if len(collection) < 100:
            break
        cursor += len(collection)
    return candidates[:retmax]


def biorxiv_item_to_candidate(item: dict[str, Any], server: str) -> PaperCandidate | None:
    title = clean_text(str(item.get("title") or ""))
    doi = clean_text(str(item.get("doi") or ""))
    published_date = parse_date_string(str(item.get("date") or ""))
    if not title or not doi or not published_date:
        return None
    return PaperCandidate(
        title=title,
        abstract=clean_text(str(item.get("abstract") or "")),
        authors=split_authors(str(item.get("authors") or "")),
        source=server,
        url=f"https://doi.org/{doi}",
        doi=doi,
        published_date=published_date,
        source_type="preprint",
        api_source=server,
        matched_query="keyword-filtered details",
        raw_categories=[clean_text(str(item.get("category") or ""))] if item.get("category") else [],
    )


def fetch_crossref_candidates(
    start_date: date,
    end_date: date,
    retmax: int,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    crossref_mailto: str,
    request_context: ApiRequestContext,
) -> list[PaperCandidate]:
    candidates: list[PaperCandidate] = []
    per_query_limit = max(1, min(10, retmax))
    for query in DEFAULT_PAPER_QUERIES:
        if len(candidates) >= retmax:
            break
        params = {
            "query.bibliographic": query,
            "filter": f"from-pub-date:{start_date.isoformat()},until-pub-date:{end_date.isoformat()},type:journal-article",
            "rows": str(per_query_limit),
            "sort": "published",
            "order": "desc",
        }
        if crossref_mailto.strip():
            params["mailto"] = crossref_mailto.strip()
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        record_api_query(coverage, api_source="crossref", query=query, url=url)
        data = json.loads(
            fetch_url(url, timeout, opener, context=request_context).decode(
                "utf-8", errors="replace"
            )
        )
        for item in data.get("message", {}).get("items", []):
            candidate = crossref_item_to_candidate(item, query)
            if candidate:
                candidates.append(candidate)
    return candidates[:retmax]


def crossref_item_to_candidate(item: dict[str, Any], query: str) -> PaperCandidate | None:
    title = clean_text(" ".join(item.get("title") or []))
    published_date = crossref_date(item)
    doi = clean_text(str(item.get("DOI") or ""))
    url = clean_text(str(item.get("URL") or "")) or (f"https://doi.org/{doi}" if doi else "")
    if not title or not published_date or not url:
        return None
    return PaperCandidate(
        title=title,
        abstract=clean_text(str(item.get("abstract") or "")),
        authors=crossref_authors(item),
        source=clean_text(" ".join(item.get("container-title") or [])) or "Crossref",
        url=url,
        doi=doi,
        published_date=published_date,
        source_type="paper",
        api_source="crossref",
        matched_query=query,
        raw_categories=[clean_text(str(subject)) for subject in item.get("subject", []) if subject],
    )


def fetch_url(
    url: str,
    timeout: int,
    opener: UrlOpen,
    *,
    context: ApiRequestContext | None = None,
) -> bytes:
    cache_path = api_cache_path(context.cache_dir, url) if context and context.cache_dir else None
    if cache_path and context:
        cached = read_api_cache(cache_path, context.cache_ttl_seconds)
        if cached is not None:
            return cached

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    max_retries = context.max_retries if context else 0
    limiter = context.limiter_for(url) if context else None
    for attempt in range(max_retries + 1):
        if limiter:
            limiter.wait()
        try:
            with opener(request, timeout=timeout) as response:
                body = response.read()
            if cache_path:
                write_api_cache(cache_path, body)
            return body
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUS_CODES or attempt >= max_retries:
                raise
            retry_after = parse_retry_after(exc.headers.get("Retry-After"))
        except (TimeoutError, URLError):
            if attempt >= max_retries:
                raise
            retry_after = 0.0
        time.sleep(max(retry_after, min(30.0, float(2**attempt))))
    raise RuntimeError("API request retry loop exited unexpectedly")


def record_api_query(
    coverage: dict[str, Any], *, api_source: str, query: str, url: str
) -> None:
    coverage["api_queries_run"].append(
        {
            "api_source": api_source,
            "query": query,
            "url": redact_sensitive_url(url),
        }
    )


def redact_sensitive_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    redacted_query = urllib.parse.urlencode(
        [
            (
                key,
                "<redacted>" if key.lower() in SENSITIVE_QUERY_PARAMETERS else value,
            )
            for key, value in urllib.parse.parse_qsl(
                parts.query, keep_blank_values=True
            )
        ]
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, redacted_query, parts.fragment)
    )


def redact_sensitive_text(value: str) -> str:
    return re.sub(
        r"(?i)([?&](?:access_token|api_?key|apikey|email|key|mailto|secret|token)=)"
        r"[^&\s]+",
        r"\1<redacted>",
        value,
    )


def redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in SENSITIVE_QUERY_PARAMETERS
                else redact_sensitive_values(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_values(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def api_cache_path(cache_dir: Path, url: str) -> Path:
    cache_key = hashlib.sha256(
        redact_sensitive_url(url).encode("utf-8")
    ).hexdigest()
    return cache_dir / f"{cache_key}.response"


def read_api_cache(path: Path, ttl_seconds: int) -> bytes | None:
    if ttl_seconds <= 0 or not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > ttl_seconds:
        return None
    return path.read_bytes()


def write_api_cache(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_bytes(body)
    os.chmod(path, 0o600)


def parse_retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 0.0


def filter_candidates_by_period(candidates: list[PaperCandidate], start_date: date, end_date: date) -> list[PaperCandidate]:
    filtered = []
    for candidate in candidates:
        try:
            published = date.fromisoformat(candidate.published_date)
        except ValueError:
            continue
        if start_date <= published <= end_date:
            filtered.append(candidate)
    return filtered


def dedupe_candidates(candidates: list[PaperCandidate]) -> list[PaperCandidate]:
    seen: set[str] = set()
    deduped: list[PaperCandidate] = []
    for candidate in candidates:
        key = candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def candidate_key(candidate: PaperCandidate) -> str:
    if candidate.doi:
        return "doi:" + candidate.doi.lower()
    if candidate.url:
        return "url:" + candidate.url.rstrip("/").lower()
    return "title:" + normalize_title(candidate.title)


def candidate_matches_queries(candidate: PaperCandidate) -> bool:
    haystack = f"{candidate.title} {candidate.abstract}".lower()
    return any(query.lower() in haystack for query in DEFAULT_PAPER_QUERIES)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def parse_date_string(value: str) -> str:
    if not value:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        return match.group(0)
    match = re.search(r"\d{4}/\d{2}/\d{2}", value)
    if match:
        return match.group(0).replace("/", "-")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def find_text(node: ET.Element, path: str, ns: dict[str, str]) -> str:
    return node.findtext(path, default="", namespaces=ns)


def node_text(node: ET.Element) -> str:
    return "".join(node.itertext())


def split_authors(value: str) -> list[str]:
    return [clean_text(part) for part in re.split(r";|,", value) if clean_text(part)]


def pubmed_authors(article_node: ET.Element) -> list[str]:
    authors = []
    for author in article_node.findall(".//Author"):
        collective = clean_text(author.findtext("CollectiveName", default=""))
        if collective:
            authors.append(collective)
            continue
        name = clean_text(" ".join([author.findtext("ForeName", default=""), author.findtext("LastName", default="")]))
        if name:
            authors.append(name)
    return authors


def pubmed_article_date(article_node: ET.Element) -> str:
    article_date = article_node.find("ArticleDate")
    if article_date is not None:
        parsed = date_from_parts(
            article_date.findtext("Year", default=""),
            article_date.findtext("Month", default=""),
            article_date.findtext("Day", default=""),
        )
        if parsed:
            return parsed
    pub_date = article_node.find("Journal/JournalIssue/PubDate")
    if pub_date is not None:
        return date_from_parts(
            pub_date.findtext("Year", default=""),
            pub_date.findtext("Month", default="1"),
            pub_date.findtext("Day", default="1"),
        )
    return ""


def date_from_parts(year: str, month: str, day: str) -> str:
    if not year:
        return ""
    try:
        month_number = int(month) if month.isdigit() else month_name_to_number(month)
        day_number = int(day) if day.isdigit() else 1
        return date(int(year), month_number, day_number).isoformat()
    except ValueError:
        return ""


def month_name_to_number(value: str) -> int:
    names = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return names.get(value[:3].lower(), 1)


def crossref_date(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "created"):
        parts = item.get(key, {}).get("date-parts") or []
        if parts and parts[0]:
            values = parts[0]
            try:
                return date(int(values[0]), int(values[1]) if len(values) > 1 else 1, int(values[2]) if len(values) > 2 else 1).isoformat()
            except (TypeError, ValueError):
                continue
    return ""


def crossref_authors(item: dict[str, Any]) -> list[str]:
    authors = []
    for author in item.get("author", []) or []:
        name = clean_text(" ".join([str(author.get("given") or ""), str(author.get("family") or "")]))
        if name:
            authors.append(name)
    return authors
