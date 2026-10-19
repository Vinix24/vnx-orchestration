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

    Returns "" when the file is absent, cannot be decoded as UTF-8, has no
    DEEPSEEK_API_KEY line, or the last matching line's value is empty. Only
    that one variable is ever read.
    """
    path = Path(env_file) if env_file is not None else provider_env_file_path()
    try:
        lines = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in reversed(lines.splitlines()):
        match = _DEEPSEEK_KEY_LINE_RE.match(line)
        if not match:
            continue
        # The last matching assignment wins even when empty (the bash loader's
        # ``tail -n 1``): an empty final ``DEEPSEEK_API_KEY=`` must not
        # resurrect an earlier key (OI-2187).
        return _strip_quotes(match.group(1)).strip()
    return ""


def resolve_deepseek_api_key(
    *, env_file: "Optional[Path]" = None, env: "Optional[dict]" = None
) -> str:
    """Resolve the own DeepSeek key: environment first, then the provider env file.

    ``env`` overrides the process environment for the KEY lookup only, so the
    smart router can pass its decision-time env view and a lane check never
    reads an ambient DEEPSEEK_API_KEY the caller chose not to model. The
    provider env file path still comes from the process environment
    (``VNX_PROVIDER_ENV_FILE``), exactly as in every other call site.

    Never prints or logs the value. Returns "" when neither source carries one.
    """
    _env = env if env is not None else os.environ
    env_value = (_env.get(DEEPSEEK_API_KEY_ENV) or "").strip()
    if env_value:
        return env_value
    return load_deepseek_api_key_from_file(env_file)
