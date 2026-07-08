"""tests/test_gate_findings_bridge.py — blocking gate verdict -> fabric open-item.

Covers the dispatch contract for 20260708-gate-findings-fabric:
  1. linked dispatch + BLOCKING gate -> a `blocks` track_open_item, idempotent on rerun.
  2. unlinked dispatch (no track_id / no project_id) -> quiet no-op, no orphaned row.
  3. a subsequent clean gate run resolves the finding (tracks.unlink_open_item semantics).
  4. tenant scoping: a dispatch in project B never links a track in project A.
  5. the ADR-005 ledger event carries gate_name/summary/pr_ref (Past-layer detail).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402
import gate_findings_bridge as bridge  # noqa: E402

PROJECT_A = "test-proj-a"
PROJECT_B = "test-proj-b"


def _build_db(tmp_path: Path) -> Path:
    """State_dir with the track layer (0022-0030) + a dispatches.track_id column.

    Mirrors dispatch_cli._persist_track_id: track_id is an additive ALTER TABLE, not
    part of the original dispatches schema.
    """
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
    conn.commit()

    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()

    # track_id is an ADDITIVE column (dispatch_cli._persist_track_id), not part of any
    # migration's base schema — added here exactly as production adds it lazily.
    conn.execute("ALTER TABLE dispatches ADD COLUMN track_id TEXT")
    conn.commit()
    conn.close()
    return state_dir


def _insert_dispatch(
    state_dir: Path, dispatch_id: str, project_id: str, track_id: str | None
) -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, track_id) VALUES (?, ?, ?)",
        (dispatch_id, project_id, track_id),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def state_dir(tmp_path):
    return _build_db(tmp_path)


def _oi_rows(state_dir: Path, track_id: str, project_id: str) -> list[dict]:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM track_open_items WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class TestRecordGateFinding:
    def test_linked_dispatch_creates_blocking_open_item(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-1", PROJECT_A, "T1", "G1", phase="active")
        _insert_dispatch(state_dir, "d-1", PROJECT_A, "feat-1")

        ok = bridge.record_gate_finding(
            state_dir, dispatch_id="d-1", gate_name="pre_merge_gate",
            summary="pr_size: too large", pr_ref="PR-6",
        )
        assert ok is True

        rows = _oi_rows(state_dir, "feat-1", PROJECT_A)
        assert len(rows) == 1
        assert rows[0]["oi_id"] == "gate:pre_merge_gate:d-1"
        assert rows[0]["link_type"] == "blocks"
        assert rows[0]["resolved_at"] is None

    def test_rerun_is_idempotent_no_duplicate(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-2", PROJECT_A, "T2", "G2", phase="active")
        _insert_dispatch(state_dir, "d-2", PROJECT_A, "feat-2")

        for _ in range(3):
            bridge.record_gate_finding(
                state_dir, dispatch_id="d-2", gate_name="phantom_guard",
                summary="phantom rejected",
            )

        rows = _oi_rows(state_dir, "feat-2", PROJECT_A)
        assert len(rows) == 1  # upsert on (track_id, oi_id, link_type) PK — never duplicates

    def test_different_gate_names_are_distinct_findings(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-3", PROJECT_A, "T3", "G3", phase="active")
        _insert_dispatch(state_dir, "d-3", PROJECT_A, "feat-3")

        bridge.record_gate_finding(state_dir, dispatch_id="d-3", gate_name="pre_merge_gate", summary="s1")
        bridge.record_gate_finding(state_dir, dispatch_id="d-3", gate_name="phantom_guard", summary="s2")

        rows = _oi_rows(state_dir, "feat-3", PROJECT_A)
        assert {r["oi_id"] for r in rows} == {
            "gate:pre_merge_gate:d-3", "gate:phantom_guard:d-3",
        }

    def test_unlinked_dispatch_missing_row_is_noop(self, state_dir):
        """No dispatches row at all for this dispatch_id — degrade quietly."""
        ok = bridge.record_gate_finding(
            state_dir, dispatch_id="no-such-dispatch", gate_name="pre_merge_gate", summary="x",
        )
        assert ok is False

    def test_unlinked_dispatch_null_track_id_is_noop(self, state_dir):
        """A dispatch row exists but track_id is NULL (never linked to a track)."""
        _insert_dispatch(state_dir, "d-4", PROJECT_A, None)
        ok = bridge.record_gate_finding(
            state_dir, dispatch_id="d-4", gate_name="pre_merge_gate", summary="x",
        )
        assert ok is False
        # No orphaned open-item under any track for this project.
        db = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        n = conn.execute(
            "SELECT COUNT(*) FROM track_open_items WHERE oi_id LIKE 'gate:%d-4%'"
        ).fetchone()[0]
        conn.close()
        assert n == 0

    def test_missing_project_id_column_fails_closed_not_vnx_dev(self, tmp_path):
        """ADR-007: a dispatches table without project_id must never default to 'vnx-dev'.

        Uses a standalone minimal DB (no track layer needed — resolution must fail
        before ever reaching tracks/track_open_items).
        """
        state_dir = tmp_path / "bare_state"
        state_dir.mkdir(parents=True)
        db = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, track_id TEXT)")
        conn.execute("INSERT INTO dispatches (dispatch_id, track_id) VALUES ('d-5', 'feat-x')")
        conn.commit()
        conn.close()

        ok = bridge.record_gate_finding(
            state_dir, dispatch_id="d-5", gate_name="pre_merge_gate", summary="x",
        )
        assert ok is False

    def test_tenant_scoping_same_track_id_different_project(self, state_dir):
        """Two projects can legally reuse the same track_id string; a finding for project B
        must never land on project A's track row."""
        tracks_lib.create_track(state_dir, "shared-id", PROJECT_A, "TA", "GA", phase="active")
        tracks_lib.create_track(state_dir, "shared-id", PROJECT_B, "TB", "GB", phase="active")
        _insert_dispatch(state_dir, "d-6", PROJECT_B, "shared-id")

        bridge.record_gate_finding(
            state_dir, dispatch_id="d-6", gate_name="pre_merge_gate", summary="x",
        )

        assert _oi_rows(state_dir, "shared-id", PROJECT_B) != []
        assert _oi_rows(state_dir, "shared-id", PROJECT_A) == []

    def test_ledger_event_carries_gate_details(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-7", PROJECT_A, "T7", "G7", phase="active")
        _insert_dispatch(state_dir, "d-7", PROJECT_A, "feat-7")

        bridge.record_gate_finding(
            state_dir, dispatch_id="d-7", gate_name="pre_merge_gate",
            summary="net_deletion: mass delete", pr_ref="PR-42",
        )

        events_file = state_dir.parent / "events" / "track_events.ndjson"
        events = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
        linked = [e for e in events if e.get("event_type") == "track_oi_linked" and e.get("track_id") == "feat-7"]
        assert len(linked) == 1
        details = linked[0]["details"]
        assert details["gate_name"] == "pre_merge_gate"
        assert details["dispatch_id"] == "d-7"
        assert details["pr_ref"] == "PR-42"
        assert "net_deletion" in details["summary"]


class TestResolveGateFinding:
    def test_resolve_closes_active_finding(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-8", PROJECT_A, "T8", "G8", phase="active")
        _insert_dispatch(state_dir, "d-8", PROJECT_A, "feat-8")
        bridge.record_gate_finding(state_dir, dispatch_id="d-8", gate_name="pre_merge_gate", summary="x")

        ok = bridge.resolve_gate_finding(
            state_dir, dispatch_id="d-8", gate_name="pre_merge_gate", reason="clean rerun",
        )
        assert ok is True

        rows = _oi_rows(state_dir, "feat-8", PROJECT_A)
        assert len(rows) == 1
        assert rows[0]["resolved_at"] is not None
        assert rows[0]["resolution_reason"] == "clean rerun"

        # Excluded from the active-only view (no stale finding lingers).
        active = tracks_lib.get_linked_open_items(state_dir, "feat-8", PROJECT_A)
        assert active == []

    def test_resolve_noop_when_no_prior_finding(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-9", PROJECT_A, "T9", "G9", phase="active")
        _insert_dispatch(state_dir, "d-9", PROJECT_A, "feat-9")

        ok = bridge.resolve_gate_finding(
            state_dir, dispatch_id="d-9", gate_name="pre_merge_gate",
        )
        assert ok is False

    def test_resolve_noop_when_dispatch_unlinked(self, state_dir):
        ok = bridge.resolve_gate_finding(
            state_dir, dispatch_id="no-such-dispatch", gate_name="pre_merge_gate",
        )
        assert ok is False

    def test_resolve_is_idempotent(self, state_dir):
        """Resolving twice never raises — the second call finds no active row."""
        tracks_lib.create_track(state_dir, "feat-10", PROJECT_A, "T10", "G10", phase="active")
        _insert_dispatch(state_dir, "d-10", PROJECT_A, "feat-10")
        bridge.record_gate_finding(state_dir, dispatch_id="d-10", gate_name="pre_merge_gate", summary="x")

        first = bridge.resolve_gate_finding(state_dir, dispatch_id="d-10", gate_name="pre_merge_gate")
        second = bridge.resolve_gate_finding(state_dir, dispatch_id="d-10", gate_name="pre_merge_gate")
        assert first is True
        assert second is False
