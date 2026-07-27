from __future__ import annotations

from .db import NewsbotStore
from .review import build_feedback_custom_id


INTERESTED_ACTION = "interested"
INTERESTED_EMOJI = "\U0001f44d"


def feedback_components(article_id: str, interested_count: int = 0) -> list[dict[str, object]]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 1,
                    "label": f"Interested ({interested_count})",
                    "emoji": {"name": INTERESTED_EMOJI},
                    "custom_id": build_feedback_custom_id(INTERESTED_ACTION, article_id),
                }
            ],
        }
    ]


def record_interested_feedback(
    store: NewsbotStore,
    *,
    article_id: str,
    user_id: str,
    channel_id: str | None = None,
    message_id: str | None = None,
) -> bool:
    return store.record_feedback(
        article_id,
        user_id,
        action_type=INTERESTED_ACTION,
        weight=1,
        channel_id=channel_id,
        message_id=message_id,
    )
