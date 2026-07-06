"""tests/test_track_reconciler.py — Phase 3 advisory rollup reconciler tests.

Verifies:
- derived_status='done' when all dispatches are terminal + pr_merged event exists
- derived_status='blocked' when a blocker OI is linked (link_type='blocks')
- derived_status='blocked' when a dependency track's declared phase is not 'done'
- derived_status='queued' when no dispatches exist
- derived_status='in_progress' when dispatches are active
- derived_status='done' when all dispatches terminal and no pr_ref on track
- derived_status='in_progress' when all dispatches terminal but pr_ref set with no merged event
- Idempotent: re-running produces the same result
- Duplicate pr_merged event does not double-advance (presence check, not counter)
- ROADMAP.yaml is NEVER written by the reconciler
- tracks.phase (authoritative) is NEVER modified by the reconciler
- Migration 0028 up adds derived_status column + down rebuilds without it
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schema_migration
import track_reconciler
import tracks as tracks_lib


PROJECT_ID = "test-proj"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path) -> Path:
    """Return a state_dir with migrations 0022 + 0024 + 0027 + 0028 applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch',
            entity_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT,
            metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            project_id TEXT
        )
    """)
    conn.commit()

    for ver, fname in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    schema_migration.apply_script_if_below(
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()

    schema_migration.apply_script_if_below(
        conn, 28,
        (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    conn.close()
    return state_dir


def _seed_track(
    state_dir: Path,
    track_id: str,
    *,
    phase: str = "active",
    pr_ref: str | None = None,
) -> None:
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID,
        title=f"Track {track_id}",
        goal_state=f"ship {track_id}",
        phase=phase,
        pr_ref=pr_ref,
    )


def _seed_dispatch(
    state_dir: Path,
    dispatch_id: str,
    track_id: str,
    *,
    state: str = "completed",
    pr_ref: str | None = None,
) -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) VALUES (?,?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, track_id, pr_ref),
    )
    conn.commit()
    conn.close()


def _seed_pr_merged_event(state_dir: Path, dispatch_id: str) -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO coordination_events
            (event_id, event_type, entity_type, entity_id, occurred_at, project_id)
        VALUES (?, 'pr_merged', 'dispatch', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)
        """,
        (f"ev-{dispatch_id}", dispatch_id, PROJECT_ID),
    )
    conn.commit()
    conn.close()


def _get_track_row(state_dir: Path, track_id: str) -> dict:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Core derived_status tests
# ---------------------------------------------------------------------------

def test_done_when_all_dispatches_terminal_and_pr_merged(tmp_path):
    """derived_status='done' when all dispatches terminal + pr_merged event."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-done", pr_ref="PR-100")
    _seed_dispatch(state_dir, "D-done-1", "T-done", state="completed", pr_ref="PR-100")
    _seed_pr_merged_event(state_dir, "D-done-1")

    result = track_reconciler.reconcile_track(state_dir, "T-done", PROJECT_ID)
    assert result["derived_status"] == "done"
    assert result["declared_phase"] == "active"
    assert result["drifted"] is True

    row = _get_track_row(state_dir, "T-done")
    assert row["derived_status"] == "done"
    assert row["phase"] == "active"  # authoritative phase unchanged


def test_done_when_no_pr_ref_and_all_terminal(tmp_path):
    """derived_status='done' when track has no pr_ref and all dispatches terminal."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-nopr", pr_ref=None)
    _seed_dispatch(state_dir, "D-nopr-1", "T-nopr", state="completed")
    _seed_dispatch(state_dir, "D-nopr-2", "T-nopr", state="dead_letter")

    result = track_reconciler.reconcile_track(state_dir, "T-nopr", PROJECT_ID)
    assert result["derived_status"] == "done"


def test_in_progress_when_all_terminal_but_no_merged_event(tmp_path):
    """derived_status='in_progress' when all terminal but pr_ref set and no merged event."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-pr-pending", pr_ref="PR-200")
    _seed_dispatch(state_dir, "D-pp-1", "T-pr-pending", state="completed")

    result = track_reconciler.reconcile_track(state_dir, "T-pr-pending", PROJECT_ID)
    assert result["derived_status"] == "in_progress"


def test_blocked_by_blocker_oi(tmp_path):
    """derived_status='blocked' when a link_type='blocks' OI is linked."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-blocked-oi")
    _seed_dispatch(state_dir, "D-boi-1", "T-blocked-oi", state="completed")
    _seed_pr_merged_event(state_dir, "D-boi-1")

    tracks_lib.link_open_item(state_dir, "T-blocked-oi", PROJECT_ID, "OI-999", "blocks", "manual")

    result = track_reconciler.reconcile_track(state_dir, "T-blocked-oi", PROJECT_ID)
    assert result["derived_status"] == "blocked"


