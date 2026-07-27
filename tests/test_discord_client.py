import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from newsbot.discord_client import DiscordBotClient, parse_message_interval


class RecordingDiscordBotClient(DiscordBotClient):
    def __init__(self):
        super().__init__("fake-token")
        self.requests = []

    def _request(self, method, path, payload=None):
        self.requests.append({"method": method, "path": path, "payload": payload or {}})
        return {"id": "message-1"}


class DiscordClientTests(unittest.TestCase):
    def test_create_message_suppresses_embeds_by_default(self):
        client = RecordingDiscordBotClient()

        client.create_message("channel-1", "Source: https://example.com")

        self.assertEqual(client.requests[0]["payload"]["flags"], 4)

    def test_edit_message_suppresses_embeds_by_default(self):
        client = RecordingDiscordBotClient()

        client.edit_message("channel-1", "message-1", "Source: https://example.com")

        self.assertEqual(client.requests[0]["payload"]["flags"], 4)

    def test_parse_message_interval_defaults_for_invalid_values(self):
        self.assertEqual(parse_message_interval(""), 1.0)
        self.assertEqual(parse_message_interval("invalid"), 1.0)
        self.assertEqual(parse_message_interval("-1"), 0.0)
        self.assertEqual(parse_message_interval("2.5"), 2.5)

    def test_request_retries_rate_limit_response(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"id":"message-2"}'

        rate_limit_error = HTTPError(
            url="https://discord.test/channels/channel-1/messages",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "0.25"},
            fp=BytesIO(b'{"retry_after":0.25}'),
        )
        client = DiscordBotClient(
            "fake-token",
            base_url="https://discord.test",
            message_interval_seconds=0,
        )

        with patch("newsbot.discord_client.urlopen", side_effect=[rate_limit_error, FakeResponse()]) as urlopen_mock:
            with patch("newsbot.discord_client.time.sleep") as sleep_mock:
                response = client.create_message("channel-1", "hello")

        self.assertEqual(response["id"], "message-2")
        self.assertEqual(urlopen_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
