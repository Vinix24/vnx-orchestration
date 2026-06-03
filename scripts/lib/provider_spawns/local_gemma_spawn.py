"""local_gemma_spawn.py — Shim re-exporting from providers.local_gemma.spawn.

Canonical location: scripts/lib/providers/local_gemma/spawn.py
This shim preserves provider_spawns/ namespace consistency (PR-4.6 convention).

BILLING SAFETY: delegates entirely to providers.local_gemma.spawn.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from providers.local_gemma.spawn import (  # noqa: F401, E402
    DEFAULT_MLX_MODEL,
    DEFAULT_OLLAMA_MODEL,
    LocalGemmaSpawnResult,
    spawn_local_gemma,
)

__all__ = [
    "spawn_local_gemma",
    "LocalGemmaSpawnResult",
    "DEFAULT_MLX_MODEL",
    "DEFAULT_OLLAMA_MODEL",
]
