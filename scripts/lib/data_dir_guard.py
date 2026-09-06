#!/usr/bin/env python3
"""VNX data-dir / project_id consistency guard (ADR-028 Phase-0).

Advisory by default: warns when the resolved VNX_DATA_DIR does not belong to
the project identified by ``.vnx-project-id`` / ``VNX_PROJECT_ID``.  When
``VNX_DATA_DIR_GUARD=enforce`` the same check aborts startup.

This is intentionally separate from path resolution so it can be imported and
called at the natural startup point (``vnx_paths.resolve_paths()``) without
duplicating path logic.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Optional

from project_root import resolve_central_data_dir, resolve_project_id
from project_root import resolve_data_dir as _resolve_repo_local_data_dir
from vnx_ids import PROJECT_ID_RE


class VNXDataDirMismatchWarning(UserWarning):
    """Emitted when the resolved data-dir does not match the resolved project_id."""


def _guard_mode() -> str:
    raw = os.environ.get("VNX_DATA_DIR_GUARD", "warn").lower().strip()
    if raw in ("off", "warn", "enforce"):
        return raw
    return "warn"


def _expected_central_dir(project_id: str) -> Path:
    return resolve_central_data_dir(project_id).resolve()


def _is_under_central_dir(resolved: Path, expected: Path) -> bool:
    if resolved == expected:
        return True
    expected_prefix = str(expected) + os.sep
    return str(resolved).startswith(expected_prefix)


def check_data_dir_project_id_guard(
    data_dir: str | Path,
    project_id: Optional[str] = None,
) -> None:
    """Check that ``data_dir`` belongs to ``project_id``'s central data directory.

    Behavior is controlled by ``VNX_DATA_DIR_GUARD``:
      * ``off``    — no check.
      * ``warn``   — emit a ``VNXDataDirMismatchWarning`` on mismatch (default).
      * ``enforce`` — raise ``RuntimeError`` on mismatch.

    If ``project_id`` is not supplied it is resolved from the environment / marker
    file via ``project_root.resolve_project_id()``.  A missing or invalid
    project_id is treated as "cannot verify" and the guard skips silently rather
    than fabricating a false mismatch.

    Args:
        data_dir: The resolved VNX_DATA_DIR to check.
        project_id: Optional explicit project_id.  When omitted the ambient
                    project_id is resolved.

    Raises:
        RuntimeError: In ``enforce`` mode when the data-dir is not under the
                      expected central directory for the resolved project_id.
    """
    mode = _guard_mode()
    if mode == "off":
        return

    resolved = Path(data_dir).expanduser().resolve()

    pid = project_id
    if pid is None:
        try:
            pid = resolve_project_id()
        except RuntimeError:
            return
    pid = pid.strip()
    if not pid or not PROJECT_ID_RE.match(pid):
        return

    expected = _expected_central_dir(pid)
    if _is_under_central_dir(resolved, expected):
        return

    msg = (
        f"VNX data-dir mismatch: resolved data-dir {resolved} does not belong to "
        f"project {pid!r} (expected central dir {expected}). "
        "Running with the wrong data directory can write receipts/state into "
        "another project's store or a stray repo-local tree. "
        "Fix: ensure VNX_PROJECT_ID matches the intended project, add a "
        ".vnx-project-id marker in the project root, or stop pinning a "
        "repo-local VNX_DATA_DIR / VNX_STATE_DIR. "
        "Set VNX_DATA_DIR_GUARD=enforce to abort on this condition, "
        "or =off to silence this warning."
    )

    if mode == "enforce":
        raise RuntimeError(msg)

    warnings.warn(VNXDataDirMismatchWarning(msg), stacklevel=2)


def _candidate_report_stores(data_dir: "str | Path") -> "list[Path]":
    """Return the OTHER known VNX data-dir candidates besides *data_dir* itself.

    Two resolvers exist fleet-wide for "the" data dir and can legitimately
    disagree (OI-1635): ``project_root.resolve_data_dir()`` (repo-local
    ``<project_root>/.vnx-data`` by default) and the central
    ``~/.vnx-data/<project_id>`` store (``resolve_central_data_dir`` /
    ``vnx_paths.resolve_paths()``). This returns whichever of those two do NOT
    match the resolved *data_dir*, so a caller can check both for a stray report.

    Best-effort and never raises: an unresolvable project_id/project_root drops
    that candidate silently — this is an advisory signal, not a path resolver
    that anything depends on for correctness.
    """
    resolved = Path(data_dir).expanduser().resolve()
    candidates: "list[Path]" = []

    try:
        local = _resolve_repo_local_data_dir().resolve()
        if local != resolved:
            candidates.append(local)
    except Exception:  # vnx-silent-except: advisory-only, never raise past this
        pass

    try:
        central = resolve_central_data_dir(resolve_project_id()).resolve()
        if central != resolved and central not in candidates:
            candidates.append(central)
    except Exception:  # vnx-silent-except: advisory-only, never raise past this
        pass

    return candidates


def check_report_store_split_brain(
    dispatch_id: str, data_dir: "str | Path"
) -> "Optional[Path]":
    """Detect a unified report for *dispatch_id* sitting in a DIFFERENT known
    store than *data_dir* (OI-1635).

    Returns the path of the orphaned report when one is found in another known
    store's ``unified_reports/`` directory, else ``None``. Callers should treat
    a hit as advisory-but-loud: it means the worker's real output likely landed
    somewhere the governance layer never reads from, and whatever is about to
    be written to *data_dir* is at risk of being a narrative stand-in for real
    work sitting elsewhere, unlinked from the audit trail.

    Best-effort — resolution errors are swallowed (never raises); this must
    never block or fail a report-emit call site.
    """
    resolved = Path(data_dir).expanduser().resolve()
    for candidate in _candidate_report_stores(resolved):
        try:
            alt_report = candidate / "unified_reports" / f"{dispatch_id}.md"
            if alt_report.is_file():
                return alt_report
        except OSError:  # vnx-silent-except: advisory-only, never raise past this
            continue
    return None
