from __future__ import annotations

from dataclasses import dataclass


VALID_REVIEW_ACTIONS = {
    "weekly",
    "publish_weekly",
    "publish_digest",
    "reorder_digest",
    "ready_go",
    "edit",
    "breaking",
    "breaking_confirm",
    "check_source",
    "cancel_weekly",
    "priority_top",
    "priority_up",
    "priority_down",
    "cancel_publish",
    "hold",
    "reject",
}
VALID_FEEDBACK_ACTIONS = {"interested"}


@dataclass(frozen=True)
class ParsedCustomId:
    scope: str
    action: str
    article_id: str


def build_review_custom_id(action: str, article_id: str) -> str:
    if action not in VALID_REVIEW_ACTIONS:
        raise ValueError(f"Unsupported review action: {action}")
    return f"nb:review:{action}:{article_id}"


def build_feedback_custom_id(action: str, article_id: str) -> str:
    if action not in VALID_FEEDBACK_ACTIONS:
        raise ValueError(f"Unsupported feedback action: {action}")
    return f"nb:feedback:{action}:{article_id}"


def parse_custom_id(value: str) -> ParsedCustomId:
    parts = (value or "").split(":")
    if len(parts) != 4 or parts[0] != "nb" or not parts[3]:
        raise ValueError(f"Invalid custom_id: {value}")
    scope = parts[1]
    action = parts[2]
    article_id = parts[3]
    if scope == "review" and action in VALID_REVIEW_ACTIONS:
        return ParsedCustomId(scope=scope, action=action, article_id=article_id)
    if scope == "feedback" and action in VALID_FEEDBACK_ACTIONS:
        return ParsedCustomId(scope=scope, action=action, article_id=article_id)
    raise ValueError(f"Invalid custom_id: {value}")


def review_components(
    article_id: str,
    *,
    include_cancel: bool = False,
    exclude: set[str] | None = None,
) -> list[dict[str, object]]:
    excluded = exclude or set()
    actions: list[tuple[str, int, str]] = []
    if include_cancel:
        actions.append(("cancel_weekly", 4, "Cancel Weekly"))
    elif "weekly" not in excluded:
        actions.append(("weekly", 3, "Approve Weekly"))
    actions.extend(
        [
            ("edit", 2, "Edit Draft"),
            ("check_source", 2, "Check Source"),
            ("breaking", 4, "Publish Breaking"),
            ("hold", 2, "Hold"),
            ("reject", 4, "Reject"),
        ]
    )
    components = [
        {
            "type": 2,
            "style": style,
            "label": label,
            "custom_id": build_review_custom_id(action, article_id),
        }
        for action, style, label in actions
        if action not in excluded
    ]
    return [
        {
            "type": 1,
            "components": components[:5],
        },
        {
            "type": 1,
            "components": components[5:],
        },
    ] if len(components) > 5 else [{"type": 1, "components": components}]


def final_weekly_components(article_id: str) -> list[dict[str, object]]:
    actions: list[tuple[str, int, str]] = [
        ("ready_go", 3, "Publish"),
        ("edit", 2, "Edit Draft"),
        ("cancel_publish", 4, "Cancel"),
    ]
    components = [
        {
            "type": 2,
            "style": style,
            "label": label,
            "custom_id": build_review_custom_id(action, article_id),
        }
        for action, style, label in actions
    ]
    return [
        {
            "type": 1,
            "components": components[:5],
        },
        {
            "type": 1,
            "components": components[5:],
        },
    ] if len(components) > 5 else [{"type": 1, "components": components}]


def final_digest_components(article_id: str) -> list[dict[str, object]]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 1,
                    "label": "Reorder",
                    "custom_id": build_review_custom_id("reorder_digest", article_id),
                },
                {
                    "type": 2,
                    "style": 3,
                    "label": "Publish Digest",
                    "custom_id": build_review_custom_id("publish_digest", article_id),
                }
            ],
        }
    ]
