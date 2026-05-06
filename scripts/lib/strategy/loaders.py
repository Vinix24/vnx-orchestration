"""Boot-path loader for strategy/ folder.

Batches the four read operations build_t0_state needs (roadmap.yaml,
decisions.ndjson, prd_index.json, adr_index.json) behind a single
defensive entry point.  All loads are best-effort: a missing or
malformed file yields an empty / None field rather than raising.

Used by ``build_t0_state._build_strategic_state`` (Phase 2 W-state-5).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .decisions import Decision, recent_decisions
from .doc_indexes import DocEntry, load_adr_index, load_prd_index
from .roadmap import Roadmap, load_roadmap


def _load_roadmap_safe(strategy_dir: Path) -> Optional[Roadmap]:
    rmap = strategy_dir / "roadmap.yaml"
    if not rmap.exists():
        return None
    try:
        return load_roadmap(rmap, strict=False)
    except Exception:
        return None


def _load_decisions_safe(strategy_dir: Path, n: int) -> list[Decision]:
    try:
        return recent_decisions(n, path=strategy_dir / "decisions.ndjson")
    except Exception:
        return []


def _load_prd_index_safe(strategy_dir: Path) -> list[DocEntry]:
    try:
        return load_prd_index(strategy_dir)
    except Exception:
        return []


def _load_adr_index_safe(strategy_dir: Path) -> list[DocEntry]:
    try:
        return load_adr_index(strategy_dir)
    except Exception:
        return []


def load_strategy_for_boot(
    strategy_dir: Path, *, decisions_n: int = 20
) -> dict[str, Any]:
    """Batch-read strategy/ for the boot path; never raises.

    Returns a dict with four keys:
      - ``roadmap``: Optional[Roadmap] (None when roadmap.yaml is absent or
        cannot be parsed in non-strict mode)
      - ``decisions``: list[Decision] — at most ``decisions_n`` entries
        (oldest-first), empty when decisions.ndjson is absent
      - ``prd_index``: list[DocEntry] — empty when prd_index.json is absent
      - ``adr_index``: list[DocEntry] — empty when adr_index.json is absent
    """
    if not strategy_dir.exists() or not strategy_dir.is_dir():
        return {
            "roadmap": None,
            "decisions": [],
            "prd_index": [],
            "adr_index": [],
        }

    return {
        "roadmap": _load_roadmap_safe(strategy_dir),
        "decisions": _load_decisions_safe(strategy_dir, decisions_n),
        "prd_index": _load_prd_index_safe(strategy_dir),
        "adr_index": _load_adr_index_safe(strategy_dir),
    }


__all__ = ["load_strategy_for_boot"]
