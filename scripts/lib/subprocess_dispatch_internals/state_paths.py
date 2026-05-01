"""state_paths — VNX state-directory and dispatch-file path resolvers."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    """Resolve project root: scripts/lib/subprocess_dispatch_internals/ -> ../../../."""
    return Path(__file__).resolve().parents[3]


def _default_state_dir() -> Path:
    """Resolve VNX state directory from environment."""
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "state"
    return _project_root() / ".vnx-data" / "state"


def _resolve_active_dispatch_file(dispatch_id: str) -> Path | None:
    """Locate the dispatch file in dispatches/active/ for cleanup_worker_exit.

    Returns None when no matching file exists (e.g. file already moved by
    another path).  Used by the deliver_with_recovery exit hooks.
    """
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    base = (
        Path(data_dir) / "dispatches" / "active"
        if data_dir
        else _project_root() / ".vnx-data" / "dispatches" / "active"
    )
    if not base.is_dir():
        return None
    for path in base.iterdir():
        if path.is_file() and dispatch_id in path.name:
            return path
    return None


def _dispatch_manifest_dir(stage: str, dispatch_id: str) -> Path:
    """Resolve .vnx-data/dispatches/<stage>/<dispatch_id>/ for manifest storage."""
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "dispatches" / stage / dispatch_id
    return _project_root() / ".vnx-data" / "dispatches" / stage / dispatch_id


def _safe_remove_active_dir(src_dir: Path) -> bool:
    """Remove ``src_dir`` recursively iff it lives under ``dispatches/active/``.

    Safety contract (CFX-7):
      * The directory itself must not be a symlink — refuse to follow.
      * ``src_dir.parent.name`` must be ``"active"`` and the grandparent
        must be named ``"dispatches"``.  This anchors the removal to the
        intended layout and rejects any caller-supplied path that escapes
        the active bucket.
      * Missing directory is a successful no-op (idempotent).

    Returns True when the directory was removed (or was already gone),
    False when removal was refused or failed.  Never raises.
    """
    try:
        if src_dir.is_symlink():
            logger.warning(
                "_safe_remove_active_dir: refusing symlinked path %s", src_dir
            )
            return False
        if not src_dir.exists():
            return True
        if not src_dir.is_dir():
            logger.warning(
                "_safe_remove_active_dir: refusing non-directory %s", src_dir
            )
            return False
        parent = src_dir.parent
        grandparent = parent.parent
        if parent.name != "active" or grandparent.name != "dispatches":
            logger.warning(
                "_safe_remove_active_dir: refusing %s — not under dispatches/active/",
                src_dir,
            )
            return False
        shutil.rmtree(src_dir)
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        logger.warning("_safe_remove_active_dir failed for %s: %s", src_dir, exc)
        return False
