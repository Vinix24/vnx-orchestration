"""Plan-first-gate enforcement (defense-in-depth, advisory-first).

The plan-first gate seeds a synthetic ``OI-PLAN-<track>`` blocker on a track so it
is "born plan-gated" (``planning_cli._seed_plan_blocker``). Until now that blocker
only gated CLOSURE bookkeeping — ``track_reconciler.close_track_if_done`` revalidates
it at close-time — but never the WORK: neither the dispatch door nor the merge gate
consulted it, so a track could be dispatched and its PR merged without the plan gate
ever passing (build-before-plan). Both enforcement points call the read-only check
here so the rule lives in exactly one place.

Flag ``VNX_PLAN_GATE_ENFORCE`` (off | advisory | required), default ``advisory``:
  - ``off``      : no check (an unknown value also fails safe to off).
  - ``advisory`` : check + surface a WARN, never block (default; mirrors the
                   evidence-bound-gate D3 rollout in ``evidence_bound_gate.py``).
  - ``required`` : block when the plan gate is unresolved.

Operator override ``VNX_OVERRIDE_PLAN_GATE=1`` forces a pass in ``required`` mode; the
caller records ``override_applied`` so the deviation stays in the audit trail (never
silent) — the same discipline as the ADR-027 signed gate-override.

This module is deliberately dependency-free (stdlib + sqlite only): the door
constructs a ``ConstraintVerdict`` from the state, the merge gate constructs its own
gate-shaped result, but both share this one truth.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PLAN_OI_PREFIX = "OI-PLAN-"

# plan-gate state discriminators
PASSED = "passed"            # no unresolved OI-PLAN blocker (gate passed, or never seeded)
UNRESOLVED = "unresolved"    # an OI-PLAN-<track> 'blocks' row with resolved_at IS NULL
UNSUPPORTED = "unsupported"  # schema predates the resolvable-blocker columns; cannot enforce

_ENFORCE_MODES = {"off", "advisory", "required"}
_TRUTHY = {"1", "true", "yes", "on"}


def plan_blocker_oi(track_id: str) -> str:
    """The synthetic open-item id for a track's plan-first gate."""
    return f"{PLAN_OI_PREFIX}{track_id}"


def enforce_mode() -> str:
    """Resolve ``VNX_PLAN_GATE_ENFORCE`` to off/advisory/required (default advisory).

    An unknown value fails safe to ``off`` — enforcement never turns on by accident.
    """
    raw = (os.environ.get("VNX_PLAN_GATE_ENFORCE") or "advisory").strip().lower()
    return raw if raw in _ENFORCE_MODES else "off"


def override_active() -> bool:
    """True when the operator set ``VNX_OVERRIDE_PLAN_GATE`` to an affirmative value."""
    return (os.environ.get("VNX_OVERRIDE_PLAN_GATE") or "").strip().lower() in _TRUTHY


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        is not None
    )


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def plan_gate_state(db_path: "str | Path", track_id: str, project_id: str) -> str:
    """Return ``PASSED`` / ``UNRESOLVED`` / ``UNSUPPORTED`` for a track's plan-first gate.

    Read-only URI connection: a missing DB file raises immediately rather than
    silently creating an empty one (callers degrade any exception to a WARN — never
    crash the door). ``UNSUPPORTED`` when the schema lacks ``track_open_items`` or its
    ``resolved_at`` column — the same predicate ``planning_cli._plan_gate_supported``
    guards SEED with, so a DB that could never CLEAR a blocker is never enforced against.

    Only the ``OI-PLAN-<track>`` blocker counts here; other unresolved ``blocks``
    open-items are the closure gate's concern, not the plan-first gate's.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        if not (_has_table(conn, "track_open_items") and _has_col(conn, "track_open_items", "resolved_at")):
            return UNSUPPORTED
        oi = plan_blocker_oi(track_id)
        if _has_col(conn, "track_open_items", "project_id"):
            row = conn.execute(
                "SELECT 1 FROM track_open_items "
                "WHERE track_id=? AND project_id=? AND oi_id=? "
                "AND link_type='blocks' AND resolved_at IS NULL LIMIT 1",
                (track_id, project_id, oi),
            ).fetchone()
        else:  # pre-0024 DB: no tenant column
            row = conn.execute(
                "SELECT 1 FROM track_open_items "
                "WHERE track_id=? AND oi_id=? AND link_type='blocks' "
                "AND resolved_at IS NULL LIMIT 1",
                (track_id, oi),
            ).fetchone()
        return UNRESOLVED if row is not None else PASSED
    finally:
        conn.close()
