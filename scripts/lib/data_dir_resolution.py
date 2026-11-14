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
with ``vnx_paths.resolve_paths`` — the rule receipts use, so events and
receipts share one root (OI-1276) — and then FAILS LOUD — raising ``DataDirResolutionError`` naming the
resolution chain — if the result lands under the version store.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem resolution only.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings
from pathlib import Path

logger = logging.getLogger(__name__)

# Bare VNX_DATA_DIR values already named on stderr in this process (OI-1276).
_WARNED_BARE_VALUES: "set[str]" = set()


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
            "there is forbidden. Set VNX_DATA_DIR (with VNX_DATA_DIR_EXPLICIT=1) "
            "to a writable project store, "
            "or ensure VNX_PROJECT_ID / a .vnx-project-id marker resolves the "
            "project store (~/.vnx-data/<project_id>)."
        )
    return resolved


def _warn_bare_vnx_data_dir(value: str) -> None:
    """Name an ignored bare ``VNX_DATA_DIR`` on stderr, once per value per process."""
    if value in _WARNED_BARE_VALUES:
        return
    _WARNED_BARE_VALUES.add(value)
    print(
        f"[vnx] VNX_DATA_DIR={value} is ignored: VNX_DATA_DIR_EXPLICIT=1 is "
        "required for it to be honored, so events and receipts use the "
        "resolved state root (issue #225, OI-1276).",
        file=sys.stderr,
    )


def resolve_data_dir_fail_loud() -> Path:
    """Resolve the VNX data dir with the receipts' rule, failing loud on the version store.

    OI-1276: the data dir is ``vnx_paths.resolve_paths()["VNX_DATA_DIR"]`` —
    the exact answer receipts get — so events and receipts can never split
    across two roots. ``VNX_DATA_DIR`` is honored only together with
    ``VNX_DATA_DIR_EXPLICIT=1`` (the #225 policy); a bare one is ignored and
    named on stderr, never silently. ``VNX_PROJECT_ID`` and ``VNX_DATA_HOME``
    act through the same resolver.

    OI-1179 guarantee kept: a result under the read-only version store
    ``~/.vnx-system/versions/`` raises ``DataDirResolutionError`` naming the
    chain, whichever step produced it.
    """
    vnx_data = os.environ.get("VNX_DATA_DIR", "")
    explicit = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    if vnx_data and explicit:
        chain = f"VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR={vnx_data!r}"
    else:
        chain = (
            "no explicit VNX_DATA_DIR -> canonical "
            "vnx_paths.resolve_paths()['VNX_DATA_DIR']"
        )

    if vnx_data and explicit:
        # Same answer resolve_paths() gives in this case (vnx_paths
        # _resolve_state_root branch 1: expanduser().resolve()), without the
        # git call resolve_paths makes to derive a project id.
        return refuse_version_store(Path(vnx_data).expanduser().resolve(), chain)

    from vnx_paths import resolve_paths

    with warnings.catch_warnings():
        # A bare VNX_DATA_DIR is named on stderr below when it is really
        # ignored; resolve_paths' DeprecationWarning would only duplicate that.
        warnings.simplefilter("ignore", DeprecationWarning)
        resolved = Path(resolve_paths()["VNX_DATA_DIR"])
    # bin/vnx and vnx_paths.sh export the resolved root as a bare VNX_DATA_DIR;
    # only a bare value that differs from the answer is actually ignored.
    if vnx_data and not explicit and Path(vnx_data).expanduser().resolve() != resolved.resolve():
        _warn_bare_vnx_data_dir(vnx_data)
    return refuse_version_store(resolved, chain)
