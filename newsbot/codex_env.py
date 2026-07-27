from __future__ import annotations

import os
from typing import Mapping


# Codex authentication and basic process/runtime settings. Application credentials
# must never be inherited by the Codex child process.
CODEX_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CODEX_HOME",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT_ID",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
CODEX_ENV_ALLOWLIST_PREFIXES = ("LC_",)
CODEX_ENV_DENYLIST_PREFIXES = ("DISCORD_", "NCBI_", "X_")


def codex_subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    values = source if source is not None else os.environ
    return {
        key: value
        for key, value in values.items()
        if not key.startswith(CODEX_ENV_DENYLIST_PREFIXES)
        and (key in CODEX_ENV_ALLOWLIST or key.startswith(CODEX_ENV_ALLOWLIST_PREFIXES))
    }
