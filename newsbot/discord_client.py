from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .notifiers import redact_secret_text


class DiscordApiError(RuntimeError):
    pass


class DiscordBotClient:
    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://discord.com/api/v10",
        message_interval_seconds: float | None = None,
        max_rate_limit_retries: int = 3,
    ):
        token = bot_token.strip()
        if not token:
            raise DiscordApiError("DISCORD_BOT_TOKEN is not set")
        self.bot_token = token
        self.base_url = base_url.rstrip("/")
        self.message_interval_seconds = (
            parse_message_interval(os.getenv("NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS", "1"))
            if message_interval_seconds is None
            else max(0.0, float(message_interval_seconds))
        )
        self.max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._last_message_at = 0.0

    def _wait_for_message_slot(self) -> None:
        if self.message_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_message_at
        remaining = self.message_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _mark_message_sent(self) -> None:
        self._last_message_at = time.monotonic()

    def _retry_after_seconds(self, exc: HTTPError, body_text: str) -> float:
        retry_after = exc.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            return self.message_interval_seconds
        try:
            return max(0.0, float(payload.get("retry_after", self.message_interval_seconds)))
        except (TypeError, ValueError):
            return self.message_interval_seconds

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "User-Agent": "newsbot/0.2",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method.upper())
        for attempt in range(self.max_rate_limit_retries + 1):
            self._wait_for_message_slot()
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                self._mark_message_sent()
                break
            except HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < self.max_rate_limit_retries:
                    sleep_seconds = max(self.message_interval_seconds, self._retry_after_seconds(exc, body_text))
                    time.sleep(sleep_seconds)
                    self._last_message_at = time.monotonic() - self.message_interval_seconds
                    continue
                raise DiscordApiError(f"Discord API error ({exc.code}): {redact_secret_text(body_text)}") from exc
            except Exception as exc:  # noqa: BLE001
                raise DiscordApiError(redact_secret_text(str(exc))) from exc

        if not raw:
            return {}
        return json.loads(raw)

    def create_message(
        self,
        channel_id: str,
        content: str,
        *,
        components: list[dict[str, Any]] | None = None,
        suppress_embeds: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        if suppress_embeds:
            payload["flags"] = 4
        if components:
            payload["components"] = components
        return self._request("POST", f"/channels/{channel_id}/messages", payload)

    def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        components: list[dict[str, Any]] | None = None,
        suppress_embeds: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        if suppress_embeds:
            payload["flags"] = 4
        if components is not None:
            payload["components"] = components
        return self._request("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)


def parse_message_interval(value: str) -> float:
    stripped = value.strip()
    if not stripped:
        return 1.0
    try:
        return max(0.0, float(stripped))
    except ValueError:
        return 1.0