def test_blocked_by_dependency_not_done(tmp_path):
    """derived_status='blocked' when a dependency track's declared phase is not 'done'."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-dep-parent", phase="queued")
    _seed_track(state_dir, "T-dep-child")
    tracks_lib.add_dependency(
        state_dir,
        "T-dep-child", PROJECT_ID,
        "T-dep-parent", PROJECT_ID,
        kind="hard", derivation_source="manual",
    )
    _seed_dispatch(state_dir, "D-dep-1", "T-dep-child", state="completed")
    _seed_pr_merged_event(state_dir, "D-dep-1")

    result = track_reconciler.reconcile_track(state_dir, "T-dep-child", PROJECT_ID)
    assert result["derived_status"] == "blocked"


def test_not_blocked_when_dependency_done(tmp_path):
    """derived_status='done' when dependency's declared phase is 'done'."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-prereq", phase="done")
    _seed_track(state_dir, "T-downstream")
    tracks_lib.add_dependency(
        state_dir,
        "T-downstream", PROJECT_ID,
        "T-prereq", PROJECT_ID,
        kind="hard", derivation_source="manual",
    )
    _seed_dispatch(state_dir, "D-ds-1", "T-downstream", state="completed")

    result = track_reconciler.reconcile_track(state_dir, "T-downstream", PROJECT_ID)
    assert result["derived_status"] == "done"


def test_queued_when_no_dispatches(tmp_path):
    """derived_status='queued' when no dispatches are linked."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-nowork", phase="queued")

    result = track_reconciler.reconcile_track(state_dir, "T-nowork", PROJECT_ID)
    assert result["derived_status"] == "queued"


def test_in_progress_when_dispatches_active(tmp_path):
    """derived_status='in_progress' when dispatches are in-flight."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-active")
    _seed_dispatch(state_dir, "D-act-1", "T-active", state="running")
    _seed_dispatch(state_dir, "D-act-2", "T-active", state="completed")

    result = track_reconciler.reconcile_track(state_dir, "T-active", PROJECT_ID)
    assert result["derived_status"] == "in_progress"


# ---------------------------------------------------------------------------
# Idempotency and replay-safety
# ---------------------------------------------------------------------------

def test_idempotent_rerun(tmp_path):
    """Running the reconciler twice produces the same derived_status."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-idem", pr_ref="PR-300")
    _seed_dispatch(state_dir, "D-idem-1", "T-idem", state="completed")
    _seed_pr_merged_event(state_dir, "D-idem-1")

    r1 = track_reconciler.reconcile_track(state_dir, "T-idem", PROJECT_ID)
    r2 = track_reconciler.reconcile_track(state_dir, "T-idem", PROJECT_ID)

    assert r1["derived_status"] == "done"
    assert r2["derived_status"] == "done"
    assert r1["derived_status"] == r2["derived_status"]


def test_duplicate_event_no_double_advance(tmp_path):
    """A duplicate pr_merged event does not cause inconsistent state."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-dup", pr_ref="PR-400")
    _seed_dispatch(state_dir, "D-dup-1", "T-dup", state="completed")

    # Insert the pr_merged event twice (simulates duplicate/replay).
    _seed_pr_merged_event(state_dir, "D-dup-1")
    _seed_pr_merged_event(state_dir, "D-dup-1")

    result = track_reconciler.reconcile_track(state_dir, "T-dup", PROJECT_ID)
    assert result["derived_status"] == "done"

    # Second reconcile pass: still 'done', not some unexpected state.
    result2 = track_reconciler.reconcile_track(state_dir, "T-dup", PROJECT_ID)
    assert result2["derived_status"] == "done"


# ---------------------------------------------------------------------------
# Contract: ROADMAP.yaml and authoritative phase must never be written
# ---------------------------------------------------------------------------

def test_roadmap_yaml_never_written(tmp_path):
    """The reconciler must not create or modify ROADMAP.yaml."""
    state_dir = _build_db(tmp_path)
    project_root = tmp_path
    roadmap_yaml = project_root / "ROADMAP.yaml"

    _seed_track(state_dir, "T-nowrite")
    _seed_dispatch(state_dir, "D-nw-1", "T-nowrite", state="completed")
    _seed_pr_merged_event(state_dir, "D-nw-1")

    track_reconciler.reconcile_all_tracks(state_dir, PROJECT_ID)

    assert not roadmap_yaml.exists(), "reconciler must not create ROADMAP.yaml"


