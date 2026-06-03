"""provider_lanes.deepseek — Re-export of DeepSeek harness spawn public API.

New canonical import: from providers.provider_lanes.deepseek import spawn_deepseek_harness
Route: own-key key-auth harness lane (ADR-003 safe; no Anthropic SDK).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[2])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from provider_spawns.deepseek_harness_spawn import (  # noqa: F401, E402
    DEFAULT_DEEPSEEK_HARNESS_MODEL,
    DEEPSEEK_API_KEY_ENV,
    DeepSeekHarnessSpawnResult,
    resolve_harness_model,
    spawn_deepseek_harness,
)

__all__ = [
    "spawn_deepseek_harness",
    "DeepSeekHarnessSpawnResult",
    "resolve_harness_model",
    "DEFAULT_DEEPSEEK_HARNESS_MODEL",
    "DEEPSEEK_API_KEY_ENV",
]
