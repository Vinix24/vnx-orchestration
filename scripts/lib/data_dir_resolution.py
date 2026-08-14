#!/usr/bin/env python3
"""Fail-loud VNX data-dir resolution shared by event_store and the headless
dispatch daemon (OI-1179 + OI-1172).

Two defects, one class: a data-dir resolver lands on the *read-only* pinned
version store ``~/.vnx-system/versions/<v>/.vnx-data`` and the caller then
tries to write there.

  * ``event_store._events_dir`` ignored a set ``VNX_DATA_DIR`` unless
    ``VNX_DATA_DIR_EXPLICIT=1`` was also set (the "two-flag trap"), then fell
    back to the canonical resolver which — in a central install — can resolve
    the keystone (see #1023).
  * ``headless_dispatch_daemon._default_data_dir`` fell back to a
    ``_repo_root() / ".vnx-data"`` walk when the canonical resolver returned
    None, which resolves the keystone.

The read-only version store is never a valid data dir. This module resolves
env-first (a set ``VNX_DATA_DIR`` is honored directly, no explicit flag
required) and then FAILS LOUD — raising ``DataDirResolutionError`` naming the
resolution chain — if the result lands under the version store.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem resolution only.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class DataDirResolutionError(RuntimeError):
    """Raised when the resolved data dir is a read-only pinned version store."""


def _version_store_roots() -> "list[Path]":
    """Roots that are never valid VNX data dirs.

    ``~/.vnx-system/versions/`` holds the read-only pinned version dirs
    (vX.Y.Z) that ``vnx_version_ro`` locks after install. ``current`` is the
    symlink shim: resolving through it lands on a version dir, which the
    ``versions`` root already covers once ``.resolve()`` follows the symlink —
    but keeping ``current`` here also catches a non-symlinked ``current``
    layout so no install shape slips through.
    """
    home = Path.home()
    return [
        home / ".vnx-system" / "versions",
        home / ".vnx-system" / "current",
    ]


def _is_under_version_store(resolved: Path) -> bool:
    sep = os.sep
    for root in _version_store_roots():
        try:
            r = root.resolve()
        except OSError:
            r = root.expanduser()
        if resolved == r or str(resolved).startswith(str(r) + sep):
            return True
    return False


def refuse_version_store(resolved: "Path | str", chain: str) -> Path:
    """Return ``resolved`` unchanged unless it is a read-only version store.

    Raises ``DataDirResolutionError`` — naming the resolution chain that
    produced the path — when ``resolved`` sits under
    ``~/.vnx-system/versions/`` (or resolves through ``~/.vnx-system/current``
    into it). A read-only version store is never a valid VNX data dir.
    """
    resolved = Path(resolved).expanduser().resolve()
    if _is_under_version_store(resolved):
        raise DataDirResolutionError(
            "resolved VNX data dir is a read-only version store: "
            f"{resolved} (resolution chain: {chain}). A pinned version store "
            "under ~/.vnx-system/versions/ is never a valid data dir — writing "
            "there is forbidden. Set VNX_DATA_DIR to a writable project store, "
            "or ensure VNX_PROJECT_ID / a .vnx-project-id marker resolves the "
            "project store (~/.vnx-data/<project_id>)."
        )
    return resolved


def resolve_data_dir_fail_loud() -> Path:
    """Resolve the VNX data dir env-first, failing loud on the version store.

    Resolution order (a set ``VNX_DATA_DIR`` is honored directly; the
    ``VNX_DATA_DIR_EXPLICIT`` flag is no longer required — OI-1179):
      1. ``VNX_DATA_DIR`` set           -> that dir (resolved).
      2. ``VNX_PROJECT_ID`` set         -> ``~/.vnx-data/<project_id>``.
      3. canonical resolver             -> ``vnx_paths.resolve_paths()["VNX_DATA_DIR"]``.

    Every step is refused when it lands on the read-only version store; the
    refusal names the chain that produced the path.
    """
    vnx_data = os.environ.get("VNX_DATA_DIR", "")
    if vnx_data:
        return refuse_version_store(
            vnx_data, f"VNX_DATA_DIR={vnx_data!r}"
        )

    project_id = os.environ.get("VNX_PROJECT_ID", "")
    if project_id:
        return refuse_version_store(
            Path.home() / ".vnx-data" / project_id,
            f"VNX_PROJECT_ID={project_id!r} -> ~/.vnx-data/<project_id>",
        )

    from vnx_paths import resolve_paths

    resolved = Path(resolve_paths()["VNX_DATA_DIR"])
    return refuse_version_store(
        resolved,
        "VNX_DATA_DIR unset, VNX_PROJECT_ID unset -> "
        "canonical vnx_paths.resolve_paths()['VNX_DATA_DIR']",
    )
