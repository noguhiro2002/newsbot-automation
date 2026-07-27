from __future__ import annotations

import unittest

from newsbot.codex_env import codex_subprocess_env


class CodexEnvironmentTests(unittest.TestCase):
    def test_only_allowlisted_values_are_inherited(self):
        result = codex_subprocess_env(
            {
                "PATH": "/usr/bin",
                "HOME": "/home/newsbot",
                "CODEX_HOME": "/home/newsbot/.codex",
                "HTTPS_PROXY": "http://proxy.example",
                "LANG": "C.UTF-8",
                "LC_TIME": "C",
                "NEWSBOT_CODEX_MODEL": "gpt-test",
                "UNRELATED_VALUE": "do-not-pass",
            }
        )

        self.assertEqual(
            result,
            {
                "PATH": "/usr/bin",
                "HOME": "/home/newsbot",
                "CODEX_HOME": "/home/newsbot/.codex",
                "HTTPS_PROXY": "http://proxy.example",
                "LANG": "C.UTF-8",
                "LC_TIME": "C",
            },
        )

    def test_application_credentials_are_always_denied(self):
        result = codex_subprocess_env(
            {
                "PATH": "/usr/bin",
                "DISCORD_BOT_TOKEN": "discord-secret",
                "DISCORD_WEBHOOK_URL": "discord-webhook",
                "NCBI_API_KEY": "ncbi-secret",
                "NEWSBOT_NCBI_EMAIL": "private@example.com",
                "NEWSBOT_CROSSREF_MAILTO": "private@example.com",
                "X_API_KEY": "x-secret",
            }
        )

        self.assertEqual(result, {"PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
