from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from newsbot.db import NewsbotStore
from newsbot.feed_discovery import FeedSource, collect_feed_candidates, parse_feed_document
from newsbot.models import ArticleDraft, NewsItem
from newsbot.render import render_weekly_digest_overview
from scripts.generate_payload_openai import feedback_lookback_weeks, merge_discovery_lane_outputs


def lane_output(mode: str, url: str = "https://example.com/item") -> dict:
    coverage = {
        "merged_candidate_count": 1,
        "notable_zero_result_queries": [],
        "limitations": "",
    }
    if mode == "structured":
        coverage.update({"structured_queries_run": ["q"], "structured_candidate_count": 1, "broad_queries_run": []})
    else:
        coverage.update({"broad_queries_run": ["q1", "q2", "q3"], "broad_candidate_count": 1})
    return {
        "phase": "phase_1_domestic_official",
        "period": "2026-08-11 to 2026-08-18 (JST)",
        "candidates": [{
            "title": "Automation item", "source": "Example", "url": url,
            "published_date": "2026-08-17", "source_type": "official", "geography": "Japan",
            "lab_automation_relevance": "direct", "evidence": "verified", "confidence": 0.8,
            "canonical_source_checked": True, "duplicate_key": "automation-item",
            "discovery_mode": mode, "discovery_modes": [mode],
        }],
        "search_coverage": coverage,
    }


class FeedbackConfigTests(unittest.TestCase):
    def test_feedback_window_default_override_and_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(feedback_lookback_weeks(), 12)
        with patch.dict(os.environ, {"NEWSBOT_FEEDBACK_LOOKBACK_WEEKS": "4"}, clear=True):
            self.assertEqual(feedback_lookback_weeks(), 4)
        for value in ("0", "-1", "1.5", "weeks"):
            with self.subTest(value=value), patch.dict(os.environ, {"NEWSBOT_FEEDBACK_LOOKBACK_WEEKS": value}, clear=True):
                with self.assertRaises(ValueError):
                    feedback_lookback_weeks()

    def test_editorial_window_boundary_and_profile_hash_audit_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NewsbotStore(Path(temp_dir) / "db.sqlite")
            store.init()
            now = datetime(2026, 8, 18, 12, tzinfo=UTC)
            start = now - timedelta(weeks=4)
            ids = [
                store.record_editorial_judgment(decision="missed", reason_code="search_miss", reviewer_user_id="u", topic="lab_automation", cadence="weekly", canonical_url=f"https://example.com/{index}", title=f"Item {index}", phase="phase_3", organization="Org", domain="example.com")
                for index in range(3)
            ]
            with store.connect() as conn:
                conn.execute("UPDATE editorial_judgments SET created_at = ? WHERE id = ?", ((start - timedelta(seconds=1)).isoformat(timespec="seconds"), ids[0]))
                conn.execute("UPDATE editorial_judgments SET created_at = ? WHERE id = ?", (start.isoformat(timespec="seconds"), ids[1]))
                conn.execute("UPDATE editorial_judgments SET created_at = ? WHERE id = ?", (now.isoformat(timespec="seconds"), ids[2]))
            profile = store.editorial_feedback_profile(topic="lab_automation", cadence="weekly", lookback_weeks=4, now=now)
            self.assertEqual(profile["judgment_count"], 2)
            self.assertEqual(profile["dynamic_watchlist"], ["Org", "example.com"])
            run_id = store.start_pipeline_run(topic="lab_automation", cadence="weekly", period="p", feedback_lookback_weeks=4, feedback_window_start=profile["window_start"], feedback_window_end=profile["window_end"], feedback_profile_hash="abc")
            store.record_pipeline_candidates(run_id, "phase_1", lane_output("structured")["candidates"])
            store.finish_pipeline_run(run_id, status="complete", audit={"profile_hash": "abc"})
            with store.connect() as conn:
                row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
                count = conn.execute("SELECT COUNT(*) AS count FROM pipeline_candidates WHERE run_id = ?", (run_id,)).fetchone()["count"]
            self.assertEqual(row["feedback_lookback_weeks"], 4)
            self.assertEqual(row["feedback_profile_hash"], "abc")
            self.assertEqual(count, 1)

    def test_reason_is_mandatory_and_other_needs_note(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NewsbotStore(Path(temp_dir) / "db.sqlite")
            store.init()
            with self.assertRaises(ValueError):
                store.record_editorial_judgment(decision="missed", reason_code="", reviewer_user_id="u", topic="lab_automation", cadence="weekly", title="x")
            with self.assertRaises(ValueError):
                store.record_editorial_judgment(decision="missed", reason_code="other", reviewer_user_id="u", topic="lab_automation", cadence="weekly", title="x")


class DiscoveryLaneTests(unittest.TestCase):
    def test_isolated_lanes_merge_provenance(self):
        merged = merge_discovery_lane_outputs(lane_output("structured"), lane_output("broad"), expected_phase="phase_1_domestic_official")
        self.assertEqual(len(merged["candidates"]), 1)
        self.assertEqual(set(merged["candidates"][0]["discovery_modes"]), {"structured", "broad"})

    def test_feed_parsing_filtering_failure_and_dedupe(self):
        rss = b"""<rss><channel><item><title>A</title><link>https://example.com/a?utm_source=x</link><pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
        self.assertEqual(parse_feed_document(rss, "https://example.com")[0]["title"], "A")
        sources = [FeedSource("ok", "OK", "https://example.com/rss", "phase_1"), FeedSource("bad", "Bad", "https://bad/rss", "phase_1")]

        def fake_fetch(source, cache_dir, timeout=30):
            if source.source_id == "bad":
                raise RuntimeError("down")
            return {"entries": parse_feed_document(rss, source.url), "http_status": 200, "etag": "x", "last_modified": "", "fetched_at": "now"}

        with patch("newsbot.feed_discovery.fetch_source", side_effect=fake_fetch):
            result = collect_feed_candidates(period="2026-08-11 to 2026-08-18", sources=sources, cache_dir="/tmp/newsbot-feed-test")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["candidates"][0]["discovery_modes"], ["feed"])


class EventRenderTests(unittest.TestCase):
    def _draft(self, identifier: str, kind: str, event_date: str = "") -> ArticleDraft:
        import json
        return ArticleDraft(identifier, "lab_automation", "weekly", "Source", f"https://example.com/{identifier}", f"https://example.com/{identifier}", identifier, identifier, "body", identifier, "body", "category", "[]", 1.0, "final_ready", "weekly", "normal", "", "", "", "", json.dumps({"content_kind": kind, "event_date_start": event_date}), "", "", "", "")

    def test_events_are_rendered_in_separate_section(self):
        text = render_weekly_digest_overview([self._draft("event", "event_occurrence", "2026-08-20"), self._draft("news", "news")])
        self.assertIn("News\n1. news", text)
        self.assertIn("Events\n2. event (2026-08-20)", text)

    def test_event_date_does_not_replace_publication_date(self):
        item = NewsItem.from_dict({"title": "Event", "url": "https://example.com/event", "content_kind": "event_occurrence", "published_date": "2026-07-01", "event_date_start": "2026-08-20", "date_basis": "event"})
        self.assertEqual(item.published_date, "2026-07-01")
        self.assertEqual(item.event_date_start, "2026-08-20")


if __name__ == "__main__":
    unittest.main()
