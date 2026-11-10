#!/usr/bin/env python3
"""Reference grammar and read-only open-item resolver for lessons (ADR-067).

A lesson (memory note, build-log nugget) names open items as ``OI-<n>`` and
memory notes as ``[[name]]``. This module finds those references and looks up
the live status of the open items in the store of the process itself.

Fail-closed: unknown is never open. A missing, unreadable or unbound store, a
reference that is not in the store, a malformed item and an unknown status all
resolve to ``resolved: False`` with a reason, never to ``open``.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, List, Tuple

from vnx_paths import project_id_from_state_dir

_OI_REF_RE = re.compile(r"(?<![A-Za-z0-9_-])OI-[1-9][0-9]*(?![0-9A-Za-z_])")
_MEMORY_REF_RE = re.compile(r"\[\[([^\[\]|#\n]+?)(?:[|#][^\[\]\n]*)?\]\]")

KNOWN_STATUSES = frozenset({"open", "done", "deferred", "wontfix"})
CLOSED_STATUSES = frozenset({"done", "wontfix"})

REASON_STORE_UNAVAILABLE = "store_unavailable"
REASON_NOT_IN_STORE = "not_in_store"
REASON_PROJECT_UNKNOWN = "project_unknown"

# Code form of ADR-067 decision 1 (which bin for what). A change of the routing
# is a change of the ADR; this table follows it and never leads it.
BIN_ROUTING: Dict[str, Dict[str, str]] = {
    "open_item": {
        "home": "state/open_items.json",
        "why": "A problem that outlives the session and needs a status.",
    },
    "memory_note": {
        "home": "~/.claude/projects/<slug>/memory/<name>.md",
        "why": "A durable lesson that changes future behaviour.",
    },
    "build_log_nugget": {
        "home": "build-log-bucket/YYYY-MM.md",
        "why": "A publishable session observation.",
    },
    "learning_loop": {
        "home": "state/quality_intelligence.db",
        "why": "A pattern derived from evidence. A proposal only, never a source.",
    },
}

_INDEX_CACHE: Dict[str, Tuple[Tuple[int, int], Dict[str, dict]]] = {}
_CACHE_LOCK = threading.Lock()


def find_oi_refs(text: str) -> List[str]:
    """Return every ``OI-<n>`` in text, sorted and de-duplicated."""
    return sorted(set(_OI_REF_RE.findall(text or "")))


def find_memory_refs(text: str) -> List[str]:
    """Return every ``[[name]]`` target, de-duplicated, without the alias part."""
    seen: Dict[str, None] = {}
    for m in _MEMORY_REF_RE.finditer(text or ""):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)


def _read_store(path: Path) -> object:
    """Read and parse the store file. One call is one read of the store."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _entry(item: dict) -> dict:
    closed_at = item.get("closed_at")
    pr_id = item.get("pr_id")
    return {
        "status": item.get("status"),
        "closed_at": closed_at if isinstance(closed_at, str) else None,
        "pr_id": str(pr_id) if isinstance(pr_id, (str, int)) and not isinstance(pr_id, bool) else None,
    }


def load_oi_index(state_dir: Path) -> Tuple[Dict[str, dict], str]:
    """Read ``open_items.json`` under state_dir, read-only.

    Returns ``({id: {status, closed_at, pr_id}}, "ok" | "absent" | "error")``.
    ``absent`` when the file does not exist (``read_items`` would silently hand
    back an empty store, so existence is checked first). Items that carry another
    project's ``project_id`` are excluded. A state dir without a derivable
    project id is ``error``: the foreign-item filter cannot be applied.
    Cached on the file's mtime and size.
    """
    path = Path(state_dir) / "open_items.json"
    try:
        if not path.is_file():
            return {}, "absent"
        stat = path.stat()
    except OSError:
        return {}, "error"
    project_id = project_id_from_state_dir(Path(state_dir))
    if not project_id:
        return {}, "error"

    key = f"{path.resolve()}|{project_id}"
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1], "ok"

    try:
        data = _read_store(path)
    except (OSError, ValueError):
        return {}, "error"
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, "error"

    index: Dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        item_project = item.get("project_id")
        if item_project is not None and item_project != project_id:
            continue
        index[item_id] = _entry(item)
    with _CACHE_LOCK:
        _INDEX_CACHE[key] = (stamp, index)
    return index, "ok"


def resolve_oi_refs(refs, index: Dict[str, dict], state: str) -> Dict[str, dict]:
    """Resolve each ref to ``{status, resolved, reason, closed_at, pr_id}``.

    ``state`` is the state of the index load (``ok``, ``absent``, ``error``) or
    ``project_unknown`` when the caller could not derive a project id. Only a
    resolved ref carries a status. Everything doubtful is ``resolved: False``
    with ``status: None`` and a reason, never ``open``.
    """
    out: Dict[str, dict] = {}
    for ref in refs:
        out[ref] = _resolve_one(ref, index, state)
    return out


def _unresolved(reason: str) -> dict:
    return {"status": None, "resolved": False, "reason": reason, "closed_at": None, "pr_id": None}


def _resolve_one(ref: str, index: Dict[str, dict], state: str) -> dict:
    if state == REASON_PROJECT_UNKNOWN:
        return _unresolved(REASON_PROJECT_UNKNOWN)
    if state != "ok" or not isinstance(index, dict):
        return _unresolved(REASON_STORE_UNAVAILABLE)
    entry = index.get(ref)
    if entry is None:
        return _unresolved(REASON_NOT_IN_STORE)
    status = entry.get("status") if isinstance(entry, dict) else None
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        return _unresolved(REASON_STORE_UNAVAILABLE)
    return {
        "status": status,
        "resolved": True,
        "reason": None,
        "closed_at": entry.get("closed_at"),
        "pr_id": entry.get("pr_id"),
    }
