"""Project-bound readers for the capture audit (ADR-067, capture PR-2).

The audit compares open items, memory notes and build-log nuggets of ONE
project. Three of those stores are operator property outside the repo, so the
audit reads them only through an explicit binding file,
``<state_dir>/capture_sources.json``. Every reader is fail-closed: a source
that is unbound, absent or broken gets a state of its own and is never an
empty ``ok`` list.

States: ``ok``, ``absent`` (the store does not exist), ``unbound`` (no valid
binding, so nothing may be read) and ``error`` (the store exists but cannot be
read as a whole).

Nothing here writes. Readers return titles and bodies in memory only (the
caller needs them for token matching); the persisted forms are hashes and
counts (decision 19).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vnx_paths import project_id_from_state_dir

BINDING_FILENAME = "capture_sources.json"
BINDING_SCHEMA = "capture_sources/1"
BINDING_KEYS = ("schema", "project_id", "memory_dir", "bucket_dir", "bucket_slug", "bound_at")

STATE_OK = "ok"
STATE_ABSENT = "absent"
STATE_UNBOUND = "unbound"
STATE_ERROR = "error"

_OI_REF_RE = re.compile(r"(?<![A-Za-z0-9])OI-[1-9][0-9]*(?![0-9])")
_MEM_REF_RE = re.compile(r"\[\[([^\]\|#]+)")
_NEWLINE_RE = re.compile(r"\r?\n")


def projects_root() -> Path:
    """The only tree a bound memory dir may live under (resolved at call time)."""
    return (Path.home() / ".claude" / "projects").resolve()


def state_of(status: str) -> str:
    """``"unbound: reason"`` -> ``"unbound"``; a plain state passes through."""
    return status.split(":", 1)[0].strip()


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _locator(project_id: str, bin_name: str, key: str) -> str:
    raw = f"{project_id}|{bin_name}|{key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def find_refs(text: str) -> Dict[str, List[str]]:
    """Open-item and memory references in free text, sorted and de-duplicated."""
    return {
        "oi": sorted(set(_OI_REF_RE.findall(text))),
        "memory": sorted({m.strip() for m in _MEM_REF_RE.findall(text) if m.strip()}),
    }


# --- binding ---------------------------------------------------------------

def _binding_problem(data: Any, state_dir: Path) -> Optional[str]:
    """First reason a parsed binding is unusable, or None. Fail-closed."""
    if not isinstance(data, dict):
        return "binding is not a JSON object"
    if data.get("schema") != BINDING_SCHEMA:
        return "unsupported binding schema"
    for key in ("project_id", "memory_dir", "bucket_dir", "bucket_slug"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"binding field {key} missing or empty"
    derived = project_id_from_state_dir(state_dir)
    if not derived:
        return "project id cannot be derived from the state dir"
    if data["project_id"] != derived:
        return "binding project_id differs from the state dir project"
    if _memory_dir_problem(data["memory_dir"]):
        return "memory_dir is not under ~/.claude/projects/"
    return None


def _memory_dir_problem(memory_dir: str) -> Optional[str]:
    try:
        resolved = Path(memory_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return "memory_dir cannot be resolved"
    if not _under(resolved, projects_root()) or resolved == projects_root():
        return "memory_dir outside the allowed tree"
    return None


def load_binding(state_dir: Path) -> Tuple[Optional[dict], str]:
    """Read and validate the binding. ``(binding, "ok")`` or ``(None, "unbound: <reason>")``."""
    path = Path(state_dir) / BINDING_FILENAME
    if not path.is_file():
        return None, "unbound: binding file missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unbound: binding file unreadable or corrupt"
    problem = _binding_problem(data, Path(state_dir))
    if problem:
        return None, f"unbound: {problem}"
    return data, STATE_OK


# --- memory notes ----------------------------------------------------------

def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    lines = _NEWLINE_RE.split(text)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: Dict[str, str] = {}
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return meta, "\n".join(lines[idx + 1:])
        key, sep, value = lines[idx].strip().partition(":")
        if sep and key in ("name", "description", "modified"):
            meta[key] = value.strip().strip("'\"")
    return {}, text


def _to_utc_iso(value: str) -> Optional[str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _modified(meta: Dict[str, str], path: Path) -> Tuple[str, str]:
    stamp = _to_utc_iso(meta["modified"]) if meta.get("modified") else None
    if stamp:
        return stamp, "frontmatter"
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime.isoformat(), "mtime"


def _read_note(path: Path) -> dict:
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    modified, source = _modified(meta, path)
    return {
        "key": path.stem,
        "name": meta.get("name") or path.stem,
        "description": meta.get("description", ""),
        "body_refs": find_refs(body),
        "modified": modified,
        "modified_source": source,
    }


def read_memory_notes(binding: dict) -> Tuple[List[dict], str]:
    """Notes of the bound memory dir (``*.md`` one level deep, index excluded).

    A dir outside ``~/.claude/projects/`` (also via a symlink) is ``error``; a
    note that resolves outside it is not followed but makes the read ``error``.
    """
    try:
        root = Path(binding["memory_dir"]).expanduser().resolve()
    except (KeyError, TypeError, OSError, RuntimeError, ValueError):
        return [], f"{STATE_ERROR}: memory_dir missing or unresolvable"
    if _memory_dir_problem(str(root)):
        return [], f"{STATE_ERROR}: memory_dir outside the allowed tree"
    if not root.is_dir():
        return [], f"{STATE_ERROR}: memory_dir is not a directory"
    notes: List[dict] = []
    try:
        # iterdir propagates a directory-read error; glob swallows it as "empty".
        for path in sorted(root.iterdir()):
            if path.suffix != ".md" or path.name == "MEMORY.md":
                continue
            if path.is_symlink():
                try:  # judged before the non-file filter; a loop raises RuntimeError on 3.12
                    target = path.resolve(strict=False)
                except RuntimeError:
                    return [], f"{STATE_ERROR}: note {path.name} is a symlink loop"
                if not _under(target, root):
                    return [], f"{STATE_ERROR}: note {path.name} resolves outside memory_dir"
                if not target.is_file():
                    return [], f"{STATE_ERROR}: note {path.name} is a dangling or non-file symlink"
            elif not path.is_file():
                continue
            notes.append(_read_note(path))
    except (OSError, UnicodeDecodeError):
        return [], f"{STATE_ERROR}: a memory note cannot be read"
    return notes, STATE_OK


# --- open items ------------------------------------------------------------

def read_open_items(state_dir: Path) -> Tuple[List[dict], str, int]:
    """Open items of this project's store: ``(items, state, foreign_project_skipped)``.

    ``read_items`` returns an empty store for a missing file, so existence is
    checked first: no file is ``absent``, never an empty ``ok`` list. Items
    carry no project id by default (bound by the store location); an item that
    does carry another one is skipped and counted.
    """
    from open_items_manager import read_items

    project_id = project_id_from_state_dir(Path(state_dir))
    if not project_id:
        return [], f"{STATE_UNBOUND}: project id cannot be derived from the state dir", 0
    if not (Path(state_dir) / "open_items.json").is_file():
        return [], STATE_ABSENT, 0
    try:
        raw = read_items(Path(state_dir)).get("items")
    except (OSError, ValueError, AttributeError):
        return [], f"{STATE_ERROR}: open_items.json unreadable or corrupt", 0
    if not isinstance(raw, list) or not all(isinstance(i, dict) for i in raw):
        return [], f"{STATE_ERROR}: open_items.json has no valid items list", 0
    items: List[dict] = []
    foreign = 0
    for item in raw:
        if item.get("project_id") not in (None, "", project_id):
            foreign += 1
            continue
        items.append({
            "id": item.get("id"),
            "status": item.get("status"),
            "created_at": item.get("created_at"),
            "title_tokens_input": item.get("title") or "",
            "locator": _locator(project_id, "open_item", str(item.get("id"))),
        })
    return items, STATE_OK, foreign
