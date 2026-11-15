"""ADR-078: the operator-owned binding of the memory ledger.

``bind`` is the only writer of ``state/memory_sources.json``. The scan that reads
the bound folders and appends ``memory_ledger/1`` lines follows in M2b. No slug
is ever derived from a path.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import atomic_io
from project_root import resolve_central_data_dir

SOURCES_FILE, SOURCES_SCHEMA = "memory_sources.json", "memory_sources/1"
REGISTRY_FILE, REGISTRY_SCHEMA = "memory_bindings.json", "memory_bindings/1"
PERSISTED = ("appended", "duplicate")  # the AppendResult statuses that mean the receipt is on disk
INDEX_NOTE = "MEMORY.md"


class BindRefused(Exception):
    """``vnx memory bind`` refuses; the message is the reason."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def projects_root() -> Path:
    return (Path.home() / ".claude" / "projects").resolve()


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def source_id(directory: str) -> str:
    return hashlib.sha256(str(Path(directory).resolve()).encode("utf-8")).hexdigest()[:16]


# --- binding ---------------------------------------------------------------

def load_sources(state_dir: Path) -> Tuple[Optional[dict], str]:
    """``(binding, "ok")`` or ``(None, "unbound: <reason>")``. Fail-closed."""
    path = Path(state_dir) / SOURCES_FILE
    if not path.is_file():
        return None, "unbound: binding file missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unbound: binding file unreadable or corrupt"
    dirs = data.get("memory_dirs") if isinstance(data, dict) else None
    if (data.get("schema") != SOURCES_SCHEMA if isinstance(data, dict) else True) \
            or not isinstance(data.get("project_id"), str) or not data["project_id"].strip() \
            or not isinstance(dirs, list) or not dirs \
            or not all(isinstance(d, str) and d for d in dirs):
        return None, "unbound: binding invalid"
    return data, "ok"


def _encodings(root: Path) -> Tuple[str, str]:
    text = str(root)
    return text.replace("/", "-"), re.sub(r"[/._]", "-", text)


def _boundary_refusal(resolved: Path, project_id: str, boundary: Any) -> Optional[str]:
    if boundary is None or not getattr(boundary, "configured", False):
        return None
    if project_id in boundary.client_project_ids:
        return None
    try:
        relative = resolved.relative_to(projects_root())
    except ValueError:
        return "folder is outside ~/.claude/projects/"
    if not relative.parts:
        return "folder is the projects root"
    project_dir = relative.parts[0]  # the encoded project the folder belongs to, at any depth
    for root in tuple(boundary.client_roots) + tuple(boundary.personal_roots):
        for enc in _encodings(root):
            if project_dir == enc or project_dir.startswith(enc + "-"):
                return "folder lies under a client or personal root"
    return None


def registry_path() -> Path:
    return resolve_central_data_dir(REGISTRY_FILE)  # ~/.vnx-data/memory_bindings.json


def _load_registry() -> dict:
    """The folder -> project_id map every bind records, read fail-closed."""
    path = registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BindRefused("binding registry unreadable or corrupt")
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, dict) or data.get("schema") != REGISTRY_SCHEMA:
        raise BindRefused("binding registry invalid")
    return bindings


def _other_project_binds(state_dir: Path, resolved: Path, project_id: str, registry: dict) -> bool:
    if registry.get(str(resolved), project_id) != project_id:
        return True
    stores = set(registry_path().parent.glob("*/state/" + SOURCES_FILE))  # every central store
    stores.add(Path(state_dir) / SOURCES_FILE)  # the caller's own store, repo-local or central
    for other in sorted(stores):
        data, _ = load_sources(other.parent)
        if data and data["project_id"] != project_id and str(resolved) in data["memory_dirs"]:
            return True
    return False


def bind(state_dir: Path, directory: str, project_id: str, *, boundary: Any = None,
         emit: Optional[Callable[..., Any]] = None) -> dict:
    """Add one folder to the binding (ADR-078 §4). Raises BindRefused.

    The state_mutation receipt is persisted before the binding is written
    (ADR-005); the uniqueness check and both writes share one registry lock.
    """
    if emit is None:
        raise BindRefused("no state_mutation emitter; a binding needs a receipt (ADR-005)")
    try:
        resolved = Path(directory).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise BindRefused("path cannot be resolved")
    if not resolved.is_dir() or resolved == projects_root() or not _under(resolved, projects_root()):
        raise BindRefused("not an existing folder under ~/.claude/projects/")
    if not any(p.is_file() and p.name != INDEX_NOTE for p in resolved.glob("*.md")):
        raise BindRefused("folder holds no note besides MEMORY.md")
    reason = _boundary_refusal(resolved, project_id, boundary)
    if reason:
        raise BindRefused(reason)
    target, registry_file = Path(state_dir) / SOURCES_FILE, registry_path()
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    with atomic_io.slot_lock(registry_file):
        registry = _load_registry()
        if _other_project_binds(state_dir, resolved, project_id, registry):
            raise BindRefused("folder is already bound to another project")
        with atomic_io.slot_lock(target):
            old, _ = load_sources(state_dir)
            old_dirs = list(old["memory_dirs"]) if old and old["project_id"] == project_id else []
            new_dirs = old_dirs + ([str(resolved)] if str(resolved) not in old_dirs else [])
            doc = {"schema": SOURCES_SCHEMA, "project_id": project_id, "memory_dirs": new_dirs,
                   "bound_at": _now()}
            try:
                result = emit(str(target), trigger="memory_bind old=%s new=%s"
                              % (json.dumps(old_dirs), json.dumps(new_dirs)))
            except Exception as exc:  # any emitter failure is a missing receipt: refuse, write nothing
                raise BindRefused("state_mutation receipt not persisted; binding unchanged") from exc
            if getattr(result, "status", None) not in PERSISTED:
                raise BindRefused("state_mutation receipt not persisted; binding unchanged")
            atomic_io.atomic_write_json(target, doc)
        registry[str(resolved)] = project_id
        atomic_io.atomic_write_json(registry_file, {"schema": REGISTRY_SCHEMA, "bindings": registry})
    return doc
