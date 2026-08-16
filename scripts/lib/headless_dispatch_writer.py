#!/usr/bin/env python3
"""Headless dispatch writer for T0 orchestrator.

Provides write_dispatch() and generate_dispatch_id() for headless T0 to
create dispatch files in .vnx-data/dispatches/pending/ without manual
operator intervention.

Known consumers of this module:
  - scripts/lib/decision_executor.py  — imports write_dispatch + generate_dispatch_id
  - scripts/commands/dispatch.sh      — imports generate_dispatch_id via python3 -c
  - scripts/commands/dispatch-agent.sh — imports generate_dispatch_id via python3 -c

Design invariants:
  - Only T1/T2/T3 may receive dispatches (T0 cannot dispatch to itself).
  - Role validation checks .claude/skills/<role>/ directory existence.
  - dispatch.json is written atomically to pending/<dispatch_id>/dispatch.json.
  - created_at is always a UTC ISO-8601 timestamp.
  - The per-dispatch worktree is resolved via dispatch_worktree_isolation
    BEFORE writing: the worker branches inside that worktree, never the main
    checkout, and the resolution is fail-closed (no main-checkout fallback).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure lib dir (this file's directory) is on sys.path for project_scope import
_lib_dir = str(Path(__file__).resolve().parent)
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

from project_scope import current_project_id as _current_project_id  # noqa: E402

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _dispatch_dir() -> Path:
    """Resolve VNX_DISPATCH_DIR, populating os.environ defaults via ensure_env."""
    # Import here to avoid circular deps; ensure_env sets VNX_DISPATCH_DIR
    lib_dir = Path(__file__).parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    try:
        from vnx_paths import ensure_env  # type: ignore
        env = ensure_env()
        return Path(env["VNX_DISPATCH_DIR"])
    except Exception:
        # Fallback: walk up from this file to find .vnx-data
        candidate = Path(__file__).resolve()
        for _ in range(6):
            candidate = candidate.parent
            vnx_data = candidate / ".vnx-data"
            if vnx_data.is_dir():
                return vnx_data / "dispatches"
        raise RuntimeError(
            "Cannot resolve VNX_DISPATCH_DIR. "
            "Set the environment variable or ensure .vnx-data/ exists in the project tree."
        )


def _skills_dir() -> Path:
    """Resolve the project's skills directory via canonical vnx_paths.

    A raw __file__ upward walk for ``.claude/skills`` resolves relative to the
    KEYSTONE (not the project root) in a central install. See #1023.
    """
    lib_dir = Path(__file__).resolve().parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        from vnx_paths import ensure_env
        skills = Path(ensure_env()["VNX_SKILLS_DIR"])
        if skills.is_dir():
            return skills
    except Exception:
        logging.getLogger(__name__).debug(
            "vnx_paths canonical resolver unavailable; using __file__ last-resort path fallback",
            exc_info=True,
        )
    # Last-resort fallback: walk up from this file looking for .claude/skills.
    candidate = Path(__file__).resolve()
    for _ in range(6):
        candidate = candidate.parent
        skills = candidate / ".claude" / "skills"
        if skills.is_dir():
            return skills
    raise RuntimeError("Cannot locate .claude/skills/ directory.")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_TERMINALS = {"T1", "T2", "T3"}
VALID_TRACKS = {"A", "B", "C"}


def _get_project_id() -> str:
    """Return project ID — delegates to project_scope.current_project_id (OI-1342)."""
    return _current_project_id()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DispatchValidationError(ValueError):
    """Raised when dispatch parameters fail validation."""


class DispatchWorktreeError(RuntimeError):
    """Raised when the dispatch worktree cannot be resolved (fail-closed).

    The headless worker must operate inside the per-dispatch worktree the door
    created, never the main checkout.  When that worktree cannot be resolved —
    unresolvable project root, unresolvable base_ref, occupancy conflict, or
    identity mismatch — the writer refuses outright.  There is no fallback to
    the main checkout; a silent fallback is the exact defect this exists to
    remove.
    """


# ---------------------------------------------------------------------------
# generate_dispatch_id
# ---------------------------------------------------------------------------

def generate_dispatch_id(prefix: str, track: str) -> str:
    """Generate a dispatch ID like 20260411-083000-<prefix>-<track>.

    Args:
        prefix: Short label describing the dispatch purpose (e.g. "fix-adapter").
        track:  Track letter (A, B, or C).

    Returns:
        Dispatch ID string.
    """
    now = datetime.now(tz=timezone.utc)
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    return f"{date_part}-{time_part}-{prefix}-{track}"


# ---------------------------------------------------------------------------
# dispatch worktree resolution
# ---------------------------------------------------------------------------

def _resolve_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
) -> "tuple[Path, str]":
    """Resolve/create the per-dispatch worktree and return ``(path, branch)``.

    Uses the single canonical mechanism — ``dispatch_worktree_isolation`` —
    already shared by the tmux and provider lanes, so the headless writer does
    not build a second worktree mechanism beside it.  ``create_dispatch_worktree``
    creates or re-enters the worktree at
    ``<project_root>/.vnx-data/worktrees/dispatch-<safe_id>`` on branch
    ``dispatch/<safe_id>`` and acquires the OI-1232 occupancy lock; the identity
    is then verified so a wrong-identity hand-out fails loud instead of running
    on the wrong tree.

    Fail-closed: any failure raises :class:`DispatchWorktreeError`.  The writer
    refuses and does NOT fall back to the main checkout.
    """
    from dispatch_worktree_isolation import (  # noqa: PLC0415
        _sanitize_dispatch_id,
        create_dispatch_worktree,
        resolve_consumer_project_root,
        verify_worktree_identity,
    )
    root = Path(project_root) if project_root is not None else resolve_consumer_project_root()
    try:
        wt_path = create_dispatch_worktree(dispatch_id, project_root=root)
        claim = verify_worktree_identity(dispatch_id, wt_path, project_root=root)
    except Exception as exc:
        raise DispatchWorktreeError(
            f"cannot resolve the dispatch worktree for {dispatch_id!r}: {exc}"
        ) from exc
    branch = claim.get("branch") or f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"
    return Path(wt_path), branch


# ---------------------------------------------------------------------------
# write_dispatch
# ---------------------------------------------------------------------------

def write_dispatch(
    dispatch_id: str,
    terminal: str,
    track: str,
    role: str,
    instruction: str,
    *,
    gate: str = "gate_fix",
    cognition: str = "normal",
    priority: str = "P1",
    pr_id: Optional[str] = None,
    parent_dispatch: Optional[str] = None,
    feature: Optional[str] = None,
    branch: Optional[str] = None,
    context_files: Optional[list[str]] = None,
) -> Path:
    """Write a dispatch.json to pending/<dispatch_id>/dispatch.json.

    Args:
        dispatch_id:     Unique identifier for this dispatch.
        terminal:        Target terminal: T1, T2, or T3 (not T0).
        track:           Track letter: A, B, or C.
        role:            Agent role/skill name (e.g. backend-developer).
        instruction:     Full instruction text for the worker agent.
        gate:            Gate label (default: gate_fix).
        cognition:       Cognition level hint for the worker (default: normal).
        priority:        Priority label: P0, P1, P2 (default: P1).
        pr_id:           Optional PR identifier (e.g. PR-1).
        parent_dispatch: Optional parent dispatch ID for chaining.
        feature:         Optional feature tag (e.g. F42).
        branch:          Optional git branch the worker should operate on.
                         Defaults to the dispatch worktree's branch
                         (``dispatch/<safe_id>``) when not provided.
        context_files:   Optional list of file paths to include as context.

    Returns:
        Path to the created dispatch.json file.

    Raises:
        DispatchValidationError: If terminal, track, or role validation fails.
        DispatchWorktreeError:   If the dispatch worktree cannot be resolved;
                                 the dispatch is refused (no main-checkout
                                 fallback).
    """
    # --- validate terminal ---
    if terminal not in VALID_TERMINALS:
        raise DispatchValidationError(
            f"Invalid terminal {terminal!r}. Must be one of {sorted(VALID_TERMINALS)}. "
            "T0 cannot dispatch to itself."
        )

    # --- validate track ---
    if track not in VALID_TRACKS:
        raise DispatchValidationError(
            f"Invalid track {track!r}. Must be one of {sorted(VALID_TRACKS)}."
        )

    # --- validate role against skills directory ---
    try:
        skills_root = _skills_dir()
        role_skill_dir = skills_root / role
        if not role_skill_dir.is_dir():
            raise DispatchValidationError(
                f"Unknown role {role!r}. No skill directory found at {role_skill_dir}. "
                f"Available skills: {sorted(p.name for p in skills_root.iterdir() if p.is_dir())}"
            )
    except RuntimeError:
        # Skills dir not found — skip role validation rather than hard-fail
        pass

    # --- resolve/create the dispatch worktree (fail-closed) ---
    # The worker must operate inside this worktree, not the main checkout.
    # A failure here refuses the dispatch outright — no fallback to the main
    # checkout, which is the exact behavior this exists to remove.
    worktree_path, resolved_branch = _resolve_dispatch_worktree(dispatch_id)

    # --- resolve dispatch directory ---
    dispatch_base = _dispatch_dir()
    pending_dir = dispatch_base / "pending" / dispatch_id
    pending_dir.mkdir(parents=True, exist_ok=True)

    # --- build payload ---
    payload: dict = {
        "dispatch_id": dispatch_id,
        "terminal": terminal,
        "track": track,
        "role": role,
        "skill_name": role,
        "gate": gate,
        "cognition": cognition,
        "priority": priority,
        "pr_id": pr_id,
        "parent_dispatch": parent_dispatch,
        "feature": feature,
        "branch": branch or resolved_branch,
        "worktree_path": str(worktree_path),
        "instruction": instruction,
        "context_files": context_files or [],
        "created_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_id": _get_project_id(),
    }

    # --- write atomically ---
    dispatch_path = pending_dir / "dispatch.json"
    tmp_path = dispatch_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(dispatch_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return dispatch_path
