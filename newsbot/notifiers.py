from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


class NotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class NotificationResult:
    status: str
    response_code: int | None = None
    response_body: str | None = None
    error_message: str | None = None


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> NotificationResult:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "newsbot/0.1",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return NotificationResult("sent", response.getcode(), response_body)
    except Exception as exc:  # noqa: BLE001 - preserve remote error text for notification logs.
        return NotificationResult("failed", error_message=redact_secret_text(str(exc)))


def redact_secret_text(value: str) -> str:
    patterns = [
        r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+",
        r"sk-proj-[A-Za-z0-9_-]+",
        r"sk-[A-Za-z0-9]+",
        r"Bearer\s+[A-Za-z0-9._~+/=-]+",
        r"OAuth\s+[^,\s]+",
    ]
    redacted = value
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted


def send_discord(message: str) -> NotificationResult:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise NotificationError("DISCORD_WEBHOOK_URL is not set")
    return post_json(webhook_url, {"content": message})


def _oauth_quote(value: str) -> str:
    return quote(value, safe="~-._")


def _oauth1_header(method: str, url: str, body: dict[str, Any]) -> str:
    consumer_key = os.getenv("X_API_KEY", "").strip()
    consumer_secret = os.getenv("X_API_SECRET", "").strip()
    access_token = os.getenv("X_ACCESS_TOKEN", "").strip()
    access_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "").strip()
    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        raise NotificationError(
            "X OAuth 1.0a credentials are not set. Required: X_API_KEY, X_API_SECRET, "
            "X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET"
        )

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    signature_params = {**oauth_params}
    parameter_string = "&".join(
        f"{_oauth_quote(key)}={_oauth_quote(value)}" for key, value in sorted(signature_params.items())
    )
    base_string = "&".join([method.upper(), _oauth_quote(url), _oauth_quote(parameter_string)])
    signing_key = f"{_oauth_quote(consumer_secret)}&{_oauth_quote(access_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return "OAuth " + ", ".join(
        f'{_oauth_quote(key)}="{_oauth_quote(value)}"' for key, value in sorted(oauth_params.items())
    )


def send_x_post(message: str) -> NotificationResult:
    url = "https://api.x.com/2/tweets"
    bearer_token = os.getenv("X_BEARER_TOKEN", "").strip()
    headers: dict[str, str]
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}
    else:
        headers = {"Authorization": _oauth1_header("POST", url, {"text": message})}
    return post_json(url, {"text": message}, headers)
