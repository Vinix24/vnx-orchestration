"""provider_lanes.codex — Re-export of codex_wrapper public API.

New canonical import: from providers.provider_lanes.codex import run_codex
Old import (backward compat, 90-day alias): from codex_wrapper import run_codex
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[2])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from codex_wrapper import (  # noqa: F401, E402
    DEFAULT_CODEX_MODEL,
    DEFAULT_TIMEOUT,
    codex_exec,
)

__all__ = ["codex_exec", "DEFAULT_CODEX_MODEL", "DEFAULT_TIMEOUT"]
