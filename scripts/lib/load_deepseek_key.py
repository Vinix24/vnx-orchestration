"""load_deepseek_key.py — resolve the operator's own DeepSeek API key.

Python mirror of ``scripts/lib/load_deepseek_key.sh``: the value is PARSED out
of the provider env file, never sourced, and only ``DEEPSEEK_API_KEY`` is ever
read from it. The value is never printed or logged.

Every DeepSeek spawn site (``provider_spawns.deepseek_harness_spawn``,
``provider_dispatch._dispatch_deepseek_harness``,
``classifier_providers.deepseek_provider``) resolves its key through
``resolve_deepseek_api_key`` so a key that lives only in the provider env file
still reaches a spawn fired from a shell that never exported it (OI-2182).

Path: ``$VNX_PROVIDER_ENV_FILE``, default ``~/.config/vnx/provider-usage.env``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
PROVIDER_ENV_FILE_ENV = "VNX_PROVIDER_ENV_FILE"
DEFAULT_PROVIDER_ENV_FILE = "~/.config/vnx/provider-usage.env"

# Mirrors the bash loader's grep: optional leading whitespace, optional
# ``export ``, then ``DEEPSEEK_API_KEY=`` and the rest of the line as the value.
_DEEPSEEK_KEY_LINE_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?DEEPSEEK_API_KEY=(.*)$")


def provider_env_file_path() -> Path:
    """Return the provider env file path: $VNX_PROVIDER_ENV_FILE or the default."""
    raw = os.environ.get(PROVIDER_ENV_FILE_ENV) or DEFAULT_PROVIDER_ENV_FILE
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def _strip_quotes(value: str) -> str:
    """Drop a trailing CR and one pair of matching surrounding quotes."""
    value = value.rstrip("\r")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def load_deepseek_api_key_from_file(env_file: "Optional[Path]" = None) -> str:
    """Parse DEEPSEEK_API_KEY out of the provider env file (never source it).

    Returns "" when the file is absent, has no DEEPSEEK_API_KEY line, or the
    value is empty. Only that one variable is ever read.
    """
    path = Path(env_file) if env_file is not None else provider_env_file_path()
    try:
        lines = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in reversed(lines.splitlines()):
        match = _DEEPSEEK_KEY_LINE_RE.match(line)
        if not match:
            continue
        value = _strip_quotes(match.group(1)).strip()
        if value:
            return value
    return ""


def resolve_deepseek_api_key(*, env_file: "Optional[Path]" = None) -> str:
    """Resolve the own DeepSeek key: environment first, then the provider env file.

    Never prints or logs the value. Returns "" when neither source carries one.
    """
    env_value = (os.environ.get(DEEPSEEK_API_KEY_ENV) or "").strip()
    if env_value:
        return env_value
    return load_deepseek_api_key_from_file(env_file)