def test_authoritative_phase_never_modified(tmp_path):
    """The reconciler must not touch tracks.phase."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-phase", phase="active")
    _seed_dispatch(state_dir, "D-ph-1", "T-phase", state="completed")
    _seed_pr_merged_event(state_dir, "D-ph-1")

    before = _get_track_row(state_dir, "T-phase")["phase"]

    track_reconciler.reconcile_track(state_dir, "T-phase", PROJECT_ID)

    after = _get_track_row(state_dir, "T-phase")["phase"]
    assert before == after == "active"


# ---------------------------------------------------------------------------
# reconcile_all_tracks
# ---------------------------------------------------------------------------

def test_reconcile_all_tracks_processes_all(tmp_path):
    """reconcile_all_tracks handles multiple tracks and returns one result per track."""
    state_dir = _build_db(tmp_path)
    for tid in ("T-all-1", "T-all-2"):
        _seed_track(state_dir, tid)
    _seed_track(state_dir, "T-all-3", phase="queued")

    _seed_dispatch(state_dir, "D-all-1", "T-all-1", state="running")
    _seed_dispatch(state_dir, "D-all-2", "T-all-2", state="completed")
    # T-all-3: no dispatches → queued

    results = track_reconciler.reconcile_all_tracks(state_dir, PROJECT_ID)

    by_id = {r["track_id"]: r for r in results}
    assert set(by_id.keys()) == {"T-all-1", "T-all-2", "T-all-3"}
    assert by_id["T-all-1"]["derived_status"] == "in_progress"
    assert by_id["T-all-2"]["derived_status"] == "done"  # no pr_ref on track
    assert by_id["T-all-3"]["derived_status"] == "queued"


# ---------------------------------------------------------------------------
# Migration 0028 up/down
# ---------------------------------------------------------------------------

def _base_v27_db(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """Build a DB at user_version=27 (0022+0024+0027 applied)."""
    db_path = tmp_path / "rc.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()
    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    return conn, db_path


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_0028_up_adds_derived_status_column(tmp_path):
    conn, _ = _base_v27_db(tmp_path)
    assert "derived_status" not in _cols(conn, "tracks")
    assert schema_migration.get_user_version(conn) == 27

    sql = (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8")
    applied = schema_migration.apply_script_if_below(conn, 28, sql)
    conn.commit()

    assert applied is True
    assert schema_migration.get_user_version(conn) == 28
    assert "derived_status" in _cols(conn, "tracks")


def test_0028_idempotent(tmp_path):
    conn, _ = _base_v27_db(tmp_path)
    sql = (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 28, sql)
    conn.commit()
    applied_again = schema_migration.apply_script_if_below(conn, 28, sql)
    assert applied_again is False
    assert schema_migration.get_user_version(conn) == 28


def test_0028_down_removes_derived_status(tmp_path):
    conn, _ = _base_v27_db(tmp_path)
    sql_up = (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 28, sql_up)
    conn.commit()
    assert "derived_status" in _cols(conn, "tracks")

    # Seed a track to verify data survives
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, horizon, derived_status) "
        "VALUES ('t-down', 'vnx-dev', 'T', 'g', 'queued', 'now', 'queued')"
    )
    conn.commit()

    sql_down = (_MIGRATIONS / "0028_tracks_derived_status_down.sql").read_text(encoding="utf-8")
    for stmt in schema_migration._split_sql_statements(sql_down):
        conn.execute(stmt)
    conn.commit()

    assert schema_migration.get_user_version(conn) == 27
    assert "derived_status" not in _cols(conn, "tracks")
    assert "horizon" in _cols(conn, "tracks")  # horizon preserved

    row = conn.execute(
        "SELECT track_id, phase FROM tracks WHERE track_id='t-down'"
    ).fetchone()
    assert row == ("t-down", "queued")


def test_blocking_detail_names_blocker_oi(tmp_path):
    """blocking_detail.blocking_ois names the unresolved blocker OI; the hint
    carries the exact oi-close command to resolve it."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-hint-oi")
    _seed_dispatch(state_dir, "D-hint-oi-1", "T-hint-oi", state="completed")
    _seed_pr_merged_event(state_dir, "D-hint-oi-1")
    tracks_lib.link_open_item(state_dir, "T-hint-oi", PROJECT_ID, "OI-777", "blocks", "manual")

    result = track_reconciler.reconcile_track(state_dir, "T-hint-oi", PROJECT_ID)
    assert result["derived_status"] == "blocked"
    detail = result["blocking_detail"]
    assert detail["blocking_ois"] == [{"oi_id": "OI-777"}]
    assert detail["blocking_deps"] == []

    hint = track_reconciler.format_blocking_hint(detail)
    assert "OI-777" in hint
    assert "vnx track oi-close OI-777" in hint


