#!/usr/bin/env python3
"""tracks.py — pure data-access layer for the VNX track system.

CRUD + phase transitions + dependency helpers.
Uses coordination_db.get_connection_for_db for WAL-mode, FK-enforced connections.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_FILENAME = "runtime_coordination.db"
_TRACK_EVENTS_FILE = "track_events.ndjson"

VALID_PHASES = frozenset({"queued", "active", "parked", "done"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued":  frozenset({"active", "parked"}),
    "active":  frozenset({"done", "parked"}),
    "parked":  frozenset({"queued"}),
    "done":    frozenset(),
}


class TrackNotFoundError(ValueError):
    pass


class InvalidPhaseError(ValueError):
    pass


class InvalidTransitionError(ValueError):
    pass


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _db_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / DB_FILENAME


def _emit_track_event(
    state_dir: str | Path,
    event_type: str,
    track_id: str,
    actor: str,
    details: Optional[dict] = None,
) -> None:
    """Best-effort: write one NDJSON audit line to track_events.ndjson."""
    try:
        _lib = Path(__file__).resolve().parent
        if str(_lib) not in sys.path:
            sys.path.insert(0, str(_lib))
        import state_writer  # noqa: PLC0415
        record: dict[str, Any] = {
            "event_type": event_type,
            "track_id": track_id,
            "actor": actor,
            "timestamp": _now_utc(),
        }
        if details:
            record["details"] = details
        state_writer.append_locked(Path(state_dir) / _TRACK_EVENTS_FILE, record)
    except Exception:
        pass


def _get_conn(state_dir: str | Path) -> sqlite3.Connection:
    path = _db_path(state_dir)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_track(
    state_dir: str | Path,
    track_id: str,
    title: str,
    goal_state: str,
    *,
    phase: str = "queued",
    sort_order: int = 0,
    priority: Optional[str] = None,
    project_id: str = "vnx-dev",
    requires_operator_promotion: int = 1,
    instruction_template: Optional[str] = None,
    context_composer_rules: Optional[str] = None,
    pr_ref: Optional[str] = None,
    trigger_condition: Optional[str] = None,
    metadata_json: Optional[str] = None,
) -> dict[str, Any]:
    if phase not in VALID_PHASES:
        raise InvalidPhaseError(f"Invalid phase: {phase!r}. Must be one of {sorted(VALID_PHASES)}")

    now = _now_utc()
    conn = _get_conn(state_dir)
    try:
        conn.execute(
            """
            INSERT INTO tracks (
                track_id, title, goal_state, phase, next_up, sort_order, priority,
                requires_operator_promotion, instruction_template, context_composer_rules,
                pr_ref, trigger_condition, project_id, created_at, phase_changed_at,
                metadata_json
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                track_id, title, goal_state, phase, sort_order, priority,
                requires_operator_promotion, instruction_template, context_composer_rules,
                pr_ref, trigger_condition, project_id, now, now,
                metadata_json or "{}",
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        result = dict(row)
    finally:
        conn.close()
    _emit_track_event(state_dir, "track_created", track_id, "system", {"title": title})
    return result


def get_track(state_dir: str | Path, track_id: str) -> dict[str, Any] | None:
    conn = _get_conn(state_dir)
    try:
        row = conn.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_tracks(
    state_dir: str | Path,
    *,
    phase: Optional[str] = None,
    project_id: str = "vnx-dev",
) -> list[dict[str, Any]]:
    conn = _get_conn(state_dir)
    try:
        if phase is not None:
            if phase not in VALID_PHASES:
                raise InvalidPhaseError(f"Invalid phase: {phase!r}")
            rows = conn.execute(
                """
                SELECT * FROM tracks
                WHERE project_id = ? AND phase = ?
                ORDER BY next_up DESC, sort_order ASC, track_id ASC
                """,
                (project_id, phase),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM tracks
                WHERE project_id = ?
                ORDER BY next_up DESC, sort_order ASC, track_id ASC
                """,
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------

def transition_phase(
    state_dir: str | Path,
    track_id: str,
    to_phase: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    approval_id: Optional[str] = None,
) -> dict[str, Any]:
    if actor not in ("operator", "T0", "system"):
        raise ValueError(f"Invalid actor: {actor!r}. Must be 'operator', 'T0', or 'system'")
    if to_phase not in VALID_PHASES:
        raise InvalidPhaseError(f"Invalid to_phase: {to_phase!r}")

    conn = _get_conn(state_dir)
    try:
        row = conn.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        if not row:
            raise TrackNotFoundError(f"Track not found: {track_id!r}")

        track = dict(row)
        from_phase = track["phase"]

        if from_phase == to_phase:
            return track

        allowed = ALLOWED_TRANSITIONS.get(from_phase, frozenset())
        if to_phase not in allowed:
            raise InvalidTransitionError(
                f"Transition {from_phase!r} → {to_phase!r} not allowed. "
                f"Allowed from {from_phase!r}: {sorted(allowed) or 'none'}"
            )

        now = _now_utc()
        completed_at = now if to_phase == "done" else None

        conn.execute(
            """
            UPDATE tracks
            SET phase = ?, phase_changed_at = ?, completed_at = COALESCE(?, completed_at),
                updated_at = ?
            WHERE track_id = ?
            """,
            (to_phase, now, completed_at, now, track_id),
        ) if _has_updated_at(conn) else conn.execute(
            """
            UPDATE tracks
            SET phase = ?, phase_changed_at = ?, completed_at = COALESCE(?, completed_at)
            WHERE track_id = ?
            """,
            (to_phase, now, completed_at, track_id),
        )

        conn.execute(
            """
            INSERT INTO track_phase_history
                (track_id, from_phase, to_phase, actor, reason, approval_id, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (track_id, from_phase, to_phase, actor, reason, approval_id, now),
        )

        conn.commit()

        updated = conn.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        result = dict(updated)
    finally:
        conn.close()
    _emit_track_event(
        state_dir, "track_phase_transition", track_id, actor,
        {"from": from_phase, "to": to_phase},
    )
    return result


def _has_updated_at(conn: sqlite3.Connection) -> bool:
    info = conn.execute("PRAGMA table_info(tracks)").fetchall()
    return any(row[1] == "updated_at" for row in info)


# ---------------------------------------------------------------------------
# Next-up management
# ---------------------------------------------------------------------------

def set_next_up(state_dir: str | Path, track_id: str) -> None:
    """Mark track_id as next_up=1, clearing any previous next_up in the same project."""
    conn = _get_conn(state_dir)
    try:
        row = conn.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        if not row:
            raise TrackNotFoundError(f"Track not found: {track_id!r}")

        track = dict(row)
        project_id = track["project_id"]

        # Clear existing next_up for this project
        conn.execute(
            "UPDATE tracks SET next_up = 0 WHERE project_id = ? AND next_up = 1",
            (project_id,),
        )
        conn.execute(
            "UPDATE tracks SET next_up = 1 WHERE track_id = ?",
            (track_id,),
        )
        conn.commit()
    finally:
        conn.close()
    _emit_track_event(state_dir, "track_next_up_set", track_id, "system")


# ---------------------------------------------------------------------------
# Open item linking
# ---------------------------------------------------------------------------

def link_open_item(
    state_dir: str | Path,
    track_id: str,
    oi_id: str,
    link_type: str,
    link_source: str,
) -> None:
    valid_link_types = frozenset({"blocks", "warns", "related"})
    valid_link_sources = frozenset({"file_path", "mention", "manual"})

    if link_type not in valid_link_types:
        raise ValueError(f"Invalid link_type: {link_type!r}. Must be one of {sorted(valid_link_types)}")
    if link_source not in valid_link_sources:
        raise ValueError(f"Invalid link_source: {link_source!r}. Must be one of {sorted(valid_link_sources)}")

    conn = _get_conn(state_dir)
    try:
        if not conn.execute("SELECT 1 FROM tracks WHERE track_id = ?", (track_id,)).fetchone():
            raise TrackNotFoundError(f"Track not found: {track_id!r}")

        conn.execute(
            """
            INSERT OR REPLACE INTO track_open_items
                (track_id, oi_id, link_type, link_source, linked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (track_id, oi_id, link_type, link_source, _now_utc()),
        )
        conn.commit()
    finally:
        conn.close()
    _emit_track_event(
        state_dir, "track_oi_linked", track_id, "system",
        {"oi_id": oi_id, "link_type": link_type},
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def add_dependency(
    state_dir: str | Path,
    from_track_id: str,
    to_track_id: str,
    kind: str,
    derivation_source: str,
    *,
    confidence: float = 1.0,
    evidence_json: Optional[str] = None,
) -> None:
    valid_kinds = frozenset({"hard", "soft", "overlap"})
    valid_sources = frozenset({"manual", "git_ancestry", "path_overlap", "oi_ref", "pr_ref"})

    if kind not in valid_kinds:
        raise ValueError(f"Invalid kind: {kind!r}. Must be one of {sorted(valid_kinds)}")
    if derivation_source not in valid_sources:
        raise ValueError(f"Invalid derivation_source: {derivation_source!r}. Must be one of {sorted(valid_sources)}")

    conn = _get_conn(state_dir)
    try:
        for tid in (from_track_id, to_track_id):
            if not conn.execute("SELECT 1 FROM tracks WHERE track_id = ?", (tid,)).fetchone():
                raise TrackNotFoundError(f"Track not found: {tid!r}")

        conn.execute(
            """
            INSERT OR REPLACE INTO track_dependencies
                (from_track_id, to_track_id, kind, derivation_source, confidence,
                 evidence_json, derived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                from_track_id, to_track_id, kind, derivation_source, confidence,
                evidence_json, _now_utc(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    _emit_track_event(
        state_dir, "track_dep_added", from_track_id, "system",
        {"to": to_track_id, "kind": kind},
    )


# ---------------------------------------------------------------------------
# Dispatch link queries (used by `vnx track show`)
# ---------------------------------------------------------------------------

def get_linked_dispatches(
    state_dir: str | Path,
    track_id: str,
) -> list[dict[str, Any]]:
    conn = _get_conn(state_dir)
    try:
        rows = conn.execute(
            "SELECT * FROM dispatches WHERE track = ? ORDER BY created_at DESC",
            (track_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_linked_open_items(
    state_dir: str | Path,
    track_id: str,
) -> list[dict[str, Any]]:
    conn = _get_conn(state_dir)
    try:
        rows = conn.execute(
            "SELECT * FROM track_open_items WHERE track_id = ? ORDER BY linked_at DESC",
            (track_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_receipts(
    state_dir: str | Path,
    track_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return recent coordination events for dispatches linked to track_id."""
    conn = _get_conn(state_dir)
    try:
        dispatch_ids = [
            row[0]
            for row in conn.execute(
                "SELECT dispatch_id FROM dispatches WHERE track = ?", (track_id,)
            ).fetchall()
        ]
        if not dispatch_ids:
            return []

        placeholders = ", ".join("?" * len(dispatch_ids))
        rows = conn.execute(
            f"""
            SELECT * FROM coordination_events
            WHERE entity_id IN ({placeholders})
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (*dispatch_ids, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
