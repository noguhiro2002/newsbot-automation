import unittest

from newsbot.review import (
    build_feedback_custom_id,
    build_review_custom_id,
    final_digest_components,
    final_weekly_components,
    parse_custom_id,
    review_components,
)


class CustomIdTests(unittest.TestCase):
    def test_parse_review_custom_id(self):
        value = build_review_custom_id("weekly", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "review")
        self.assertEqual(parsed.action, "weekly")
        self.assertEqual(parsed.article_id, "article-123")

    def test_parse_feedback_custom_id(self):
        value = build_feedback_custom_id("interested", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "feedback")
        self.assertEqual(parsed.action, "interested")
        self.assertEqual(parsed.article_id, "article-123")

    def test_parse_cancel_weekly_custom_id(self):
        value = build_review_custom_id("cancel_weekly", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "review")
        self.assertEqual(parsed.action, "cancel_weekly")
        self.assertEqual(parsed.article_id, "article-123")

    def test_parse_check_source_custom_id(self):
        value = build_review_custom_id("check_source", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "review")
        self.assertEqual(parsed.action, "check_source")
        self.assertEqual(parsed.article_id, "article-123")

    def test_parse_publish_weekly_custom_id(self):
        value = build_review_custom_id("publish_digest", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "review")
        self.assertEqual(parsed.action, "publish_digest")
        self.assertEqual(parsed.article_id, "article-123")

    def test_parse_reorder_digest_custom_id(self):
        value = build_review_custom_id("reorder_digest", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "review")
        self.assertEqual(parsed.action, "reorder_digest")
        self.assertEqual(parsed.article_id, "article-123")

    def test_parse_ready_go_custom_id(self):
        value = build_review_custom_id("ready_go", "article-123")
        parsed = parse_custom_id(value)

        self.assertEqual(parsed.scope, "review")
        self.assertEqual(parsed.action, "ready_go")
        self.assertEqual(parsed.article_id, "article-123")

    def test_review_components_are_english(self):
        components = review_components("article-123", include_cancel=True)
        labels = [button["label"] for row in components for button in row["components"]]

        self.assertEqual(
            labels,
            ["Cancel Weekly", "Edit Draft", "Check Source", "Publish Breaking", "Hold", "Reject"],
        )
        self.assertLessEqual(max(len(row["components"]) for row in components), 5)

    def test_final_weekly_components_are_english(self):
        components = final_weekly_components("article-123")
        labels = [button["label"] for row in components for button in row["components"]]

        self.assertEqual(labels, ["Publish", "Edit Draft", "Cancel"])
        self.assertLessEqual(max(len(row["components"]) for row in components), 5)

    def test_final_digest_components_are_english(self):
        components = final_digest_components("article-123")
        labels = [button["label"] for row in components for button in row["components"]]

        self.assertEqual(labels, ["Reorder", "Publish Digest"])
        self.assertLessEqual(max(len(row["components"]) for row in components), 5)

    def test_invalid_custom_id_raises(self):
        with self.assertRaises(ValueError):
            parse_custom_id("nb:review:weekly")


if __name__ == "__main__":
    unittest.main()