def test_blocking_detail_names_dependency(tmp_path):
    """blocking_detail.blocking_deps names the not-done dependency track."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-hint-dep-parent", phase="queued")
    _seed_track(state_dir, "T-hint-dep-child")
    tracks_lib.add_dependency(
        state_dir,
        "T-hint-dep-child", PROJECT_ID,
        "T-hint-dep-parent", PROJECT_ID,
        kind="hard", derivation_source="manual",
    )
    _seed_dispatch(state_dir, "D-hint-dep-1", "T-hint-dep-child", state="completed")

    result = track_reconciler.reconcile_track(state_dir, "T-hint-dep-child", PROJECT_ID)
    assert result["derived_status"] == "blocked"
    detail = result["blocking_detail"]
    assert detail["blocking_ois"] == []
    assert detail["blocking_deps"] == [{"track_id": "T-hint-dep-parent", "phase": "queued"}]

    hint = track_reconciler.format_blocking_hint(detail)
    assert "T-hint-dep-parent" in hint
    assert "not done" in hint


def test_no_blocking_detail_when_not_blocked(tmp_path):
    """A non-blocked track carries no blocking_detail key; its hint is empty."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-hint-clean")
    _seed_dispatch(state_dir, "D-hint-clean-1", "T-hint-clean", state="completed")
    _seed_pr_merged_event(state_dir, "D-hint-clean-1")

    result = track_reconciler.reconcile_track(state_dir, "T-hint-clean", PROJECT_ID)
    assert result["derived_status"] != "blocked"
    assert "blocking_detail" not in result
    assert track_reconciler.format_blocking_hint(result.get("blocking_detail")) == ""


def test_blocking_detail_works_pre_0030_without_resolved_at(tmp_path):
    """No resolved_at column (pre-migration-0030 DB) — the presence-only
    fallback still NAMES the blocker OI, not just detects it."""
    state_dir = _build_db(tmp_path)
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(track_open_items)")]
    conn.close()
    assert "resolved_at" not in cols  # _build_db never applies migration 0030

    _seed_track(state_dir, "T-hint-pre0030")
    _seed_dispatch(state_dir, "D-hint-pre0030-1", "T-hint-pre0030", state="completed")
    tracks_lib.link_open_item(state_dir, "T-hint-pre0030", PROJECT_ID, "OI-888", "blocks", "manual")

    result = track_reconciler.reconcile_track(state_dir, "T-hint-pre0030", PROJECT_ID)
    assert result["derived_status"] == "blocked"
    assert result["blocking_detail"]["blocking_ois"] == [{"oi_id": "OI-888"}]


def test_format_blocking_hint_none_and_empty():
    """format_blocking_hint tolerates None and an empty dict — both '' """
    assert track_reconciler.format_blocking_hint(None) == ""
    assert track_reconciler.format_blocking_hint({}) == ""


def test_peek_derived_status_carries_blocking_detail(tmp_path):
    """peek_derived_status (read-only preview) also carries blocking_detail."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-hint-peek")
    _seed_dispatch(state_dir, "D-hint-peek-1", "T-hint-peek", state="completed")
    tracks_lib.link_open_item(state_dir, "T-hint-peek", PROJECT_ID, "OI-555", "blocks", "manual")

    result = track_reconciler.peek_derived_status(state_dir, "T-hint-peek", PROJECT_ID)
    assert result["derived_status"] == "blocked"
    assert result["blocking_detail"]["blocking_ois"] == [{"oi_id": "OI-555"}]


def test_0028_down_preserves_track_dependencies(tmp_path):
    """Down migration must not break FK-referenced data."""
    conn, _ = _base_v27_db(tmp_path)
    sql_up = (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 28, sql_up)
    conn.commit()

    conn.executemany(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, phase) VALUES (?,?,?,?,?)",
        [("t-a", "vnx-dev", "A", "g", "active"), ("t-b", "vnx-dev", "B", "g", "queued")],
    )
    conn.commit()
    conn.execute(
        "INSERT INTO track_dependencies "
        "(from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source) "
        "VALUES ('t-a','vnx-dev','t-b','vnx-dev','hard','manual')"
    )
    conn.commit()

    sql_down = (_MIGRATIONS / "0028_tracks_derived_status_down.sql").read_text(encoding="utf-8")
    for stmt in schema_migration._split_sql_statements(sql_down):
        conn.execute(stmt)
    conn.commit()

    dep = conn.execute("SELECT * FROM track_dependencies").fetchone()
    assert dep is not None
    assert dep[0] == "t-a"
