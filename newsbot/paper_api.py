from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError

import fcntl


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

ARXIV_DISCOVERY_CATEGORIES = (
    "cs.RO",
    "cs.AI",
    "cs.LG",
    "physics.chem-ph",
    "physics.ins-det",
    "cond-mat.mtrl-sci",
)
ARXIV_DISCOVERY_TERMS = (
    "chemistry",
    "chemical",
    "laboratory",
    "experiment",
    "experimental",
    "synthesis",
    "materials",
    "instrument",
    "assay",
)
ARXIV_AUTOMATION_TERMS = (
    "autonomous",
    "robotic",
    "automation",
    "robot",
    "agent",
    "closed-loop",
    "self-driving",
    "controller",
    "policy",
    "active learning",
    "high-throughput",
    "replay",
    "orchestration",
)
ARXIV_PAGE_SIZE = 100
ARXIV_MAX_SCAN_RESULTS_PER_DAY = 500
BIORXIV_PAGE_SIZE = 30
BIORXIV_MAX_SCAN_RESULTS = 5000
CHEMRXIV_MAX_SCAN_RESULTS = 2000
CHEMRXIV_DOI_PREFIX = "10.26434"
CHEMRXIV_DOI_PATTERN = re.compile(r"^10\.26434/chemrxiv[.-]", re.IGNORECASE)
PAPER_RELEVANCE_THRESHOLD = 6
PAPER_SPARSE_METADATA_RELEVANCE_THRESHOLD = 5
PAPER_SPARSE_ABSTRACT_MIN_CHARS = 120
POSITIVE_AUXILIARY_TERMS: ContextVar[tuple[str, ...]] = ContextVar("paper_positive_auxiliary_terms", default=())

