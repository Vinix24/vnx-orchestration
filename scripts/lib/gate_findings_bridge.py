#!/usr/bin/env python3
"""gate_findings_bridge.py — blocking gate verdicts -> first-class fabric open-items.

The gap this closes (proof-case: ppb #1039): a BLOCKING gate verdict (pre_merge_gate
HOLD, phantom_guard reject, quality_advisory severity="blocking") used to live only in
a gate-result file or a worker report. Nobody scanning `vnx horizon show <track>` or the
kickoff/human-gate surface would see it without reading the PR. This module makes a
blocking verdict a `track_open_items` row (link_type="blocks") the moment the gate fires,
and closes it the moment a later clean run on the SAME dispatch supersedes it.

Single writer (D1, STATE_FABRIC.md): every ``track_open_items`` mutation goes THROUGH
``tracks.link_open_item`` / ``tracks.unlink_open_item`` — this module owns no SQL of its
own against that table. It only reads ``dispatches`` (read-only) to resolve the track a
gate's dispatch_id points to, mirroring ``scripts/import_open_items_to_tracks.py``.

ADR-005 (D3 semantics, mirrored from the OI bridge): the DB mutation and its commit are
the authoritative act. The ``track_oi_linked``/``track_oi_closed`` ledger event is
deferred and emitted ONLY AFTER that commit succeeds — at-most-once, non-fatal on
failure (logged, never rolled back; the reconciler can re-derive from the committed row).

ADR-007 (tenant scoping): project_id is resolved FROM the dispatch's own row in
``dispatches`` — never defaulted to ``vnx-dev``. A dispatch with no row, no track_id, or
no project_id is, from this module's perspective, UNLINKED: it degrades quietly (a single
log line) and creates nothing. An unlinked gate finding is not a fabric open-item, and
that is an accepted, intentional no-op (per the dispatch contract), not an error.

Both entry points are best-effort and NEVER raise: a gate's own PASS/HOLD decision must
never be affected by a finding-emit failure (this module is observability bolted onto an
already-made decision, not part of the decision itself).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import tracks

_LOG = logging.getLogger(__name__)

DB_FILENAME = "runtime_coordination.db"

# The gate finding is idempotent by construction: the SAME (gate_name, dispatch_id) pair
# always derives the SAME oi_id, and tracks.link_open_item upserts on the
# (track_id, oi_id, link_type) primary key — a re-run never duplicates the row.
_LINK_TYPE = "blocks"
_LINK_SOURCE = "manual"


def _oi_id(gate_name: str, dispatch_id: str) -> str:
    return f"gate:{gate_name}:{dispatch_id}"


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(row[1] == col for row in conn.execute(f"PRAGMA table_info({table})"))


def _resolve_dispatch_track(
    state_dir: str | Path, dispatch_id: str
) -> Optional[tuple[str, str]]:
    """Resolve (track_id, project_id) for ``dispatch_id``, or None to degrade quietly.

    Fail-closed on tenancy (ADR-007): a dispatch row missing ``project_id`` (or
    ``track_id``) is treated as unresolvable, NEVER defaulted to 'vnx-dev'. Any DB
    error (locked/absent/malformed) also resolves to None — a gate must never crash
    because the fabric-linking read failed.
    """
    if not dispatch_id or not dispatch_id.strip():
        return None
    db_path = Path(state_dir) / DB_FILENAME
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error:
        return None
    try:
        if not _has_col(conn, "dispatches", "track_id") or not _has_col(
            conn, "dispatches", "project_id"
        ):
            return None
        row = conn.execute(
            "SELECT track_id, project_id FROM dispatches WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        return (row[0], row[1])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def record_gate_finding(
    state_dir: str | Path,
    *,
    dispatch_id: str,
    gate_name: str,
    summary: str,
    pr_ref: Optional[str] = None,
) -> bool:
    """Best-effort: upsert a ``blocks`` open-item for a BLOCKING gate verdict.

    No-op (returns False, logged) when the dispatch has no linked track — an
    unlinked gate finding is intentionally not a fabric open-item. Never raises.
    """
    resolved = _resolve_dispatch_track(state_dir, dispatch_id)
    if resolved is None:
        _LOG.info(
            "gate_findings_bridge: dispatch=%s gate=%s has no linked track — "
            "degrading quietly (no fabric open-item created)",
            dispatch_id, gate_name,
        )
        return False
    track_id, project_id = resolved
    oi_id = _oi_id(gate_name, dispatch_id)
    db_path = Path(state_dir) / DB_FILENAME
    events: list = []
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        tracks.link_open_item(
            state_dir, track_id, project_id, oi_id, _LINK_TYPE, _LINK_SOURCE,
            conn=conn, event_sink=events,
            details={"gate_name": gate_name, "dispatch_id": dispatch_id,
                     "summary": summary, "pr_ref": pr_ref},
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — never break the caller's gate decision
        if conn is not None:
            conn.rollback()
        _LOG.warning(
            "gate_findings_bridge: record failed dispatch=%s track=%s gate=%s: %s",
            dispatch_id, track_id, gate_name, exc,
        )
        return False
    finally:
        if conn is not None:
            conn.close()
    _emit_deferred(state_dir, events)
    _LOG.warning(
        "gate_findings_bridge: RECORDED blocking finding track=%s project=%s gate=%s "
        "dispatch=%s pr_ref=%s: %s",
        track_id, project_id, gate_name, dispatch_id, pr_ref, summary,
    )
    return True


def resolve_gate_finding(
    state_dir: str | Path,
    *,
    dispatch_id: str,
    gate_name: str,
    reason: str = "gate clean run",
) -> bool:
    """Best-effort: close a previously-recorded ``blocks`` finding on a clean gate run.

    No-op (returns False) when the dispatch is unlinked, migration 0030 is absent, or
    no ACTIVE finding is on record for (gate_name, dispatch_id) — closing nothing is
    the expected common case (most clean runs never had a finding to begin with).
    Never raises.
    """
    resolved = _resolve_dispatch_track(state_dir, dispatch_id)
    if resolved is None:
        return False
    track_id, project_id = resolved
    oi_id = _oi_id(gate_name, dispatch_id)
    db_path = Path(state_dir) / DB_FILENAME
    events: list = []
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        if not _has_col(conn, "track_open_items", "resolved_at"):
            return False  # pre-0030 store: nothing this module can resolve
        active = conn.execute(
            "SELECT 1 FROM track_open_items WHERE track_id = ? AND project_id = ? "
            "AND oi_id = ? AND link_type = ? AND resolved_at IS NULL",
            (track_id, project_id, oi_id, _LINK_TYPE),
        ).fetchone()
        if not active:
            return False
        conn.execute("BEGIN IMMEDIATE")
        tracks.unlink_open_item(
            state_dir, track_id, project_id, oi_id, _LINK_TYPE,
            reason=reason, actor="system", conn=conn, event_sink=events,
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — never break the caller's gate decision
        if conn is not None:
            conn.rollback()
        _LOG.warning(
            "gate_findings_bridge: resolve failed dispatch=%s track=%s gate=%s: %s",
            dispatch_id, track_id, gate_name, exc,
        )
        return False
    finally:
        if conn is not None:
            conn.close()
    _emit_deferred(state_dir, events)
    _LOG.info(
        "gate_findings_bridge: RESOLVED finding track=%s gate=%s dispatch=%s: %s",
        track_id, gate_name, dispatch_id, reason,
    )
    return True


def _emit_deferred(state_dir: str | Path, events: list) -> None:
    """Emit deferred ADR-005 events AFTER the commit (D3) — logged, non-fatal on failure."""
    for spec in events:
        try:
            tracks._emit_track_event(state_dir, *spec)
        except Exception as exc:  # noqa: BLE001 — post-commit best-effort, never fatal
            _LOG.warning("gate_findings_bridge: post-commit ledger emit failed: %s", exc)
