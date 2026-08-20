import json
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from newsbot.paper_api import (
    ARXIV_SECONDS_BETWEEN_REQUESTS,
    BIORXIV_PAGE_SIZE,
    CROSSREF_POLITE_REQUESTS_PER_SECOND,
    CROSSREF_PUBLIC_REQUESTS_PER_SECOND,
    NCBI_REQUESTS_PER_SECOND,
    ApiRequestContext,
    PaperCandidate,
    RequestRateLimiter,
    api_cache_path,
    collect_paper_api_candidates,
    candidate_relevance_score,
    dedupe_candidates,
    fetch_arxiv_candidates,
    fetch_biorxiv_family_candidates,
    fetch_chemrxiv_candidates,
    fetch_chemrxiv_crossref_candidates,
    fetch_crossref_candidates,
    fetch_json_url,
    fetch_pubmed_candidates,
    fetch_url,
    normalize_chemrxiv_doi,
    parse_retry_after,
    parse_arxiv_atom,
    parse_period_dates,
    parse_pubmed_xml,
    redact_sensitive_text,
    redact_sensitive_url,
    redact_sensitive_values,
    semantic_prefilter_candidates,
)


class FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class PaperApiTests(unittest.TestCase):
    def test_provider_rate_limits_match_policy(self):
        self.assertEqual(NCBI_REQUESTS_PER_SECOND, 3)
        self.assertEqual(ARXIV_SECONDS_BETWEEN_REQUESTS, 3)
        self.assertEqual(CROSSREF_PUBLIC_REQUESTS_PER_SECOND, 5)
        self.assertEqual(CROSSREF_POLITE_REQUESTS_PER_SECOND, 10)
        self.assertAlmostEqual(
            RequestRateLimiter(NCBI_REQUESTS_PER_SECOND).minimum_interval,
            1 / 3,
        )
        self.assertEqual(
            RequestRateLimiter(1 / ARXIV_SECONDS_BETWEEN_REQUESTS).minimum_interval,
            3,
        )

    def test_arxiv_uses_its_dedicated_limiter(self):
        arxiv_limiter = RequestRateLimiter(1 / ARXIV_SECONDS_BETWEEN_REQUESTS)
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=arxiv_limiter,
            ncbi_limiter=RequestRateLimiter(NCBI_REQUESTS_PER_SECOND),
            crossref_limiter=RequestRateLimiter(5),
        )

        self.assertIs(
            context.limiter_for("https://export.arxiv.org/api/query?search_query=test"),
            arxiv_limiter,
        )

    def test_redacts_sensitive_query_parameters_and_nested_values(self):
        url = "https://example.com/items?term=lab&api_key=dummy&token=dummy"

        redacted_url = redact_sensitive_url(url)
        redacted_values = redact_sensitive_values(
            {"coverage": {"url": url}, "api_key": "dummy"}
        )

        self.assertNotIn("dummy", redacted_url)
        self.assertNotIn("dummy", json.dumps(redacted_values))
        self.assertEqual(redacted_values["api_key"], "<redacted>")

    def test_redacts_sensitive_query_parameters_from_error_text(self):
        text = "request failed: https://example.com?api_key=dummy&term=lab"

        self.assertNotIn("dummy", redact_sensitive_text(text))

    def test_pubmed_sends_key_but_records_only_redacted_url(self):
        requested_urls = []
        coverage = {"api_queries_run": []}

        def fake_opener(request, timeout=20):
            requested_urls.append(request.full_url)
            return FakeResponse(json.dumps({"esearchresult": {"idlist": []}}))

        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )
        fetch_pubmed_candidates(
            date(2026, 6, 22),
            date(2026, 6, 29),
            1,
            20,
            fake_opener,
            coverage,
            "dummy",
            "newsbot_automation",
            "developer@example.com",
            context,
        )

        self.assertIn("api_key=dummy", requested_urls[0])
        recorded = coverage["api_queries_run"][0]["url"]
        self.assertNotIn("dummy", recorded)
        self.assertNotIn("developer%40example.com", recorded)
        self.assertIn("api_key=%3Credacted%3E", recorded)
        self.assertIn("tool=newsbot_automation", recorded)
        self.assertIn("email=%3Credacted%3E", recorded)

    def test_api_cache_key_and_file_do_not_contain_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            url = "https://example.com/items?api_key=dummy&term=lab"
            context = ApiRequestContext(
                cache_dir=cache_dir,
                cache_ttl_seconds=60,
                max_retries=0,
                arxiv_limiter=RequestRateLimiter(1000),
                ncbi_limiter=RequestRateLimiter(1000),
                crossref_limiter=RequestRateLimiter(1000),
            )

            body = fetch_url(
                url,
                20,
                lambda request, timeout=20: FakeResponse("cached"),
                context=context,
            )
            path = api_cache_path(cache_dir, url)

            self.assertEqual(body, b"cached")
            self.assertTrue(path.exists())
            self.assertNotIn("dummy", path.name)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_invalid_cached_json_is_removed_and_retried_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            url = "https://api.crossref.org/works?rows=1"
            responses = iter(["{invalid", '{"message": {"items": []}}'])
            calls = 0

            def fake_opener(request, timeout=20):
                nonlocal calls
                calls += 1
                return FakeResponse(next(responses))

            context = ApiRequestContext(
                cache_dir=cache_dir,
                cache_ttl_seconds=60,
                max_retries=0,
                arxiv_limiter=RequestRateLimiter(1000),
                ncbi_limiter=RequestRateLimiter(1000),
                crossref_limiter=RequestRateLimiter(1000),
            )
            coverage = {}

            value = fetch_json_url(
                url,
                20,
                fake_opener,
                context=context,
                coverage=coverage,
                api_source="crossref",
            )

            self.assertEqual(value["message"]["items"], [])
            self.assertEqual(calls, 2)
            self.assertEqual(len(coverage["api_cache_recoveries"]), 1)
            self.assertTrue(coverage["api_cache_recoveries"][0]["cache_removed"])
            self.assertEqual(
                json.loads(api_cache_path(cache_dir, url).read_text(encoding="utf-8")),
                value,
            )

    def test_retry_after_supports_seconds_and_http_date(self):
        self.assertEqual(parse_retry_after("7"), 7)
        with patch("newsbot.paper_api.time.time", return_value=0):
            self.assertEqual(
                parse_retry_after("Thu, 01 Jan 1970 00:00:10 GMT"),
                10,
            )

    def test_retryable_http_error_is_retried(self):
        calls = 0

        def flaky_opener(request, timeout=20):
            nonlocal calls
            calls += 1
            if calls == 1:
                from urllib.error import HTTPError

                raise HTTPError(request.full_url, 429, "rate limited", {}, None)
            return FakeResponse("ok")

        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=1,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )
        with patch("newsbot.paper_api.time.sleep"):
            body = fetch_url(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/test",
                20,
                flaky_opener,
                context=context,
            )

        self.assertEqual(body, b"ok")
        self.assertEqual(calls, 2)

    def test_parse_period_dates(self):
        start, end = parse_period_dates("2026-06-22 to 2026-06-29 (JST)")

        self.assertEqual(start, date(2026, 6, 22))
        self.assertEqual(end, date(2026, 6, 29))

    def test_parse_arxiv_atom(self):
        body = """
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>https://arxiv.org/abs/2606.12345</id>
            <published>2026-06-23T01:02:03Z</published>
            <title>Self-driving laboratory for autonomous experimentation</title>
            <summary>Robotic closed-loop experiments.</summary>
            <author><name>Ada Example</name></author>
            <category term="cs.RO" />
            <arxiv:doi>10.1234/example</arxiv:doi>
          </entry>
        </feed>
        """

        candidates = parse_arxiv_atom(body, "self-driving laboratory")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "Self-driving laboratory for autonomous experimentation")
        self.assertEqual(candidates[0].published_date, "2026-06-23")
        self.assertEqual(candidates[0].api_source, "arxiv")

    def test_arxiv_date_category_sweep_semantically_keeps_recent_robotic_chemistry(self):
        requested_urls = []
        body = """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2608.00001</id>
            <published>2026-08-10T08:51:43Z</published>
            <title>SAFE-CHEM: Uncertainty-Aware Policy Switching for Robust Robotic Chemistry</title>
            <summary>Autonomous robotic systems in chemistry laboratories switch to a backup controller when policy uncertainty is high.</summary>
            <author><name>Ada Example</name></author>
            <category term="cs.RO" />
          </entry>
          <entry>
            <id>https://arxiv.org/abs/2608.00002</id>
            <published>2026-08-11T10:53:39Z</published>
            <title>ChemWorld: Programmable Chemical Worlds for Controlled and Replayable Agent Experimentation</title>
            <summary>Autonomous chemistry agents act and observe in an environment with replay and audit of state transitions.</summary>
            <author><name>Grace Example</name></author>
            <category term="cs.AI" />
          </entry>
          <entry>
            <id>https://arxiv.org/abs/2608.00003</id>
            <published>2026-08-12T00:00:00Z</published>
            <title>Language modeling for chemical names</title>
            <summary>A benchmark for text classification.</summary>
            <author><name>Lin Example</name></author>
            <category term="cs.LG" />
          </entry>
        </feed>
        """

        def fake_opener(request, timeout=20):
            requested_urls.append(request.full_url)
            if len(requested_urls) == 1:
                return FakeResponse(body)
            return FakeResponse("<feed xmlns='http://www.w3.org/2005/Atom'></feed>")

        coverage = {"api_queries_run": []}
        exclusions = []
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        candidates = fetch_arxiv_candidates(
            date(2026, 8, 10),
            date(2026, 8, 17),
            2,
            20,
            fake_opener,
            coverage,
            context,
            exclusions,
        )

        self.assertEqual(
            {candidate.title.split(":", 1)[0] for candidate in candidates},
            {"SAFE-CHEM", "ChemWorld"},
        )
        self.assertIn("submittedDate%3A%5B202608100000+TO+202608102359%5D", requested_urls[0])
        self.assertIn("cat%3Acs.RO", requested_urls[0])
        self.assertEqual(coverage["arxiv_discovery"]["scanned_candidate_count"], 3)
        self.assertEqual(coverage["arxiv_discovery"]["returned_candidate_count"], 2)
        self.assertEqual(exclusions[0]["reason_code"], "local_relevance_below_threshold")

    def test_arxiv_relevance_limit_is_applied_after_semantic_scoring(self):
        body = """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2608.10001</id>
            <published>2026-08-12T00:00:00Z</published>
            <title>Language modeling for chemical names</title>
            <summary>A benchmark for text classification.</summary>
            <category term="cs.LG" />
          </entry>
          <entry>
            <id>https://arxiv.org/abs/2608.10002</id>
            <published>2026-08-11T00:00:00Z</published>
            <title>Robotic chemistry with safe policy switching</title>
            <summary>A laboratory robot executes manipulation actions under uncertainty.</summary>
            <category term="cs.RO" />
          </entry>
        </feed>
        """
        coverage = {"api_queries_run": []}
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        candidates = fetch_arxiv_candidates(
            date(2026, 8, 10),
            date(2026, 8, 17),
            1,
            20,
            lambda request, timeout=20: FakeResponse(body),
            coverage,
            context,
        )

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].title.startswith("Robotic chemistry"))

    def test_candidate_relevance_score_covers_safe_chem_and_chemworld_language(self):
        safe_chem = PaperCandidate(
            title="SAFE-CHEM: Uncertainty-Aware Policy Switching for Robust Robotic Chemistry",
            abstract="A backup controller protects autonomous robotic systems in chemistry laboratories.",
            authors=[],
            source="arXiv",
            url="https://arxiv.org/abs/2608.09303",
            doi="",
            published_date="2026-08-10",
            source_type="preprint",
            api_source="arxiv",
            matched_query="date-category-semantic-filter",
            raw_categories=["cs.RO"],
        )
        chemworld = PaperCandidate(
            title="ChemWorld: Programmable Chemical Worlds for Controlled and Replayable Agent Experimentation",
            abstract="Autonomous chemistry agents act and observe; execution can be replayed and audited.",
            authors=[],
            source="arXiv",
            url="https://arxiv.org/abs/2608.10792",
            doi="",
            published_date="2026-08-11",
            source_type="preprint",
            api_source="arxiv",
            matched_query="date-category-semantic-filter",
            raw_categories=["cs.AI"],
        )

        self.assertGreaterEqual(candidate_relevance_score(safe_chem), 6)
        self.assertGreaterEqual(candidate_relevance_score(chemworld), 6)

    def test_semantic_prefilter_keeps_digital_environment_and_rejects_generic_benchmark(self):
        candidates = [
            PaperCandidate(
                title="Scientific digital twin for autonomous experimentation",
                abstract="",
                authors=[],
                source="Crossref",
                url="https://doi.org/10.1/digital-twin",
                doi="10.1/digital-twin",
                published_date="2026-08-11",
                source_type="paper",
                api_source="crossref",
                matched_query="autonomous experimentation",
                raw_categories=[],
            ),
            PaperCandidate(
                title="A general benchmark for conversational AI assistants",
                abstract=(
                    "We compare language models on question answering and text classification. "
                    "The benchmark contains no scientific experiment state, operation, resource, "
                    "observation, or executable workflow."
                ),
                authors=[],
                source="Crossref",
                url="https://doi.org/10.1/generic-benchmark",
                doi="10.1/generic-benchmark",
                published_date="2026-08-11",
                source_type="paper",
                api_source="crossref",
                matched_query="robot scientist",
                raw_categories=[],
            ),
        ]
        coverage = {}
        exclusions = []

        selected = semantic_prefilter_candidates(
            candidates,
            api_source="crossref",
            retmax=10,
            coverage=coverage,
            prefilter_excluded_candidates=exclusions,
        )

        self.assertEqual([candidate.doi for candidate in selected], ["10.1/digital-twin"])
        self.assertEqual(exclusions[0]["reason_code"], "local_relevance_below_threshold")
        self.assertTrue(exclusions[0]["sparse_metadata"] is False)
        self.assertEqual(coverage["semantic_prefilter"]["crossref"]["returned_candidate_count"], 1)
        self.assertEqual(coverage["semantic_prefilter"]["crossref"]["semantic_excluded_count"], 1)

    def test_pubmed_semantic_prefilter_keeps_sparse_direct_title_and_audits_noise(self):
        esearch_body = json.dumps({"esearchresult": {"idlist": ["1", "2"]}})
        efetch_body = """
        <PubmedArticleSet>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>1</PMID>
              <Article>
                <Journal><Title>SLAS Technology</Title><JournalIssue><PubDate><Year>2026</Year><Month>Aug</Month><Day>11</Day></PubDate></JournalIssue></Journal>
                <ArticleTitle>Laboratory digital twin for autonomous experimentation</ArticleTitle>
              </Article>
            </MedlineCitation>
          </PubmedArticle>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>2</PMID>
              <Article>
                <Journal><Title>Medical Informatics</Title><JournalIssue><PubDate><Year>2026</Year><Month>Aug</Month><Day>11</Day></PubDate></JournalIssue></Journal>
                <ArticleTitle>Language model benchmark for clinical note classification</ArticleTitle>
                <Abstract><AbstractText>This study compares language models for clinical documentation, administrative coding, question answering, named entity recognition, and summarization across several text datasets.</AbstractText></Abstract>
              </Article>
            </MedlineCitation>
          </PubmedArticle>
        </PubmedArticleSet>
        """

        def fake_opener(request, timeout=20):
            if "esearch.fcgi" in request.full_url:
                return FakeResponse(esearch_body)
            return FakeResponse(efetch_body)

        coverage = {"api_queries_run": []}
        exclusions = []
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        selected = fetch_pubmed_candidates(
            date(2026, 8, 10),
            date(2026, 8, 17),
            2,
            20,
            fake_opener,
            coverage,
            "",
            "newsbot_automation",
            "developer@example.com",
            context,
            exclusions,
        )

        self.assertEqual([candidate.url for candidate in selected], ["https://pubmed.ncbi.nlm.nih.gov/1/"])
        self.assertEqual(exclusions[0]["api_source"], "pubmed")
        self.assertEqual(coverage["semantic_prefilter"]["pubmed"]["raw_candidate_count"], 2)

    def test_crossref_semantic_prefilter_reduces_noise_and_reports_counts(self):
        response = {
            "message": {
                "items": [
                    {
                        "title": ["Closed-loop experimentation in robotic chemistry"],
                        "abstract": "A laboratory robot executes synthesis actions and observes analytical feedback.",
                        "DOI": "10.1/robotic-chemistry",
                        "URL": "https://doi.org/10.1/robotic-chemistry",
                        "container-title": ["Digital Discovery"],
                        "published-online": {"date-parts": [[2026, 8, 11]]},
                    },
                    {
                        "title": ["Language model benchmark for document classification"],
                        "abstract": "We evaluate text classification, question answering, summarization, and retrieval over a large multilingual document collection with standard natural language processing metrics.",
                        "DOI": "10.1/document-benchmark",
                        "URL": "https://doi.org/10.1/document-benchmark",
                        "container-title": ["Information Science"],
                        "published-online": {"date-parts": [[2026, 8, 11]]},
                    },
                ]
            }
        }
        coverage = {"api_queries_run": []}
        exclusions = []
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        selected = fetch_crossref_candidates(
            date(2026, 8, 10),
            date(2026, 8, 17),
            2,
            20,
            lambda request, timeout=20: FakeResponse(json.dumps(response)),
            coverage,
            "developer@example.com",
            context,
            exclusions,
        )

        self.assertEqual([candidate.doi for candidate in selected], ["10.1/robotic-chemistry"])
        self.assertEqual(exclusions[0]["doi"], "10.1/document-benchmark")
        self.assertEqual(coverage["semantic_prefilter"]["crossref"]["semantic_excluded_count"], 1)

    def test_biorxiv_paginates_thirty_item_pages_before_shared_semantic_filter(self):
        requested_urls = []
        noise = [
            {
                "title": f"General protein dataset {index}",
                "abstract": "A descriptive biology dataset without automated experiments.",
                "authors": "Ada Example",
                "doi": f"10.1101/2026.08.11.{index:06d}",
                "date": "2026-08-11",
                "category": "bioinformatics",
            }
            for index in range(BIORXIV_PAGE_SIZE)
        ]
        relevant = {
            "title": "Closed-loop robotic experimentation for cell culture",
            "abstract": "A laboratory robot executes and observes an autonomous biology workflow.",
            "authors": "Grace Example",
            "doi": "10.1101/2026.08.12.999999",
            "date": "2026-08-12",
            "category": "bioengineering",
        }

        def fake_opener(request, timeout=20):
            requested_urls.append(request.full_url)
            collection = noise if request.full_url.endswith("/0") else [relevant]
            return FakeResponse(
                json.dumps(
                    {
                        "messages": [{"total": BIORXIV_PAGE_SIZE + 1}],
                        "collection": collection,
                    }
                )
            )

        coverage = {"api_queries_run": []}
        exclusions = []
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        selected = fetch_biorxiv_family_candidates(
            "biorxiv",
            date(2026, 8, 11),
            date(2026, 8, 18),
            5,
            20,
            fake_opener,
            coverage,
            context,
            exclusions,
        )

        self.assertEqual(len(requested_urls), 2)
        self.assertTrue(requested_urls[1].endswith(f"/{BIORXIV_PAGE_SIZE}"))
        self.assertEqual([candidate.doi for candidate in selected], [relevant["doi"]])
        self.assertEqual(coverage["biorxiv_discovery"]["raw_record_count"], 31)
        self.assertEqual(coverage["semantic_prefilter"]["biorxiv"]["raw_candidate_count"], 31)
        self.assertEqual(coverage["semantic_prefilter"]["biorxiv"]["semantic_excluded_count"], 30)
        self.assertEqual(len(exclusions), 30)

    def test_phase_4_api_semantic_filter_does_not_truncate_qualifying_candidates(self):
        collection = [
            {
                "title": f"Autonomous laboratory closed-loop experiment {index}",
                "abstract": (
                    "A robotic laboratory executes synthesis experiments, observes "
                    "measurements, and selects the next condition."
                ),
                "authors": "Ada Example",
                "doi": f"10.1101/2026.08.1{index}.12345{index}",
                "date": "2026-08-12",
                "category": "Synthetic Biology",
            }
            for index in range(3)
        ]

        def fake_opener(request, timeout=20):
            return FakeResponse(
                json.dumps(
                    {
                        "messages": [{"total": 3}],
                        "collection": collection,
                    }
                )
            )

        coverage = {"api_queries_run": []}
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
            biorxiv_limiter=RequestRateLimiter(1000),
        )

        selected = fetch_biorxiv_family_candidates(
            "biorxiv",
            date(2026, 8, 11),
            date(2026, 8, 18),
            1,
            20,
            fake_opener,
            coverage,
            context,
            [],
        )

        self.assertEqual(len(selected), 3)
        self.assertIsNone(
            coverage["semantic_prefilter"]["biorxiv"]["candidate_limit_applied"]
        )
        self.assertEqual(
            coverage["semantic_prefilter"]["biorxiv"][
                "source_limit_excluded_count"
            ],
            0,
        )

    def test_chemrxiv_uses_crossref_only_and_dedupes_current_and_legacy_versions(self):
        requested_urls = []

        def fake_opener(request, timeout=20):
            requested_urls.append(request.full_url)
            if "api.crossref.org" in request.full_url:
                return FakeResponse(
                    json.dumps(
                        {
                            "message": {
                                "items": [
                                    {
                                        "title": ["Another posted-content item"],
                                        "abstract": "Not a ChemRxiv DOI.",
                                        "DOI": "10.26434/example-2026-other-v1",
                                        "URL": "https://doi.org/10.26434/example-2026-other-v1",
                                        "posted": {"date-parts": [[2026, 8, 12]]},
                                    },
                                    {
                                        "title": ["Autonomous chemistry laboratory with closed-loop synthesis"],
                                        "abstract": "A robotic chemist executes experiments.",
                                        "DOI": "10.26434/chemrxiv.15007396/v2",
                                        "URL": "https://doi.org/10.26434/chemrxiv.15007396/v2",
                                        "posted": {"date-parts": [[2026, 8, 12]]},
                                    },
                                    {
                                        "title": ["Autonomous chemistry laboratory with closed-loop synthesis"],
                                        "abstract": "A robotic chemist executes experiments.",
                                        "DOI": "10.26434/chemrxiv.15007396.v1",
                                        "URL": "https://doi.org/10.26434/chemrxiv.15007396.v1",
                                        "posted": {"date-parts": [[2026, 8, 11]]},
                                    },
                                    {
                                        "title": ["Self-driving laboratory for automated synthesis"],
                                        "abstract": "A robotic chemistry workflow executes closed-loop experiments.",
                                        "DOI": "10.26434/chemrxiv-2026-legacy-v3",
                                        "URL": "https://doi.org/10.26434/chemrxiv-2026-legacy-v3",
                                        "posted": {"date-parts": [[2026, 8, 13]]},
                                    }
                                ],
                                "total-results": 4,
                            }
                        }
                    )
                )
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        coverage = {"api_queries_run": [], "api_failures": []}
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        selected = fetch_chemrxiv_candidates(
            date(2026, 8, 11),
            date(2026, 8, 18),
            10,
            20,
            fake_opener,
            coverage,
            "developer@example.com",
            context,
            [],
        )

        self.assertEqual(len(selected), 2)
        self.assertTrue(all(item.api_source == "chemrxiv_crossref" for item in selected))
        self.assertEqual(coverage["chemrxiv_discovery"]["doi_duplicate_count"], 1)
        crossref_audit = coverage["chemrxiv_discovery"]["routes"]["crossref_posted_content"]
        self.assertEqual(crossref_audit["raw_record_count"], 4)
        self.assertEqual(crossref_audit["chemrxiv_record_count"], 3)
        self.assertEqual(crossref_audit["non_chemrxiv_record_count"], 1)
        self.assertEqual(
            set(coverage["chemrxiv_discovery"]["routes"]),
            {"crossref_posted_content"},
        )
        self.assertEqual(len(requested_urls), 1)
        crossref_url = requested_urls[0]
        self.assertIn("type%3Aposted-content", crossref_url)
        self.assertIn("prefix%3A10.26434", crossref_url)
        self.assertIn("sort=published", crossref_url)
        self.assertNotIn("sort=posted", crossref_url)

    def test_chemrxiv_doi_normalization_supports_current_and_legacy_versions(self):
        expected = "10.26434/chemrxiv-2026-abcde"
        self.assertEqual(
            normalize_chemrxiv_doi("10.26434/chemrxiv-2026-abcde-v2"),
            expected,
        )
        self.assertEqual(
            normalize_chemrxiv_doi("10.26434/chemrxiv-2026-abcde/v3"),
            expected,
        )
        current_expected = "10.26434/chemrxiv.15007396"
        self.assertEqual(
            normalize_chemrxiv_doi("10.26434/CHEMRXIV.15007396/v2"),
            current_expected,
        )
        self.assertEqual(
            normalize_chemrxiv_doi("https://doi.org/10.26434/chemrxiv.15007396.v1"),
            current_expected,
        )

    def test_chemrxiv_crossref_pages_past_non_chemrxiv_prefix_records(self):
        requested_urls = []
        non_chemrxiv_items = [
            {
                "title": [f"Other posted content {index}"],
                "DOI": f"10.26434/example-{index}",
                "URL": f"https://doi.org/10.26434/example-{index}",
                "posted": {"date-parts": [[2026, 8, 12]]},
            }
            for index in range(100)
        ]

        def fake_opener(request, timeout=20):
            requested_urls.append(request.full_url)
            if "offset=100" in request.full_url:
                items = [
                    {
                        "title": ["Autonomous chemistry laboratory"],
                        "abstract": "A robotic chemist executes closed-loop experiments.",
                        "DOI": "10.26434/chemrxiv.15007396/v2",
                        "URL": "https://doi.org/10.26434/chemrxiv.15007396/v2",
                        "posted": {"date-parts": [[2026, 8, 12]]},
                    }
                ]
                total_results = 101
            else:
                items = non_chemrxiv_items
                total_results = 101
            return FakeResponse(
                json.dumps(
                    {
                        "message": {
                            "items": items,
                            "total-results": total_results,
                        }
                    }
                )
            )

        coverage = {"api_queries_run": []}
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        candidates = fetch_chemrxiv_crossref_candidates(
            date(2026, 8, 11),
            date(2026, 8, 18),
            20,
            fake_opener,
            coverage,
            "developer@example.com",
            context,
        )

        self.assertEqual(len(requested_urls), 2)
        self.assertEqual([candidate.doi for candidate in candidates], ["10.26434/chemrxiv.15007396/v2"])
        route = coverage["chemrxiv_route_audit"]["crossref_posted_content"]
        self.assertEqual(route["raw_record_count"], 101)
        self.assertEqual(route["chemrxiv_record_count"], 1)
        self.assertFalse(route["scan_truncated"])

    def test_chemrxiv_crossref_failure_is_audited_without_legacy_fallback(self):
        def fake_opener(request, timeout=20):
            from urllib.error import HTTPError

            self.assertIn("api.crossref.org", request.full_url)
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)

        coverage = {"api_queries_run": [], "api_failures": []}
        context = ApiRequestContext(
            cache_dir=None,
            cache_ttl_seconds=0,
            max_retries=0,
            arxiv_limiter=RequestRateLimiter(1000),
            ncbi_limiter=RequestRateLimiter(1000),
            crossref_limiter=RequestRateLimiter(1000),
        )

        selected = fetch_chemrxiv_candidates(
            date(2026, 8, 11),
            date(2026, 8, 18),
            10,
            20,
            fake_opener,
            coverage,
            "",
            context,
            [],
        )

        self.assertEqual(selected, [])
        self.assertEqual(
            coverage["chemrxiv_discovery"]["routes"]["crossref_posted_content"]["status"],
            "failed",
        )
        self.assertEqual(
            set(coverage["chemrxiv_discovery"]["routes"]),
            {"crossref_posted_content"},
        )
        self.assertEqual(coverage["api_failures"][0]["api_source"], "chemrxiv_crossref")

    def test_parse_pubmed_xml(self):
        body = """
        <PubmedArticleSet>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>123456</PMID>
              <Article>
                <Journal><Title>SLAS Technology</Title><JournalIssue><PubDate><Year>2026</Year><Month>Jun</Month><Day>24</Day></PubDate></JournalIssue></Journal>
                <ArticleTitle>Laboratory automation with robotic experimentation</ArticleTitle>
                <Abstract><AbstractText>Liquid handling automation in a wet lab.</AbstractText></Abstract>
                <AuthorList><Author><ForeName>Ada</ForeName><LastName>Example</LastName></Author></AuthorList>
              </Article>
            </MedlineCitation>
            <PubmedData><ArticleIdList><ArticleId IdType="doi">10.5555/pubmed</ArticleId></ArticleIdList></PubmedData>
          </PubmedArticle>
        </PubmedArticleSet>
        """

        candidates = parse_pubmed_xml(body)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, "SLAS Technology")
        self.assertEqual(candidates[0].published_date, "2026-06-24")
        self.assertEqual(candidates[0].doi, "10.5555/pubmed")

    def test_dedupe_candidates_prefers_first_doi(self):
        first = PaperCandidate(
            title="Self-driving lab",
            abstract="",
            authors=[],
            source="arXiv",
            url="https://arxiv.org/abs/1",
            doi="10.1/example",
            published_date="2026-06-23",
            source_type="preprint",
            api_source="arxiv",
            matched_query="q",
            raw_categories=[],
        )
        second = PaperCandidate(
            title="Self-driving lab duplicate",
            abstract="",
            authors=[],
            source="Crossref",
            url="https://doi.org/10.1/example",
            doi="10.1/example",
            published_date="2026-06-23",
            source_type="paper",
            api_source="crossref",
            matched_query="q",
            raw_categories=[],
        )

        self.assertEqual(dedupe_candidates([first, second]), [first])

    def test_collect_paper_api_candidates_records_api_failures(self):
        def failing_opener(request, timeout=20):
            raise OSError("network unavailable")

        result = collect_paper_api_candidates(
            period="2026-06-22 to 2026-06-29 (JST)",
            retmax=2,
            opener=failing_opener,
        )

        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["coverage"]["api_failures"])
        self.assertIn("Some paper APIs failed", result["coverage"]["limitations"])

    def test_collect_paper_api_candidates_normalizes_biorxiv_and_crossref(self):
        def fake_opener(request, timeout=20):
            url = request.full_url
            if "export.arxiv.org" in url:
                return FakeResponse("<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
            if "esearch.fcgi" in url:
                return FakeResponse(json.dumps({"esearchresult": {"idlist": []}}))
            if "api.biorxiv.org/details/biorxiv" in url:
                return FakeResponse(
                    json.dumps(
                        {
                            "collection": [
                                {
                                    "title": "Self-driving lab for automated synthesis",
                                    "abstract": "A robotic laboratory automation workflow.",
                                    "authors": "Ada Example; Alan Example",
                                    "doi": "10.1101/2026.06.24.123456",
                                    "date": "2026-06-24",
                                    "category": "bioengineering",
                                }
                            ]
                        }
                    )
                )
            if "api.biorxiv.org/details/medrxiv" in url:
                return FakeResponse(json.dumps({"collection": []}))
            if "api.crossref.org" in url:
                return FakeResponse(
                    json.dumps(
                        {
                            "message": {
                                "items": [
                                    {
                                        "title": ["Closed-loop experimentation in a robotic lab"],
                                        "abstract": "Autonomous experimentation.",
                                        "DOI": "10.5555/crossref",
                                        "URL": "https://doi.org/10.5555/crossref",
                                        "container-title": ["Digital Discovery"],
                                        "published-online": {"date-parts": [[2026, 6, 25]]},
                                        "author": [{"given": "Grace", "family": "Example"}],
                                        "subject": ["Chemistry"],
                                    }
                                ]
                            }
                        }
                    )
                )
            return FakeResponse("{}")

        result = collect_paper_api_candidates(
            period="2026-06-22 to 2026-06-29 (JST)",
            retmax=2,
            opener=fake_opener,
        )

        titles = [candidate["title"] for candidate in result["candidates"]]
        self.assertIn("Self-driving lab for automated synthesis", titles)
        self.assertIn("Closed-loop experimentation in a robotic lab", titles)
        self.assertEqual(result["coverage"]["candidate_count_after_dedupe"], 2)


if __name__ == "__main__":
    unittest.main()