DIRECT_RELEVANCE_PHRASES = {
    **{query: 10 for query in DEFAULT_PAPER_QUERIES},
    "autonomous laboratory": 10,
    "robotic chemistry": 10,
    "robotic chemist": 10,
    "autonomous chemistry": 10,
    "chemistry agent": 8,
    "scientific agent": 6,
    "agent experimentation": 8,
    "programmable chemical": 8,
    "chemical world": 7,
    "programmable scientific environment": 8,
    "experimental environment": 7,
    "scientific digital twin": 8,
    "laboratory digital twin": 8,
    "virtual laboratory": 7,
    "ai scientist": 7,
    "autonomous science agent": 8,
    "scientific agent benchmark": 6,
    "laboratory manipulation": 8,
    "lab manipulation": 8,
    "laboratory orchestration": 8,
    "experimental steering": 8,
    "adaptive experiment": 7,
}
DOMAIN_RELEVANCE_TERMS = (
    "chemistry",
    "chemical",
    "laboratory",
    " lab ",
    "synthesis",
    "materials",
    "biology",
    "biological",
    "biotechnology",
    "pharmaceutical",
    "scientific experiment",
    "assay",
    "instrument",
)
AUTOMATION_RELEVANCE_TERMS = (
    "autonomous",
    "robotic",
    "automation",
    " robot ",
    " agent",
    "closed-loop",
    "closed loop",
    "high-throughput",
    "high throughput",
    "active learning",
    "controller",
    "policy switching",
)
EXECUTION_RELEVANCE_TERMS = (
    "act and observe",
    "action",
    "execution",
    "manipulation",
    "workflow",
    "experiment planning",
    "decision",
    "feedback",
    "optimization",
    "replay",
    "audit",
    "uncertainty",
    "safety",
    "resource change",
    "state transition",
    "digital twin",
    "simulation environment",
    "experimental environment",
    "benchmark environment",
    "trajectory",
    "observation model",
)

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
NCBI_REQUESTS_PER_SECOND = 3
ARXIV_SECONDS_BETWEEN_REQUESTS = 3
BIORXIV_REQUESTS_PER_SECOND = 1
CROSSREF_PUBLIC_REQUESTS_PER_SECOND = 5
CROSSREF_POLITE_REQUESTS_PER_SECOND = 10


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
        shared_lock_path: Path | None = None,
    ):
        self.minimum_interval = 1.0 / max(0.1, requests_per_second)
        self.clock = clock
        self.sleep = sleep
        self.shared_lock_path = shared_lock_path
        self._last_request_at: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            remaining = self.minimum_interval - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last_request_at = now

    @contextmanager
    def request_slot(self) -> Iterator[None]:
        if self.shared_lock_path is None:
            self.wait()
            yield
            return

        self.shared_lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.shared_lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(self.shared_lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            raw_last_request_at = handle.read().strip()
            try:
                last_request_at = float(raw_last_request_at)
            except ValueError:
                last_request_at = 0.0
            now = time.time()
            remaining = self.minimum_interval - (now - last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now = time.time()
            handle.seek(0)
            handle.truncate()
            handle.write(str(now))
            handle.flush()
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class ApiRequestContext:
    cache_dir: Path | None
    cache_ttl_seconds: int
    max_retries: int
    arxiv_limiter: RequestRateLimiter
    ncbi_limiter: RequestRateLimiter
    crossref_limiter: RequestRateLimiter
    biorxiv_limiter: RequestRateLimiter | None = None

    def limiter_for(self, url: str) -> RequestRateLimiter | None:
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        if hostname == "export.arxiv.org":
            return self.arxiv_limiter
        if hostname == "eutils.ncbi.nlm.nih.gov":
            return self.ncbi_limiter
        if hostname == "api.crossref.org":
            return self.crossref_limiter
        if hostname == "api.biorxiv.org":
            return self.biorxiv_limiter
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
    positive_auxiliary_terms: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    positive_terms_token = POSITIVE_AUXILIARY_TERMS.set(
        tuple(term.casefold().strip() for term in (positive_auxiliary_terms or []) if len(term.strip()) >= 3)
    )
    start_date, end_date = parse_period_dates(period)
    limit = max(1, retmax)
    fetcher = opener or urllib.request.urlopen
    production_requests = opener is None
    shared_rate_limit_dir = Path(cache_dir) if cache_dir and production_requests else None
    request_context = ApiRequestContext(
        cache_dir=Path(cache_dir) if cache_dir else None,
        cache_ttl_seconds=max(0, cache_ttl_seconds),
        max_retries=3 if production_requests else 0,
        arxiv_limiter=RequestRateLimiter(
            1 / ARXIV_SECONDS_BETWEEN_REQUESTS if production_requests else 1000,
            shared_lock_path=(shared_rate_limit_dir / ".arxiv-rate-limit.lock")
            if shared_rate_limit_dir
            else None,
        ),
        ncbi_limiter=RequestRateLimiter(
            NCBI_REQUESTS_PER_SECOND if production_requests else 1000,
            shared_lock_path=(shared_rate_limit_dir / ".ncbi-rate-limit.lock")
            if shared_rate_limit_dir
            else None,
        ),
        crossref_limiter=RequestRateLimiter(
            (
                CROSSREF_POLITE_REQUESTS_PER_SECOND
                if crossref_mailto
                else CROSSREF_PUBLIC_REQUESTS_PER_SECOND
            )
            if production_requests
            else 1000
        ),
        biorxiv_limiter=RequestRateLimiter(
            BIORXIV_REQUESTS_PER_SECOND if production_requests else 1000,
            shared_lock_path=(shared_rate_limit_dir / ".biorxiv-rate-limit.lock")
            if shared_rate_limit_dir
            else None,
        ),
    )
    coverage: dict[str, Any] = {
        "period": period,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "scan_budget_per_query_source": limit,
        "post_filter_candidate_limit": None,
        "request_policy": {
            "arxiv": "at most one request every three seconds; one connection at a time",
            "pubmed": "at most three requests per second, including when an API key is configured",
            "biorxiv_medrxiv": "conservative client limit of at most one request per second",
            "chemrxiv": "Crossref posted-content only; uses the Crossref public or polite pool policy and honors Retry-After",
            "crossref": "public pool at most five requests per second or polite pool at most ten when mailto is configured; client serializes requests and honors Retry-After",
        },
        "api_queries_run": [],
        "api_failures": [],
        "limitations": "",
    }

    candidates: list[PaperCandidate] = []
    prefilter_excluded_candidates: list[dict[str, Any]] = []
    collectors = [
        (
            "arxiv",
            lambda: fetch_arxiv_candidates(
                start_date,
                end_date,
                limit,
                timeout,
                fetcher,
                coverage,
                request_context,
                prefilter_excluded_candidates,
            ),
        ),
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
                prefilter_excluded_candidates,
            ),
        ),
        (
            "biorxiv",
            lambda: fetch_biorxiv_family_candidates(
                "biorxiv",
                start_date,
                end_date,
                limit,
                timeout,
                fetcher,
                coverage,
                request_context,
                prefilter_excluded_candidates,
            ),
        ),
        (
            "medrxiv",
            lambda: fetch_biorxiv_family_candidates(
                "medrxiv",
                start_date,
                end_date,
                limit,
                timeout,
                fetcher,
                coverage,
                request_context,
                prefilter_excluded_candidates,
            ),
        ),
        (
            "chemrxiv",
            lambda: fetch_chemrxiv_candidates(
                start_date,
                end_date,
                limit,
                timeout,
                fetcher,
                coverage,
                crossref_mailto,
                request_context,
                prefilter_excluded_candidates,
            ),
        ),
        (
            "crossref",
            lambda: fetch_crossref_candidates(
                start_date,
                end_date,
                limit,
                timeout,
                fetcher,
                coverage,
                crossref_mailto,
                request_context,
                prefilter_excluded_candidates,
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
    result = {
        "period": period,
        "queries": DEFAULT_PAPER_QUERIES,
        "candidates": [asdict(candidate) for candidate in deduped],
        "prefilter_excluded_candidates": prefilter_excluded_candidates,
        "coverage": coverage,
    }
    POSITIVE_AUXILIARY_TERMS.reset(positive_terms_token)
    return result


def fetch_arxiv_candidates(
    start_date: date,
    end_date: date,
    retmax: int,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    request_context: ApiRequestContext,
    prefilter_excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[PaperCandidate]:
    scanned: list[PaperCandidate] = []
    categories = " OR ".join(f"cat:{category}" for category in ARXIV_DISCOVERY_CATEGORIES)
    domain_terms = " OR ".join(f"all:{term}" for term in ARXIV_DISCOVERY_TERMS)
    automation_terms = " OR ".join(
        f'all:"{term}"' if " " in term else f"all:{term}"
        for term in ARXIV_AUTOMATION_TERMS
    )
    daily_scan_counts: dict[str, int] = {}
    current_date = start_date
    while current_date <= end_date:
        date_range = (
            f"submittedDate:[{current_date:%Y%m%d}0000 TO "
            f"{current_date:%Y%m%d}2359]"
        )
        search_query = (
            f"({categories}) AND ({domain_terms}) AND ({automation_terms}) "
            f"AND {date_range}"
        )
        start = 0
        while start < ARXIV_MAX_SCAN_RESULTS_PER_DAY:
            page_size = min(
                ARXIV_PAGE_SIZE, ARXIV_MAX_SCAN_RESULTS_PER_DAY - start
            )
            params = {
                "search_query": search_query,
                "start": str(start),
                "max_results": str(page_size),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
            record_api_query(
                coverage,
                api_source="arxiv",
                query=f"date/category sweep date={current_date.isoformat()} offset={start}",
                url=url,
            )
            body = fetch_url(url, timeout, opener, context=request_context)
            page = parse_arxiv_atom(body, "date-category-semantic-filter")
            scanned.extend(page)
            start += len(page)
            if len(page) < page_size:
                break
        daily_scan_counts[current_date.isoformat()] = start
        current_date += timedelta(days=1)

    in_period = dedupe_candidates(
        filter_candidates_by_period(scanned, start_date, end_date)
    )
    returned = semantic_prefilter_candidates(
        in_period,
        api_source="arxiv",
        retmax=None,
        coverage=coverage,
        prefilter_excluded_candidates=prefilter_excluded_candidates,
    )
    semantic_audit = coverage["semantic_prefilter"]["arxiv"]

    coverage["arxiv_discovery"] = {
        "strategy": "submitted-date and category sweep followed by local semantic scoring",
        "categories": list(ARXIV_DISCOVERY_CATEGORIES),
        "domain_terms": list(ARXIV_DISCOVERY_TERMS),
        "automation_terms": list(ARXIV_AUTOMATION_TERMS),
        "scan_limit_per_day": ARXIV_MAX_SCAN_RESULTS_PER_DAY,
        "daily_scan_counts": daily_scan_counts,
        "scanned_candidate_count": len(scanned),
        "in_period_candidate_count": len(in_period),
        "relevance_threshold": PAPER_RELEVANCE_THRESHOLD,
        "relevant_candidate_count": semantic_audit["relevant_candidate_count"],
        "returned_candidate_count": len(returned),
        "source_limit_excluded_count": semantic_audit["source_limit_excluded_count"],
    }
    return returned


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
    prefilter_excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[PaperCandidate]:
    if not ncbi_tool.strip() or not ncbi_email.strip():
        raise ValueError(
            "PubMed requires registered NEWSBOT_NCBI_TOOL and NEWSBOT_NCBI_EMAIL values"
        )
    ids: list[str] = []
    seen_ids: set[str] = set()
    raw_limit = max(retmax, min(retmax * 2, retmax + 50))
    per_query_limit = max(1, min(10, raw_limit))
    for query in DEFAULT_PAPER_QUERIES:
        if len(ids) >= raw_limit:
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
        data = fetch_json_url(
            url,
            timeout,
            opener,
            context=request_context,
            coverage=coverage,
            api_source="pubmed",
        )
        for raw_id in data.get("esearchresult", {}).get("idlist", []):
            pubmed_id = str(raw_id).strip()
            if not pubmed_id or pubmed_id in seen_ids:
                continue
            seen_ids.add(pubmed_id)
            ids.append(pubmed_id)
            if len(ids) >= raw_limit:
                break
    ids = ids[:raw_limit]
    if not ids:
        return semantic_prefilter_candidates(
            [],
            api_source="pubmed",
            retmax=None,
            coverage=coverage,
            prefilter_excluded_candidates=prefilter_excluded_candidates,
        )
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
    parsed = parse_pubmed_xml(
        fetch_url(url, timeout, opener, context=request_context)
    )
    return semantic_prefilter_candidates(
        parsed,
        api_source="pubmed",
        retmax=None,
        coverage=coverage,
        prefilter_excluded_candidates=prefilter_excluded_candidates,
    )


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
    prefilter_excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[PaperCandidate]:
    cursor = 0
    scanned: list[PaperCandidate] = []
    raw_record_count = 0
    metadata_missing_count = 0
    reported_total: int | None = None
    while cursor < BIORXIV_MAX_SCAN_RESULTS:
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
        raw_record_count += len(collection)
        for item in collection:
            candidate = biorxiv_item_to_candidate(item, server)
            if candidate:
                scanned.append(candidate)
            else:
                metadata_missing_count += 1
        reported_total = biorxiv_reported_total(data) or reported_total
        cursor += len(collection)
        if reported_total is not None and cursor >= reported_total:
            break
        if len(collection) < BIORXIV_PAGE_SIZE:
            break
    in_period = filter_candidates_by_period(scanned, start_date, end_date)
    returned = semantic_prefilter_candidates(
        in_period,
        api_source=server,
        retmax=None,
        coverage=coverage,
        prefilter_excluded_candidates=prefilter_excluded_candidates,
    )
    coverage[f"{server}_discovery"] = {
        "strategy": "date-range pagination followed by shared title/abstract semantic scoring",
        "page_size": BIORXIV_PAGE_SIZE,
        "scan_limit": BIORXIV_MAX_SCAN_RESULTS,
        "reported_total": reported_total,
        "raw_record_count": raw_record_count,
        "normalized_candidate_count": len(scanned),
        "metadata_missing_candidate_count": metadata_missing_count,
        "in_period_candidate_count": len(in_period),
        "returned_candidate_count": len(returned),
        "scan_truncated": cursor >= BIORXIV_MAX_SCAN_RESULTS
        and (reported_total is None or cursor < reported_total),
    }
    return returned


def biorxiv_reported_total(data: dict[str, Any]) -> int | None:
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        for key in ("total", "count"):
            try:
                value = int(message.get(key))
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return None


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


def fetch_chemrxiv_candidates(
    start_date: date,
    end_date: date,
    retmax: int,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    crossref_mailto: str,
    request_context: ApiRequestContext,
    prefilter_excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[PaperCandidate]:
    discovery: dict[str, Any] = {
        "strategy": (
            "Crossref posted-content date-range sweep; local ChemRxiv DOI validation; "
            "work-level version deduplication"
        ),
        "routes": {},
    }
    try:
        route_candidates = fetch_chemrxiv_crossref_candidates(
            start_date,
            end_date,
            timeout,
            opener,
            coverage,
            crossref_mailto,
            request_context,
        )
        discovery["routes"]["crossref_posted_content"] = {
            "status": "ok",
            **coverage.get("chemrxiv_route_audit", {}).get(
                "crossref_posted_content", {}
            ),
        }
    except Exception as exc:  # noqa: BLE001 - an API failure must not stop Phase 4.
        record_api_failure(coverage, "chemrxiv_crossref", exc)
        route_candidates = []
        discovery["routes"]["crossref_posted_content"] = {
            "status": "failed",
            "candidate_count": 0,
            "error": redact_sensitive_text(str(exc)),
        }

    in_period = filter_candidates_by_period(route_candidates, start_date, end_date)
    deduped = dedupe_chemrxiv_candidates(in_period)
    discovery.update(
        {
            "candidate_count_before_period_filter": len(route_candidates),
            "candidate_count_in_period": len(in_period),
            "candidate_count_after_doi_dedupe": len(deduped),
            "doi_duplicate_count": len(in_period) - len(deduped),
        }
    )
    coverage["chemrxiv_discovery"] = discovery
    return semantic_prefilter_candidates(
        deduped,
        api_source="chemrxiv",
        retmax=None,
        coverage=coverage,
        prefilter_excluded_candidates=prefilter_excluded_candidates,
    )


def fetch_chemrxiv_crossref_candidates(
    start_date: date,
    end_date: date,
    timeout: int,
    opener: UrlOpen,
    coverage: dict[str, Any],
    crossref_mailto: str,
    request_context: ApiRequestContext,
) -> list[PaperCandidate]:
    candidates: list[PaperCandidate] = []
    offset = 0
    raw_record_count = 0
    chemrxiv_record_count = 0
    metadata_missing_count = 0
    reported_total: int | None = None
    scan_truncated = False
    while raw_record_count < CHEMRXIV_MAX_SCAN_RESULTS:
        rows = min(100, CHEMRXIV_MAX_SCAN_RESULTS - raw_record_count)
        params = {
            "filter": (
                f"from-posted-date:{start_date.isoformat()},"
                f"until-posted-date:{end_date.isoformat()},"
                f"type:posted-content,prefix:{CHEMRXIV_DOI_PREFIX}"
            ),
            "rows": str(rows),
            "offset": str(offset),
            "sort": "published",
            "order": "desc",
        }
        if crossref_mailto.strip():
            params["mailto"] = crossref_mailto.strip()
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        record_api_query(
            coverage,
            api_source="chemrxiv_crossref",
            query="ChemRxiv posted-content date-range sweep",
            url=url,
        )
        data = fetch_json_url(
            url,
            timeout,
            opener,
            context=request_context,
            coverage=coverage,
            api_source="chemrxiv_crossref",
        )
        message = data.get("message") or {}
        items = message.get("items") or []
        try:
            reported_total = int(message.get("total-results"))
        except (TypeError, ValueError):
            pass
        if not items:
            break
        raw_record_count += len(items)
        for item in items:
            if not is_chemrxiv_doi(extract_doi(item.get("DOI"))):
                continue
            chemrxiv_record_count += 1
            candidate = chemrxiv_crossref_item_to_candidate(item)
            if candidate:
                candidates.append(candidate)
            else:
                metadata_missing_count += 1
        offset += len(items)
        if (
            (reported_total is not None and offset >= reported_total)
            or len(items) < rows
        ):
            break
    if raw_record_count >= CHEMRXIV_MAX_SCAN_RESULTS and (
        reported_total is None or raw_record_count < reported_total
    ):
        scan_truncated = True
    coverage.setdefault("chemrxiv_route_audit", {})[
        "crossref_posted_content"
    ] = {
        "raw_record_count": raw_record_count,
        "chemrxiv_record_count": chemrxiv_record_count,
        "non_chemrxiv_record_count": raw_record_count - chemrxiv_record_count,
        "metadata_missing_candidate_count": metadata_missing_count,
        "candidate_count": len(candidates),
        "reported_total": reported_total,
        "scan_limit": CHEMRXIV_MAX_SCAN_RESULTS,
        "scan_truncated": scan_truncated,
    }
    return candidates


def chemrxiv_crossref_item_to_candidate(item: dict[str, Any]) -> PaperCandidate | None:
    if not is_chemrxiv_doi(extract_doi(item.get("DOI"))):
        return None
    candidate = crossref_item_to_candidate(item, "chemrxiv-posted-content")
    if candidate is None:
        return None
    return PaperCandidate(
        **{
            **asdict(candidate),
            "source": "ChemRxiv",
            "source_type": "preprint",
            "api_source": "chemrxiv_crossref",
            "published_date": crossref_posted_date(item) or candidate.published_date,
        }
    )


def dedupe_chemrxiv_candidates(candidates: list[PaperCandidate]) -> list[PaperCandidate]:
    seen: set[str] = set()
    deduped: list[PaperCandidate] = []
    for candidate in candidates:
        doi = normalize_chemrxiv_doi(candidate.doi)
        key = f"doi:{doi}" if doi else candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def normalize_chemrxiv_doi(value: str) -> str:
    doi = extract_doi(value).casefold()
    if CHEMRXIV_DOI_PATTERN.match(doi):
        return re.sub(r"(?:[./-]v)\d+$", "", doi)
    return doi


def is_chemrxiv_doi(value: str) -> bool:
    return bool(CHEMRXIV_DOI_PATTERN.match(extract_doi(value).casefold()))


def record_api_failure(coverage: dict[str, Any], api_source: str, exc: Exception) -> None:
    coverage.setdefault("api_failures", []).append(
        {"api_source": api_source, "error": redact_sensitive_text(str(exc))}
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
    prefilter_excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[PaperCandidate]:
    candidates: list[PaperCandidate] = []
    seen_candidate_keys: set[str] = set()
    raw_limit = max(retmax, min(retmax * 2, retmax + 50))
    per_query_limit = max(1, min(10, raw_limit))
    for query in DEFAULT_PAPER_QUERIES:
        if len(candidates) >= raw_limit:
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
                key = candidate_key(candidate)
                if key in seen_candidate_keys:
                    continue
                seen_candidate_keys.add(key)
                candidates.append(candidate)
                if len(candidates) >= raw_limit:
                    break
    return semantic_prefilter_candidates(
        candidates,
        api_source="crossref",
        retmax=None,
        coverage=coverage,
        prefilter_excluded_candidates=prefilter_excluded_candidates,
    )


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
        try:
            if limiter:
                with limiter.request_slot():
                    with opener(request, timeout=timeout) as response:
                        body = response.read()
            else:
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


def fetch_json_url(
    url: str,
    timeout: int,
    opener: UrlOpen,
    *,
    context: ApiRequestContext | None = None,
    coverage: dict[str, Any] | None = None,
    api_source: str = "",
) -> Any:
    """Fetch JSON and retry once after removing a malformed cached response."""
    last_error: json.JSONDecodeError | None = None
    for parse_attempt in range(2):
        body = fetch_url(url, timeout, opener, context=context)
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            last_error = exc
            cache_removed = invalidate_api_cache(context, url)
            if coverage is not None:
                coverage.setdefault("api_cache_recoveries", []).append(
                    {
                        "api_source": api_source,
                        "reason": "invalid_json",
                        "cache_removed": cache_removed,
                        "retry_attempted": parse_attempt == 0,
                    }
                )
            if parse_attempt == 0:
                continue
    assert last_error is not None
    raise last_error


def invalidate_api_cache(context: ApiRequestContext | None, url: str) -> bool:
    if context is None or context.cache_dir is None:
        return False
    path = api_cache_path(context.cache_dir, url)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def parse_retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.astimezone()
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
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
    return candidate_relevance_score(candidate) >= PAPER_RELEVANCE_THRESHOLD


def semantic_prefilter_candidates(
    candidates: list[PaperCandidate],
    *,
    api_source: str,
    retmax: int | None,
    coverage: dict[str, Any],
    prefilter_excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[PaperCandidate]:
    """Apply a high-recall semantic pass before candidates reach the Phase 4 LLM."""
    deduped = dedupe_candidates(candidates)
    evaluated: list[tuple[int, int, bool, PaperCandidate]] = []
    for candidate in deduped:
        relevance_score = candidate_relevance_score(candidate)
        title_score = candidate_title_relevance_score(candidate)
        sparse_metadata = len(candidate.abstract) < PAPER_SPARSE_ABSTRACT_MIN_CHARS
        evaluated.append(
            (relevance_score, title_score, sparse_metadata, candidate)
        )

    evaluated.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[3].published_date,
            item[3].title.lower(),
        ),
        reverse=True,
    )
    relevant = [
        candidate
        for relevance_score, title_score, sparse_metadata, candidate in evaluated
        if relevance_score >= PAPER_RELEVANCE_THRESHOLD
        or (
            sparse_metadata
            and title_score >= PAPER_SPARSE_METADATA_RELEVANCE_THRESHOLD
        )
    ]
    returned = relevant if retmax is None else relevant[: max(1, retmax)]

    if prefilter_excluded_candidates is not None:
        returned_keys = {candidate_key(candidate) for candidate in returned}
        relevant_keys = {candidate_key(candidate) for candidate in relevant}
        for relevance_score, title_score, sparse_metadata, candidate in evaluated:
            key = candidate_key(candidate)
            if key in returned_keys:
                continue
            if key in relevant_keys:
                assert retmax is not None
                reason_code = "source_limit"
                reason = (
                    f"The candidate passed the {api_source} semantic prefilter but "
                    f"fell below the per-source limit of {retmax}."
                )
            else:
                reason_code = "local_relevance_below_threshold"
                if sparse_metadata:
                    reason = (
                        "The abstract was missing or sparse, and the title alone did "
                        "not contain enough concrete scientific-experiment execution signals."
                    )
                else:
                    reason = (
                        "The title and abstract did not contain enough direct scientific-"
                        "experiment automation, agent-execution, or digital-environment signals."
                    )
            prefilter_excluded_candidates.append(
                {
                    **asdict(candidate),
                    "reason_code": reason_code,
                    "reason": reason,
                    "relevance_score": relevance_score,
                    "title_relevance_score": title_score,
                    "sparse_metadata": sparse_metadata,
                }
            )

    coverage.setdefault("semantic_prefilter", {})[api_source] = {
        "strategy": "high-recall title/abstract semantic scoring before Phase 4 LLM review",
        "raw_candidate_count": len(candidates),
        "deduped_candidate_count": len(deduped),
        "sparse_metadata_candidate_count": sum(item[2] for item in evaluated),
        "relevance_threshold": PAPER_RELEVANCE_THRESHOLD,
        "sparse_title_threshold": PAPER_SPARSE_METADATA_RELEVANCE_THRESHOLD,
        "relevant_candidate_count": len(relevant),
        "returned_candidate_count": len(returned),
        "semantic_excluded_count": len(deduped) - len(relevant),
        "source_limit_excluded_count": max(0, len(relevant) - len(returned)),
        "candidate_limit_applied": retmax,
    }
    return returned


def candidate_relevance_score(candidate: PaperCandidate) -> int:
    """Score direct relevance to automated, agentic, or robotic experimentation.

    This intentionally favors recall. It is only a bounded first pass before the
    Phase 4 LLM performs the evidence-based inclusion/exclusion decision.
    """
    haystack = " ".join(
        [candidate.title, candidate.abstract, candidate.source, *candidate.raw_categories]
    ).lower()
    return relevance_score_for_text(haystack, candidate.raw_categories)


def candidate_title_relevance_score(candidate: PaperCandidate) -> int:
    """Score title-only evidence for records whose abstracts are unavailable."""
    return relevance_score_for_text(candidate.title.lower(), [])


def relevance_score_for_text(text: str, raw_categories: list[str]) -> int:
    haystack = f" {text} "
    auxiliary_bonus = min(2, sum(term in haystack for term in POSITIVE_AUXILIARY_TERMS.get()))
    direct_score = max(
        (
            weight
            for phrase, weight in DIRECT_RELEVANCE_PHRASES.items()
            if phrase in haystack
        ),
        default=0,
    )
    if direct_score:
        return direct_score + min(
            2,
            sum(term in haystack for term in EXECUTION_RELEVANCE_TERMS),
        ) + auxiliary_bonus

    has_domain = any(term in haystack for term in DOMAIN_RELEVANCE_TERMS)
    automation_hits = sum(term in haystack for term in AUTOMATION_RELEVANCE_TERMS)
    execution_hits = sum(term in haystack for term in EXECUTION_RELEVANCE_TERMS)
    if not has_domain or not automation_hits or not execution_hits:
        return 0

    category_bonus = int(
        any(category in ARXIV_DISCOVERY_CATEGORIES for category in raw_categories)
    )
    return 3 + min(3, automation_hits) + min(3, execution_hits) + category_bonus + auxiliary_bonus


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


def extract_doi(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("doi", "value", "id"):
            if value.get(key):
                return extract_doi(value[key])
        return ""
    if isinstance(value, list):
        for item in value:
            doi = extract_doi(item)
            if doi:
                return doi
        return ""
    text = clean_text(str(value or ""))
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)]}")


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
    for key in ("published-print", "published-online", "published", "posted", "created"):
        parts = item.get(key, {}).get("date-parts") or []
        if parts and parts[0]:
            values = parts[0]
            try:
                return date(int(values[0]), int(values[1]) if len(values) > 1 else 1, int(values[2]) if len(values) > 2 else 1).isoformat()
            except (TypeError, ValueError):
                continue
    return ""


def crossref_posted_date(item: dict[str, Any]) -> str:
    posted = item.get("posted", {}).get("date-parts") or []
    if posted and posted[0]:
        values = posted[0]
        try:
            return date(
                int(values[0]),
                int(values[1]) if len(values) > 1 else 1,
                int(values[2]) if len(values) > 2 else 1,
            ).isoformat()
        except (TypeError, ValueError):
            pass
    return crossref_date(item)


def crossref_authors(item: dict[str, Any]) -> list[str]:
    authors = []
    for author in item.get("author", []) or []:
        name = clean_text(" ".join([str(author.get("given") or ""), str(author.get("family") or "")]))
        if name:
            authors.append(name)
    return authors
