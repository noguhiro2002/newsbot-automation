import json
import unittest
from datetime import date

from newsbot.paper_api import (
    PaperCandidate,
    collect_paper_api_candidates,
    dedupe_candidates,
    parse_arxiv_atom,
    parse_period_dates,
    parse_pubmed_xml,
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
