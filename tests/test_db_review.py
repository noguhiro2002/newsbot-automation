import tempfile
import unittest
from pathlib import Path

from newsbot.db import NewsbotStore
from newsbot.models import NewsItem


class ReviewDbTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "newsbot.sqlite"
        self.store = NewsbotStore(self.db_path)
        self.store.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_update_draft(self):
        item = NewsItem(
            title="Robotics workflow improves throughput",
            url="https://example.com/robotics?utm_source=test",
            summary="A workflow improvement.",
            source="Example",
            category_primary="robotics",
            tags=("robotics", "workflow"),
            importance_score=0.8,
            priority="high",
            raw={"title": "Robotics workflow improves throughput"},
        )

        draft, created = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)
        self.assertTrue(created)
        self.assertEqual(draft.status, "pending")
        self.assertEqual(draft.category_primary, "robotics")

        draft2, created2 = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)
        self.assertFalse(created2)
        self.assertEqual(draft.id, draft2.id)

        updated = self.store.update_draft_text(
            draft.id,
            "Edited title",
            "Edited body",
            "reviewer-1",
            category_primary="automation",
            tags=["automation"],
            source_url="https://example.com/robotics-updated?utm_campaign=review",
        )
        self.assertEqual(updated.title_edited, "Edited title")
        self.assertEqual(updated.category_primary, "automation")
        self.assertEqual(updated.source_url, "https://example.com/robotics-updated?utm_campaign=review")
        self.assertEqual(updated.canonical_url, "https://example.com/robotics-updated")

    def test_check_source_updates_source_url(self):
        item = NewsItem(
            title="Robotics workflow improves throughput",
            url="https://example.com/old-robotics",
            summary="A workflow improvement.",
            source="Example",
            raw={"title": "Robotics workflow improves throughput"},
        )
        draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)

        updated = self.store.update_draft_source_url(
            draft.id,
            "https://example.com/new-robotics?utm_source=codex",
            "reviewer-1",
            reason="Old URL returned 404.",
        )

        self.assertEqual(updated.source_url, "https://example.com/new-robotics?utm_source=codex")
        self.assertEqual(updated.canonical_url, "https://example.com/new-robotics")

    def test_audience_preference_profile_aggregates_interested_feedback(self):
        robotics = NewsItem(
            title="Robotics workflow improves throughput",
            url="https://example.com/robotics-profile",
            summary="A workflow improvement.",
            source="Example Source",
            category_primary="robotics",
            tags=("robotics", "workflow"),
            raw={"title": "Robotics workflow improves throughput"},
        )
        draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", robotics)
        self.store.record_feedback(draft.id, "user-1")
        self.store.record_feedback(draft.id, "user-2")

        profile = self.store.audience_preference_profile(topic="lab_automation", cadence="weekly")

        self.assertEqual(profile["total_feedback"], 2)
        self.assertEqual(profile["categories"]["robotics"], 2)
        self.assertEqual(profile["tags"]["workflow"], 2)
        self.assertEqual(profile["sources"]["Example Source"], 2)

    def test_status_and_delivery_helpers(self):
        item = NewsItem(
            title="Robotics workflow improves throughput",
            url="https://example.com/robotics",
            summary="A workflow improvement.",
            source="Example",
            raw={"title": "Robotics workflow improves throughput"},
        )
        draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)
        approved = self.store.set_draft_status(
            draft.id,
            "approved_weekly",
            "reviewer-1",
            publish_type="weekly",
            scheduled_at="2026-06-01 08:00 JST",
        )
        self.assertEqual(approved.status, "approved_weekly")
        self.assertEqual(approved.scheduled_at, "2026-06-01 08:00 JST")
        self.assertEqual(self.store.list_drafts_by_status("approved_weekly")[0].id, draft.id)

        ready = self.store.set_draft_status(draft.id, "ready_weekly", "reviewer-1", publish_type="weekly")
        self.assertEqual(ready.status, "ready_weekly")
        self.assertEqual(self.store.list_ready_weekly("lab_automation", "weekly")[0].id, draft.id)

        final_ready = self.store.set_draft_status(draft.id, "final_ready", "reviewer-1", publish_type="weekly")
        self.assertEqual(final_ready.status, "final_ready")
        self.assertEqual(self.store.list_final_ready_weekly("lab_automation", "weekly")[0].id, draft.id)

        published = self.store.mark_published(draft.id, "123", "456", "weekly")
        self.assertEqual(published.status, "published")
        self.assertEqual(published.published_message_id, "456")

    def test_adjust_draft_priority_records_action(self):
        item = NewsItem(
            title="Robotics workflow improves throughput",
            url="https://example.com/robotics-priority",
            summary="A workflow improvement.",
            source="Example",
            raw={"title": "Robotics workflow improves throughput"},
        )
        draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)

        updated = self.store.adjust_draft_priority(draft.id, "reviewer-1", "priority_top")

        self.assertGreater(updated.importance_score, draft.importance_score)

    def test_reorder_final_ready_weekly_updates_publish_order(self):
        drafts = []
        for index in range(3):
            item = NewsItem(
                title=f"Robotics workflow improves throughput {index}",
                url=f"https://example.com/robotics-reorder-{index}",
                summary="A workflow improvement.",
                source="Example",
                importance_score=float(index),
                raw={"title": f"Robotics workflow improves throughput {index}"},
            )
            draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)
            drafts.append(
                self.store.set_draft_status(draft.id, "final_ready", "reviewer-1", publish_type="weekly")
            )

        current_ids = [draft.id for draft in self.store.list_final_ready_weekly("lab_automation", "weekly")]
        reordered = self.store.reorder_final_ready_weekly(
            "lab_automation",
            "weekly",
            list(reversed(current_ids)),
            "reviewer-1",
        )

        self.assertEqual([draft.id for draft in reordered], list(reversed(current_ids)))
        self.assertEqual(
            [draft.id for draft in self.store.list_final_ready_weekly("lab_automation", "weekly")],
            list(reversed(current_ids)),
        )
        with self.assertRaises(ValueError):
            self.store.reorder_final_ready_weekly("lab_automation", "weekly", current_ids[:1], "reviewer-1")

    def test_weekly_batch_helpers_ignore_other_scheduled_items(self):
        first_batch = []
        for index in range(2):
            item = NewsItem(
                title=f"Current batch item {index}",
                url=f"https://example.com/current-batch-{index}",
                summary="A workflow improvement.",
                source="Example",
                importance_score=float(index),
                raw={"title": f"Current batch item {index}"},
            )
            draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)
            first_batch.append(
                self.store.set_draft_status(
                    draft.id,
                    "final_ready",
                    "reviewer-1",
                    publish_type="weekly",
                    scheduled_at="2026-06-08 08:00 JST",
                )
            )

        old_item = NewsItem(
            title="Old batch item",
            url="https://example.com/old-batch",
            summary="An older workflow improvement.",
            source="Example",
            raw={"title": "Old batch item"},
        )
        old_draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", old_item)
        self.store.set_draft_status(
            old_draft.id,
            "final_ready",
            "reviewer-1",
            publish_type="weekly",
            scheduled_at="2026-06-01 08:00 JST",
        )

        current_ids = [
            draft.id
            for draft in self.store.list_final_ready_weekly_batch(
                "lab_automation",
                "weekly",
                "2026-06-08 08:00 JST",
            )
        ]
        reordered = self.store.reorder_final_ready_weekly(
            "lab_automation",
            "weekly",
            list(reversed(current_ids)),
            "reviewer-1",
            scheduled_at="2026-06-08 08:00 JST",
        )

        self.assertEqual([draft.id for draft in reordered], list(reversed(current_ids)))
        self.assertEqual(
            [draft.id for draft in self.store.list_final_ready_weekly_batch("lab_automation", "weekly", "2026-06-01 08:00 JST")],
            [old_draft.id],
        )

    def test_cancelled_weekly_history(self):
        item = NewsItem(
            title="Robotics workflow improves throughput",
            url="https://example.com/robotics-cancel",
            summary="A workflow improvement.",
            source="Example",
            raw={"title": "Robotics workflow improves throughput"},
        )
        draft, _ = self.store.create_or_update_draft_from_item("lab_automation", "weekly", item)
        approved = self.store.set_draft_status(draft.id, "approved_weekly", "reviewer-1", publish_type="weekly")
        pending = self.store.set_draft_status(draft.id, "pending", "reviewer-1")
        self.store.record_review_action(
            draft.id,
            "reviewer-1",
            "cancel_weekly",
            before_json=approved.to_dict(),
            after_json=pending.to_dict(),
        )

        cancelled = self.store.list_recent_cancelled_weekly()

        self.assertEqual(cancelled[0].id, draft.id)


if __name__ == "__main__":
    unittest.main()
