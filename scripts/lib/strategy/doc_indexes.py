"""doc_indexes.py — Typed loaders for prd_index.json and adr_index.json.

Layer 1 strategic-state: read-only accessors for the document catalogs
produced by scripts/build_doc_indexes.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

try:
    from .. import project_root as _pr_mod
except ImportError:
    import project_root as _pr_mod  # type: ignore[no-redef]

DocStatus = Literal["draft", "active", "superseded", "retired"]


@dataclass(frozen=True)
class DocEntry:
    id: str
    path: str
    version: str
    status: DocStatus
    supersedes: Optional[str]
    title: str


def _load_index(json_path: Path) -> list[DocEntry]:
    if not json_path.exists():
        return []
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    entries: list[DocEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries.append(DocEntry(
            id=str(item.get("id", "")),
            path=str(item.get("path", "")),
            version=str(item.get("version", "")),
            status=item.get("status", "draft"),
            supersedes=item.get("supersedes"),
            title=str(item.get("title", "")),
        ))
    return entries


def _strategy_dir() -> Path:
    return _pr_mod.resolve_data_dir() / "strategy"


def load_prd_index(strategy_dir: Optional[Path] = None) -> list[DocEntry]:
    """Return parsed PRD index; empty list when prd_index.json is absent."""
    d = strategy_dir if strategy_dir is not None else _strategy_dir()
    return _load_index(d / "prd_index.json")


def load_adr_index(strategy_dir: Optional[Path] = None) -> list[DocEntry]:
    """Return parsed ADR index; empty list when adr_index.json is absent."""
    d = strategy_dir if strategy_dir is not None else _strategy_dir()
    return _load_index(d / "adr_index.json")


__all__ = ["DocEntry", "DocStatus", "load_adr_index", "load_prd_index"]
