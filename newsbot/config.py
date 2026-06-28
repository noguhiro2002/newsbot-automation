from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "newsbot.config.json"


@dataclass(frozen=True)
class Destination:
    topic: str
    type: str
    mode: str
    post_limit_chars: int | None = None


@dataclass(frozen=True)
class DiscordSettings:
    bot_token: str
    review_channel_id: str
    publish_channel_id: str
    reviewer_user_ids: frozenset[str]
    allowed_guild_id: str | None


@dataclass(frozen=True)
class NewsbotConfig:
    raw: dict[str, Any]

    @property
    def max_items(self) -> int:
        return int(self.raw.get("max_items", 30))

    def destination_for(self, topic: str) -> Destination:
        destinations = self.raw.get("destinations", {})
        value = destinations.get(topic)
        if not value:
            raise KeyError(f"No destination configured for topic: {topic}")
        return Destination(
            topic=topic,
            type=str(value["type"]),
            mode=str(value.get("mode", "")),
            post_limit_chars=value.get("post_limit_chars"),
        )

    def has_topic_cadence(self, topic: str, cadence: str) -> bool:
        topics = self.raw.get("topics", {})
        return cadence in topics.get(topic, {}).get("cadences", {})


def parse_csv_ids(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.exists():
        return candidate
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> NewsbotConfig:
    config_path = resolve_project_path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        return NewsbotConfig(json.load(handle))


def load_discord_settings() -> DiscordSettings:
    allowed_guild_id = os.getenv("DISCORD_ALLOWED_GUILD_ID", "").strip() or None
    return DiscordSettings(
        bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        review_channel_id=os.getenv("DISCORD_REVIEW_CHANNEL_ID", "").strip(),
        publish_channel_id=os.getenv("DISCORD_PUBLISH_CHANNEL_ID", "").strip(),
        reviewer_user_ids=parse_csv_ids(os.getenv("DISCORD_REVIEWER_USER_IDS", "")),
        allowed_guild_id=allowed_guild_id,
    )
