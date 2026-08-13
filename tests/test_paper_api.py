import json
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from newsbot.paper_api import (
    ARXIV_SECONDS_BETWEEN_REQUESTS,
    NCBI_REQUESTS_PER_SECOND,
    ApiRequestContext,
    PaperCandidate,
    RequestRateLimiter,
    api_cache_path,
    collect_paper_api_candidates,
    dedupe_candidates,
    fetch_pubmed_candidates,
    fetch_url,
    parse_arxiv_atom,
    parse_period_dates,
    parse_pubmed_xml,
    redact_sensitive_text,
    redact_sensitive_url,
    redact_sensitive_values,
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
