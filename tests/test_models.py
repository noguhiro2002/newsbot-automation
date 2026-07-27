import unittest

from newsbot.models import NewsItem, NewsPayload, canonicalize_url, title_similarity
from newsbot.render import chunk_text, render_discord_payload, render_x_item


class ModelTests(unittest.TestCase):
    def test_canonicalize_url_removes_tracking_params(self):
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM/news/?utm_source=x&id=1&fbclid=abc"),
            "https://example.com/news?id=1",
        )

    def test_title_similarity_detects_near_duplicate(self):
        left = "Sample Semiconductor reports stronger AI demand"
        right = "Sample Semiconductor reports strong AI demand"
        self.assertGreater(title_similarity(left, right), 0.8)

    def test_chunk_text_keeps_chunks_under_limit(self):
        chunks = chunk_text("a\n" * 100, limit=25)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 25 for chunk in chunks))

    def test_discord_render_uses_digest_summary_and_sections(self):
        payload = NewsPayload(
            topic="lab_automation",
            cadence="weekly",
            period="2026-05-18 to 2026-05-24 (JST)",
            items=[],
        )
        items = [
            NewsItem(
                title="Robotics lab expands autonomous assay workflow",
                url="https://example.com/source-1",
                summary="The workflow update improved overnight throughput.",
            ),
            NewsItem(
                title="New sample-prep release reduces manual steps",
                url="https://example.com/source-2",
                summary="The release combines robotic prep and experiment planning.",
            ),
        ]
        rendered = render_discord_payload(payload, items)

        self.assertIn("News Digest | 2026-05-18 to 2026-05-24 | Lab Automation", rendered)
        self.assertIn("1. Robotics lab expands autonomous assay workflow", rendered)
        self.assertIn("2. New sample-prep release reduces manual steps", rendered)
        self.assertIn("==\n\n1. Robotics lab expands autonomous assay workflow", rendered)
        self.assertIn("Source: https://example.com/source-1", rendered)
        self.assertNotIn("**1.", rendered)

    def test_x_render_preserves_source_when_truncated(self):
        item = NewsItem(
            title="Robotics lab expands autonomous assay workflow",
            url="https://example.com/source",
            summary="A long summary that still keeps the source link even when it needs truncation for posting.",
        )
        rendered = render_x_item(item, index=2, limit=90)

        self.assertIn("2. Robotics lab expands autonomous assay workflow", rendered)
        self.assertIn("Source: https://example.com/source", rendered)
        self.assertLessEqual(len(rendered), 90)


if __name__ == "__main__":
    unittest.main()
