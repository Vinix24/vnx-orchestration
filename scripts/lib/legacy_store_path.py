"""legacy_store_path — find pre-#1437 XDG stores (read-only inventory).

Before PR #1437 (2026-10-08) ``vnx init`` created a project's runtime store
under ``${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>``. Since then the
store lives in the central ``~/.vnx-data/<project_id>`` directory, and no
resolver in ``vnx_paths`` reads the XDG location any more — so an operator who
upgraded still has an abandoned copy on disk with nothing pointing at it.

This module only *finds* those copies: it never moves, rewrites or deletes
anything, and no resolver imports it. ``vnx doctor`` renders the findings as
its ``store:legacy-xdg`` check; relocating a store is
``vnx migrate --relocate-legacy``.

A directory is a store only when it holds ``state/runtime_coordination.db``.
An empty skeleton (the tree was created, nothing was written, or the files
were removed by hand) is not a store: nothing to move, nothing to report.

Stdlib only, so the probe works on any install without touching the engine.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Directory name ``vnx init`` used below the XDG data home.
LEGACY_DIR_NAME = "vnx"

#: Relative path of the runtime coordination DB that makes a directory a store.
RUNTIME_DB_RELPATH = Path("state") / "runtime_coordination.db"

# VNX project ids are lowercase slugs (vnx_ids.PROJECT_ID_RE). Re-checked here
# instead of imported so this module stays stdlib-only; an id that does not
# match is refused rather than joined onto the data home (no ``../`` escape).
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def legacy_data_home() -> Path:
    """Return the pre-#1437 store root: ``$XDG_DATA_HOME/vnx`` or ``~/.local/share/vnx``."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / LEGACY_DIR_NAME
    return Path.home() / ".local" / "share" / LEGACY_DIR_NAME


def legacy_store_for(project_id: str) -> Optional[Path]:
    """Return the legacy store dir of ``project_id``, or ``None``.

    ``None`` when the id is unusable, the directory is absent, or it is only an
    empty skeleton (no ``state/runtime_coordination.db``).
    """
    if not project_id or not _PROJECT_ID_RE.match(project_id):
        return None
    candidate = legacy_data_home() / project_id
    if not (candidate / RUNTIME_DB_RELPATH).is_file():
        return None
    return candidate


def iter_legacy_stores() -> List[Dict[str, Any]]:
    """Inventory every child directory of :func:`legacy_data_home`.

    One dict per child directory, sorted by ``project_id``::

        {"project_id": str, "path": Path, "has_db": bool,
         "size_bytes": int, "mtime": float | None}

    ``size_bytes`` sums the regular files under the directory (symlinked trees
    are not followed) and is a lower bound when a subtree is unreadable — an
    inventory figure only; ``has_db`` is the fact that decides whether the
    directory is a store. ``mtime`` is ``None`` when the directory vanished
    between listing and stat.
    """
    home = legacy_data_home()
    try:
        children = sorted(home.iterdir(), key=lambda p: p.name)
    except OSError:
        # Absent/unreadable data home = empty inventory, which the caller
        # renders as "no legacy store". Not fail-open: no store is named and
        # nothing here clears or passes a gate.
        return []

    stores: List[Dict[str, Any]] = []
    for child in children:
        if not child.is_dir():
            continue
        try:
            mtime: Optional[float] = child.stat().st_mtime
        except OSError:
            mtime = None
        stores.append({
            "project_id": child.name,
            "path": child,
            "has_db": (child / RUNTIME_DB_RELPATH).is_file(),
            "size_bytes": _dir_size_bytes(child),
            "mtime": mtime,
        })
    return stores


def _dir_size_bytes(path: Path) -> int:
    """Sum regular-file sizes below ``path``; unreadable subtrees are skipped."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=_skip_unreadable):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                # File deleted or made unreadable mid-walk; the figure is a
                # display value, not evidence for any pass/fail decision.
                continue
    return total


def _skip_unreadable(_exc: OSError) -> None:
    """``os.walk`` error hook: an unreadable subtree only lowers the size estimate."""
