import unittest

from newsbot.models import NewsItem
from newsbot.ranking import REVIEW_MAX_ITEMS, rank_news_items, score_news_item


class RankingTests(unittest.TestCase):
    def test_audience_preferences_boost_matching_items(self):
        profile = {
            "categories": {"robotics": 5},
            "tags": {"microscopy": 4},
            "sources": {"Example": 3},
        }
        preferred = NewsItem(
            title="Preferred",
            url="https://example.com/preferred",
            source="Example",
            category_primary="robotics",
            tags=("microscopy",),
            importance_score=0.6,
        )
        generic = NewsItem(
            title="Generic",
            url="https://example.com/generic",
            category_primary="other",
            tags=("other",),
            importance_score=0.7,
        )

        self.assertGreater(score_news_item(preferred, profile), score_news_item(generic, profile))

    def test_rank_news_items_caps_review_limit(self):
        items = [
            NewsItem(title=f"Item {index:02d}", url=f"https://example.com/{index}", importance_score=index / 100)
            for index in range(40)
        ]

        ranked = rank_news_items(items)

        self.assertEqual(len(ranked), REVIEW_MAX_ITEMS)
        self.assertEqual(ranked[0].title, "Item 39")


if __name__ == "__main__":
    unittest.main()
