"""Operator-steering source: binding and per-file containment (ADR-072, OB-1a).

Nothing is read before ``state/steering_sources.json`` binds the project; every file is
re-checked on the path of its open file descriptor. Nothing read is logged or written.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO, Tuple

from atomic_io import atomic_write_json
from vnx_paths import project_id_from_state_dir

SCHEMA = "steering_sources/1"
BINDING_FILE = "steering_sources.json"
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
FORBIDDEN_SLUG_PARTS = ("clients", "business", "salesminds")


class BindingError(ValueError):
    """bind refused: invalid input (exit 2)."""


class Refused(Exception):
    """A file failed containment; ``args[0]`` is the count key."""


@dataclass
class SourceResult:
    state: str  # ok | absent | unbound | error
    reason: str = ""
    refused: Dict[str, int] = field(default_factory=dict)
    refused_files: List[str] = field(default_factory=list)


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _slug_error(slug: object, root: Path) -> str:
    """Why ``slug`` may not be a bound transcript dir, or "" when it may."""
    if not isinstance(slug, str) or not slug or "/" in slug or slug in (".", ".."):
        return f"slug ongeldig: {slug!r}"
    if any(p in slug.lower() for p in FORBIDDEN_SLUG_PARTS):
        return f"klant- of businessmap geweigerd: {slug!r}"
    if (root / slug).resolve() != root / slug:
        return f"slug ligt niet direct onder {root}: {slug!r}"
    return ""


def _memory_ok(memory: Path, root: Path) -> bool:
    return _under(memory, root) and not any(p in str(memory).lower() for p in FORBIDDEN_SLUG_PARTS)


def bind(state_dir: Path, projects_root: Path, slugs: List[str], memory_dir: Path) -> dict:
    project_id = project_id_from_state_dir(state_dir)
    if not project_id:
        raise BindingError("project_id niet af te leiden uit de state-map")
    root = projects_root.resolve()
    memory = Path(memory_dir).resolve()
    errors = [e for e in (_slug_error(s, root) for s in slugs) if e]
    if errors or not slugs or not _memory_ok(memory, root):
        raise BindingError(errors[0] if errors else "geen slug of memory-map buiten de projectenmap")
    record = {"schema": SCHEMA, "project_id": project_id, "transcript_dirs": list(slugs),
              "memory_dir": str(memory), "bound_at": datetime.now(timezone.utc).isoformat()}
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_dir / BINDING_FILE, record)
    return record


def load_binding(state_dir: Path, projects_root: Path) -> Tuple[Optional[dict], str]:
    """Return (binding, "") or (None, reason). Anything not positively valid is unbound."""
    try:
        record = json.loads((state_dir / BINDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "geen geldig bindingsbestand"
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return None, "onbekend schema"
    project_id = project_id_from_state_dir(state_dir)
    if not project_id or record.get("project_id") != project_id:
        return None, "project_id komt niet overeen"
    slugs, memory, root = record.get("transcript_dirs"), record.get("memory_dir"), projects_root.resolve()
    if not isinstance(slugs, list) or not slugs:
        return None, "transcript_dirs ongeldig"
    errors = [e for e in (_slug_error(s, root) for s in slugs) if e]
    if errors:
        return None, errors[0]
    if not isinstance(memory, str) or not _memory_ok(Path(memory).resolve(), root):
        return None, "memory_dir ongeldig"
    return record, ""


def _fd_path(fd: int) -> str:
    """Real path of an open descriptor; raises Refused when it cannot be established."""
    try:
        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0])
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        raise Refused("fd_path_unverifiable")


def open_contained(path: Path, root: Path):
    """Open ``path`` for reading only if it resolves, before and after opening, under ``root``.

    ``root`` must be the canonical path ``read_sources`` validated. It is never resolved again here:
    a directory swapped for a symlink after that check must fail both comparisons, not become the boundary.
    """
    if not _under(Path(os.path.realpath(path)), root):
        raise Refused("outside_bound_dir")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Refused("open_failed")
    try:
        if not _under(Path(os.path.realpath(_fd_path(fd))), root):
            raise Refused("outside_bound_dir")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("not_regular_file")
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def read_sources(state_dir: Path, projects_root: Path, consume: Callable[[TextIO, str], None]) -> SourceResult:
    """Hand every contained transcript file to ``consume(handle, filename)``; nothing without a binding.

    A containment refusal makes the whole source `error` (ADR-072 besluit 2).
    """
    binding, why = load_binding(state_dir, projects_root)
    if binding is None:
        return SourceResult("unbound", why)
    root = projects_root.resolve()
    refused: Counter = Counter()
    refused_files: List[str] = []
    present = 0
    for slug in binding["transcript_dirs"]:
        directory = root / slug
        if not directory.is_dir():
            continue
        present += 1
        if directory.resolve() != directory:
            refused["outside_bound_dir"] += 1
            refused_files.append(slug)
            continue
        for path in sorted(directory.glob("*.jsonl")):
            try:
                with open_contained(path, directory) as handle:
                    consume(handle, path.name)
            except Refused as exc:
                refused[exc.args[0]] += 1
                refused_files.append(path.name)
    if refused:
        return SourceResult("error", "containment geweigerd", dict(refused), refused_files)
    if not present:
        return SourceResult("absent", "transcript-map ontbreekt")
    return SourceResult("ok")
