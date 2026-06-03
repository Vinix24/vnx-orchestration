"""provider_lanes.kimi — Re-export of kimi_wrapper public API.

New canonical import: from providers.provider_lanes.kimi import kimi_exec
Old import (backward compat, 90-day alias): from kimi_wrapper import kimi_exec
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[2])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from kimi_wrapper import (  # noqa: F401, E402
    DEFAULT_KIMI_MODEL,
    DEFAULT_TIMEOUT,
    kimi_exec,
)

__all__ = ["kimi_exec", "DEFAULT_KIMI_MODEL", "DEFAULT_TIMEOUT"]
