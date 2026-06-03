"""provider_lanes.gemini — Re-export of gemini_wrapper public API.

New canonical import: from providers.provider_lanes.gemini import gemini_exec
Old import (backward compat, 90-day alias): from gemini_wrapper import gemini_exec
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[2])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from gemini_wrapper import (  # noqa: F401, E402
    DEFAULT_GEMINI_MODEL,
    DEFAULT_TIMEOUT,
    gemini_exec,
)

__all__ = ["gemini_exec", "DEFAULT_GEMINI_MODEL", "DEFAULT_TIMEOUT"]
