#!/usr/bin/env python3
"""tracks.py — pure data-access layer for the VNX track system.

CRUD + phase transitions + dependency helpers.
Uses coordination_db.get_connection_for_db for WAL-mode, FK-enforced connections.

Breaking change (FUT-2a): all mutator functions now require project_id as a
positional parameter. Silent 'vnx-dev' default removed per ADR-007.
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

VALID_HORIZONS = frozenset({"now", "next", "later"})


class InvalidHorizonError(ValueError):
    pass

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued":  frozenset({"active", "parked"}),
    "active":  frozenset({"done", "parked"}),
    "parked":  frozenset({"queued"}),
    "done":    frozenset({"active"}),
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
    project_id: str,
    actor: str,
    details: Optional[dict] = None,
) -> None:
    """Write one NDJSON audit line to track_events.ndjson. Raises on write failure (ADR-005).

    record_id sha256 includes project_id — OI-004 fix to prevent collision
    between same track_id in different projects.
    """
    _lib = Path(__file__).resolve().parent
    if str(_lib) not in sys.path:
        sys.path.insert(0, str(_lib))
    import state_writer  # noqa: PLC0415
    import hashlib as _hashlib
    _ts = _now_utc()
    _rid = _hashlib.sha256(
        f'{event_type}:{track_id}:{project_id}:{_ts}'.encode()
    ).hexdigest()[:16]
    record: dict[str, Any] = {
        "event_type": event_type,
        "track_id": track_id,
        "project_id": project_id,
        "actor": actor,
        "timestamp": _ts,
        "record_id": _rid,
    }
    if details:
        record["details"] = details
    state_writer.append_locked(Path(state_dir).parent / "events" / _TRACK_EVENTS_FILE, record)


def _emit_or_defer(
    event_sink: Optional[list],
    state_dir: str | Path,
    event_type: str,
    track_id: str,
    project_id: str,
    actor: str,
    details: dict,
) -> None:
    """Emit a ledger event NOW, or DEFER it to a caller-owned sink (ADR-005 / D3).

    When ``event_sink`` is None the event is written immediately (standalone
    callers, backward-compatible). When a sink is provided, the
    (event_type, track_id, project_id, actor, details) spec is appended and the
    CALLER is responsible for emitting it AFTER committing the DB transaction.
    Deferring makes the DB authoritative: a rolled-back mutation can never orphan
    an already-written NDJSON event (the bridge's D3 deviation — see
    import_open_items_to_tracks).
    """
    if event_sink is None:
        _emit_track_event(state_dir, event_type, track_id, project_id, actor, details)
    else:
        event_sink.append((event_type, track_id, project_id, actor, details))


def _get_conn(state_dir: str | Path) -> sqlite3.Connection:
    path = _db_path(state_dir)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _assert_conn_targets_db(
    conn: sqlite3.Connection, state_dir: str | Path, op: str
) -> None:
    """Raise unless ``conn``'s main schema file is the DB derived from state_dir.

    C3-N2: a primitive enrolled in a caller-owned transaction must never mutate
    an unrelated database. Compare the resolved ``runtime_coordination.db`` path
    against the connection's ``PRAGMA database_list`` 'main' file so a conn opened
    on a different (e.g. wrong-tenant) database fails LOUD instead of silently
    corrupting state.
    """
    expected = _db_path(state_dir).resolve()
    main_file = next(
        (row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"),
        None,
    )
    actual = Path(main_file).resolve() if main_file else None
    if actual != expected:
        raise ValueError(
            f"{op}: supplied conn targets {str(actual)!r}, expected "
            f"{str(expected)!r} (state_dir mismatch); refusing to mutate a database "
            "other than the one derived from state_dir (C3-N2)."
        )


def _validate_shared_conn(
    conn: Optional[sqlite3.Connection],
    state_dir: str | Path,
    event_sink: Optional[list],
    op: str,
) -> None:
    """Enforce the shared-connection (owns=False) contract for link/unlink (C3-N1).

    When a caller supplies its own ``conn`` the primitive joins a CALLER-owned
    transaction and must defer BOTH the commit AND the ADR-005 event to the caller:
    a non-None ``event_sink`` is therefore REQUIRED (the event is appended for
    post-commit emission; the primitive emits NOTHING before the run-level commit).
    The conn must also target the database derived from ``state_dir`` (C3-N2). No-op
    when ``conn`` is None — the standalone owns=True path is unchanged.
    """
    if conn is None:
        return
    if event_sink is None:
        raise ValueError(
            f"{op}: a caller-supplied conn (owns=False) must defer its ADR-005 event "
            "— pass event_sink so the event emits only AFTER the caller's run-level "
            "commit (post-commit DB-authoritative model; C3-N1)."
        )
    _assert_conn_targets_db(conn, state_dir, op)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_track(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    title: str,
    goal_state: str,
    *,
    phase: str = "queued",
    sort_order: int = 0,
    priority: Optional[str] = None,
    requires_operator_promotion: int = 1,
    instruction_template: Optional[str] = None,
    context_composer_rules: Optional[str] = None,
    pr_ref: Optional[str] = None,
    trigger_condition: Optional[str] = None,
    metadata_json: Optional[str] = None,
    horizon: Optional[str] = None,
) -> dict[str, Any]:
    if phase not in VALID_PHASES:
        raise InvalidPhaseError(f"Invalid phase: {phase!r}. Must be one of {sorted(VALID_PHASES)}")
    if horizon is not None and horizon not in VALID_HORIZONS:
        raise InvalidHorizonError(
            f"Invalid horizon: {horizon!r}. Must be one of {sorted(VALID_HORIZONS)} or None"
        )

    now = _now_utc()
    _emit_track_event(state_dir, "track_created", track_id, project_id, "system", {"title": title})
    conn = _get_conn(state_dir)
    try:
        # horizon (migration 0027) is optional: only reference the column when it
        # exists, so the DAL stays compatible with pre-0027 (v24-era) databases.
        has_horizon = any(
            row[1] == "horizon" for row in conn.execute("PRAGMA table_info('tracks')")
        )
        if has_horizon:
            conn.execute(
                """
                INSERT INTO tracks (
                    track_id, project_id, title, goal_state, phase, next_up, sort_order, priority,
                    requires_operator_promotion, instruction_template, context_composer_rules,
                    pr_ref, trigger_condition, created_at, phase_changed_at,
                    metadata_json, horizon
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track_id, project_id, title, goal_state, phase, sort_order, priority,
                    requires_operator_promotion, instruction_template, context_composer_rules,
                    pr_ref, trigger_condition, now, now,
                    metadata_json or "{}", horizon,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO tracks (
                    track_id, project_id, title, goal_state, phase, next_up, sort_order, priority,
                    requires_operator_promotion, instruction_template, context_composer_rules,
                    pr_ref, trigger_condition, created_at, phase_changed_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track_id, project_id, title, goal_state, phase, sort_order, priority,
                    requires_operator_promotion, instruction_template, context_composer_rules,
                    pr_ref, trigger_condition, now, now,
                    metadata_json or "{}",
                ),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        result = dict(row)
    finally:
        conn.close()
    return result


def update_authored_fields(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    *,
    title: Optional[str] = None,
    goal_state: Optional[str] = None,
    priority: Optional[str] = None,
    sort_order: Optional[int] = None,
    horizon: Optional[str] = None,
    pr_ref: Optional[str] = None,
    metadata_json: Optional[str] = None,
    actor: str = "system",
) -> dict[str, Any]:
    """Update ONLY authored-derived fields on an existing track.

    Used by the ROADMAP seeder for idempotent re-runs. Deliberately NEVER
    touches `phase` (declared status is operator/T0/reconciler territory) or
    `next_up`. Only the fields passed (non-None) are written. Emits a
    `track_authored_synced` audit event listing the changed columns.

    Raises TrackNotFoundError if the track does not exist.
    """
    if horizon is not None and horizon not in VALID_HORIZONS:
        raise InvalidHorizonError(
            f"Invalid horizon: {horizon!r}. Must be one of {sorted(VALID_HORIZONS)} or None"
        )

    updates: dict[str, Any] = {}
    if title is not None:
        updates["title"] = title
    if goal_state is not None:
        updates["goal_state"] = goal_state
    if priority is not None:
        updates["priority"] = priority
    if sort_order is not None:
        updates["sort_order"] = sort_order
    if horizon is not None:
        updates["horizon"] = horizon
    if pr_ref is not None:
        updates["pr_ref"] = pr_ref
    if metadata_json is not None:
        updates["metadata_json"] = metadata_json

    conn = _get_conn(state_dir)
    try:
        row = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        if not row:
            raise TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")

        if updates:
            _emit_track_event(
                state_dir, "track_authored_synced", track_id, project_id, actor,
                {"fields": sorted(updates.keys())},
            )
            set_clause = ", ".join(f"{col} = ?" for col in updates)
            conn.execute(
                f"UPDATE tracks SET {set_clause} WHERE track_id = ? AND project_id = ?",
                (*updates.values(), track_id, project_id),
            )
            conn.commit()

        updated = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        return dict(updated)
    finally:
        conn.close()


def get_track(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
) -> dict[str, Any] | None:
    conn = _get_conn(state_dir)
    try:
        row = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_tracks(
    state_dir: str | Path,
    project_id: str,
    *,
    phase: Optional[str] = None,
    all_projects: bool = False,
) -> list[dict[str, Any]]:
    conn = _get_conn(state_dir)
    try:
        if all_projects:
            if phase is not None:
                if phase not in VALID_PHASES:
                    raise InvalidPhaseError(f"Invalid phase: {phase!r}")
                rows = conn.execute(
                    """
                    SELECT * FROM tracks
                    WHERE phase = ?
                    ORDER BY project_id ASC, next_up DESC, sort_order ASC, track_id ASC
                    """,
                    (phase,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM tracks
                    ORDER BY project_id ASC, next_up DESC, sort_order ASC, track_id ASC
                    """
                ).fetchall()
        elif phase is not None:
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
    project_id: str,
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
        row = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        if not row:
            raise TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")

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

        _emit_track_event(
            state_dir, "track_phase_transition", track_id, project_id, actor,
            {"from": from_phase, "to": to_phase},
        )

        conn.execute(
            """
            UPDATE tracks
            SET phase = ?, phase_changed_at = ?, completed_at = COALESCE(?, completed_at)
            WHERE track_id = ? AND project_id = ?
            """,
            (to_phase, now, completed_at, track_id, project_id),
        )

        conn.execute(
            """
            INSERT INTO track_phase_history
                (track_id, project_id, from_phase, to_phase, actor, reason, approval_id, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (track_id, project_id, from_phase, to_phase, actor, reason, approval_id, now),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        result = dict(updated)
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Next-up management
# ---------------------------------------------------------------------------

def set_next_up(state_dir: str | Path, track_id: str, project_id: str) -> None:
    """Mark track_id as next_up=1, clearing any previous next_up in the same project."""
    conn = _get_conn(state_dir)
    try:
        row = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        if not row:
            raise TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")

        _emit_track_event(state_dir, "track_next_up_set", track_id, project_id, "system")
        conn.execute(
            "UPDATE tracks SET next_up = 0 WHERE project_id = ? AND next_up = 1",
            (project_id,),
        )
        conn.execute(
            "UPDATE tracks SET next_up = 1 WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Open item linking
# ---------------------------------------------------------------------------

def link_open_item(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    oi_id: str,
    link_type: str,
    link_source: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
    event_sink: Optional[list] = None,
) -> None:
    """Upsert an active track↔open-item link (INSERT OR REPLACE; reopen-safe).

    Pass ``conn`` to enrol in a CALLER-owned transaction (owns=False): the
    primitive defers BOTH the commit AND the ADR-005 event entirely to the caller
    — it emits NOTHING itself. ``event_sink`` is then REQUIRED; the
    ``track_oi_linked`` spec is appended for the caller to emit AFTER its single
    run-level commit (D3 — DB authoritative; no event before the commit, no orphan
    event on rollback; C3-N1). The conn must target the state_dir DB (C3-N2).

    Without ``conn`` (owns=True, standalone) the function self-manages its
    connection, emits the event in-line, and commits — unchanged behaviour.
    """
    valid_link_types = frozenset({"blocks", "warns", "related"})
    valid_link_sources = frozenset({"file_path", "mention", "manual"})

    if link_type not in valid_link_types:
        raise ValueError(f"Invalid link_type: {link_type!r}. Must be one of {sorted(valid_link_types)}")
    if link_source not in valid_link_sources:
        raise ValueError(f"Invalid link_source: {link_source!r}. Must be one of {sorted(valid_link_sources)}")

    owns = conn is None
    _validate_shared_conn(conn, state_dir, event_sink, "link_open_item")
    _conn = _get_conn(state_dir) if owns else conn
    try:
        if not _conn.execute(
            "SELECT 1 FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone():
            raise TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")

        _conn.execute(
            """
            INSERT OR REPLACE INTO track_open_items
                (track_id, project_id, oi_id, link_type, link_source, linked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (track_id, project_id, oi_id, link_type, link_source, _now_utc()),
        )
        _emit_or_defer(
            event_sink, state_dir, "track_oi_linked", track_id, project_id,
            "system", {"oi_id": oi_id, "link_type": link_type},
        )
        if owns:
            _conn.commit()
    except Exception:
        if owns:
            _conn.rollback()
        raise
    finally:
        if owns:
            _conn.close()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def add_dependency(
    state_dir: str | Path,
    from_track_id: str,
    from_project_id: str,
    to_track_id: str,
    to_project_id: str,
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
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_id = ? AND project_id = ?",
            (from_track_id, from_project_id),
        ).fetchone():
            raise TrackNotFoundError(f"Track not found: ({from_track_id!r}, {from_project_id!r})")
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_id = ? AND project_id = ?",
            (to_track_id, to_project_id),
        ).fetchone():
            raise TrackNotFoundError(f"Track not found: ({to_track_id!r}, {to_project_id!r})")

        _emit_track_event(
            state_dir, "track_dep_added", from_track_id, from_project_id, "system",
            {"to_track": to_track_id, "to_project": to_project_id, "kind": kind},
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO track_dependencies
                (from_track_id, from_project_id, to_track_id, to_project_id,
                 kind, derivation_source, confidence, evidence_json, derived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                from_track_id, from_project_id, to_track_id, to_project_id,
                kind, derivation_source, confidence,
                evidence_json, _now_utc(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dispatch link queries (used by `vnx track show`)
# ---------------------------------------------------------------------------

def get_linked_dispatches(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
) -> list[dict[str, Any]]:
    conn = _get_conn(state_dir)
    try:
        rows = conn.execute(
            "SELECT * FROM dispatches WHERE track = ? AND project_id = ? ORDER BY created_at DESC",
            (track_id, project_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def unlink_open_item(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    oi_id: str,
    link_type: str,
    *,
    reason: str,
    actor: str = "operator",
    conn: Optional[sqlite3.Connection] = None,
    event_sink: Optional[list] = None,
) -> None:
    """Close a track↔OI link non-destructively (resolved_at + reason; row kept).
    Requires migration 0030. A caller ``conn`` (owns=False) defers BOTH commit AND
    the ``track_oi_closed`` event — ``event_sink`` REQUIRED, conn must target the
    state_dir DB (D3; C3-N1/C3-N2). Raises TrackNotFoundError/ValueError/RuntimeError."""
    valid_link_types = frozenset({"blocks", "warns", "related"})
    if link_type not in valid_link_types:
        raise ValueError(f"Invalid link_type: {link_type!r}. Must be one of {sorted(valid_link_types)}")
    if not reason or not reason.strip():
        raise ValueError("reason is required and must not be empty")
    owns = conn is None
    _validate_shared_conn(conn, state_dir, event_sink, "unlink_open_item")
    _conn = _get_conn(state_dir) if owns else conn
    try:
        has_resolved_at = any(
            row[1] == "resolved_at"
            for row in _conn.execute("PRAGMA table_info('track_open_items')")
        )
        if not has_resolved_at:
            raise RuntimeError("track_open_items.resolved_at column absent; apply migration 0030 first.")
        if not _conn.execute(
            "SELECT 1 FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone():
            raise TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")
        row = _conn.execute(
            "SELECT resolved_at FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = ?",
            (track_id, project_id, oi_id, link_type),
        ).fetchone()
        if not row:
            raise ValueError(
                f"Open item not found: track={track_id!r} project={project_id!r} "
                f"oi_id={oi_id!r} link_type={link_type!r}"
            )
        if row["resolved_at"] is not None:
            raise ValueError(
                f"Open item already resolved: track={track_id!r} oi_id={oi_id!r} "
                f"link_type={link_type!r} (resolved_at={row['resolved_at']!r})"
            )
        _conn.execute(
            "UPDATE track_open_items SET resolved_at = ?, resolution_reason = ? "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = ?",
            (_now_utc(), reason.strip(), track_id, project_id, oi_id, link_type),
        )
        _emit_or_defer(
            event_sink, state_dir, "track_oi_closed", track_id, project_id, actor,
            {"oi_id": oi_id, "link_type": link_type, "reason": reason},
        )
        if owns:
            _conn.commit()
    except Exception:
        if owns:
            _conn.rollback()
        raise
    finally:
        if owns:
            _conn.close()


def get_linked_open_items(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    conn = _get_conn(state_dir)
    try:
        has_resolved_at = any(
            row[1] == "resolved_at"
            for row in conn.execute("PRAGMA table_info('track_open_items')")
        )
        if has_resolved_at and not include_resolved:
            rows = conn.execute(
                """
                SELECT * FROM track_open_items
                WHERE track_id = ? AND project_id = ? AND resolved_at IS NULL
                ORDER BY linked_at DESC
                """,
                (track_id, project_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM track_open_items WHERE track_id = ? AND project_id = ? ORDER BY linked_at DESC",
                (track_id, project_id),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_receipts(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return recent coordination events for dispatches linked to (track_id, project_id)."""
    conn = _get_conn(state_dir)
    try:
        dispatch_ids = [
            row[0]
            for row in conn.execute(
                "SELECT dispatch_id FROM dispatches WHERE track = ? AND project_id = ?",
                (track_id, project_id),
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
    finally:
        conn.close()
