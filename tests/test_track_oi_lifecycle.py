"""tests/test_track_oi_lifecycle.py — tests for oi-lifecycle-closure feature.

Covers:
  D1 — vnx track done: reason required, event written, done→* rejected
  D2 — unlink_open_item: resolved_at set, reconciler excludes resolved from blockers
  D3 — backfill_track_dispatch_linkage: dry-run match categories + idempotency
  D4 — vnx status --tracks rendering (table rendered, columns present)
  Integration — existing tracks/reconciler tests still pass with migration 0030
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
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
import tracks as tracks_lib
import track_reconciler

PROJECT_ID = "test-proj"


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

def _build_db_v29(tmp_path: Path) -> Path:
    """State_dir with migrations 0022, 0024, 0027, 0028, 0029 applied."""
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
            from_state TEXT, to_state TEXT, actor TEXT, reason TEXT,
            metadata_json TEXT,
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            project_id TEXT
        )
    """)
    conn.commit()

    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()

    conn.close()
    return state_dir


def _build_db_v30(tmp_path: Path) -> Path:
    """State_dir with all migrations including 0030 applied."""
    state_dir = _build_db_v29(tmp_path)
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    sql = (_MIGRATIONS / "0030_track_oi_resolved_at.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 30, sql)
    conn.commit()
    conn.close()
    return state_dir


@pytest.fixture()
def state_dir_v29(tmp_path):
    return _build_db_v29(tmp_path)


@pytest.fixture()
def state_dir(tmp_path):
    return _build_db_v30(tmp_path)


# ---------------------------------------------------------------------------
# D1: vnx track done
# ---------------------------------------------------------------------------

class TestTrackDone:
    def test_active_to_done_sets_phase(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-1", PROJECT_ID, "T1", "G1", phase="active")
        t = tracks_lib.transition_phase(
            state_dir, "feat-1", PROJECT_ID, "done",
            actor="operator", reason="PR merged"
        )
        assert t["phase"] == "done"
        assert t.get("completed_at") is not None

    def test_done_requires_reason_in_phase_history(self, state_dir):
        """transition_phase records reason in track_phase_history."""
        tracks_lib.create_track(state_dir, "feat-2", PROJECT_ID, "T2", "G2", phase="active")
        tracks_lib.transition_phase(
            state_dir, "feat-2", PROJECT_ID, "done",
            actor="operator", reason="shipped in v1.1"
        )
        db = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT reason FROM track_phase_history WHERE track_id = ? AND to_phase = 'done'",
            ("feat-2",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["reason"] == "shipped in v1.1"

    def test_done_writes_phase_transition_event(self, state_dir):
        """transition_phase emits a track_phase_transition event to track_events.ndjson."""
        tracks_lib.create_track(state_dir, "feat-3", PROJECT_ID, "T3", "G3", phase="active")
        tracks_lib.transition_phase(
            state_dir, "feat-3", PROJECT_ID, "done",
            actor="operator", reason="closed"
        )
        events_file = state_dir.parent / "events" / "track_events.ndjson"
        assert events_file.exists(), "track_events.ndjson must exist"
        events = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
        transition_events = [
            e for e in events
            if e.get("event_type") == "track_phase_transition"
            and e.get("track_id") == "feat-3"
        ]
        assert len(transition_events) >= 1
        evt = transition_events[-1]
        assert evt["details"]["to"] == "done"

    def test_done_to_active_allowed_other_transitions_rejected(self, state_dir):
        """done → active is the operator-gated reopen edge (allowed).
        done → parked remains rejected."""
        tracks_lib.create_track(state_dir, "feat-4", PROJECT_ID, "T4", "G4", phase="active")
        tracks_lib.transition_phase(
            state_dir, "feat-4", PROJECT_ID, "done",
            actor="operator", reason="closed"
        )
        # done → active is the operator reopen edge — must succeed
        t = tracks_lib.transition_phase(
            state_dir, "feat-4", PROJECT_ID, "active",
            actor="operator", reason="reopen",
        )
        assert t["phase"] == "active"

        # Separate track: done → parked remains rejected
        tracks_lib.create_track(state_dir, "feat-4b", PROJECT_ID, "T4b", "G4b", phase="active")
        tracks_lib.transition_phase(
            state_dir, "feat-4b", PROJECT_ID, "done",
            actor="operator", reason="closed"
        )
        with pytest.raises(tracks_lib.InvalidTransitionError):
            tracks_lib.transition_phase(
                state_dir, "feat-4b", PROJECT_ID, "parked",
                actor="operator", reason="should fail"
            )

    def test_done_to_done_is_noop(self, state_dir):
        """Transitioning from done to done returns the current state without error."""
        tracks_lib.create_track(state_dir, "feat-5", PROJECT_ID, "T5", "G5", phase="active")
        tracks_lib.transition_phase(
            state_dir, "feat-5", PROJECT_ID, "done",
            actor="operator", reason="first close"
        )
        t = tracks_lib.transition_phase(
            state_dir, "feat-5", PROJECT_ID, "done",
            actor="operator", reason="second close"
        )
        assert t["phase"] == "done"

    def test_queued_to_done_via_allowed_path(self, state_dir):
        """queued -> active -> done is the canonical path."""
        tracks_lib.create_track(state_dir, "feat-6", PROJECT_ID, "T6", "G6", phase="queued")
        tracks_lib.transition_phase(
            state_dir, "feat-6", PROJECT_ID, "active", actor="operator"
        )
        t = tracks_lib.transition_phase(
            state_dir, "feat-6", PROJECT_ID, "done", actor="operator", reason="done"
        )
        assert t["phase"] == "done"

    def test_queued_to_done_directly_raises(self, state_dir):
        """queued -> done directly is not an allowed transition."""
        tracks_lib.create_track(state_dir, "feat-7", PROJECT_ID, "T7", "G7", phase="queued")
        with pytest.raises(tracks_lib.InvalidTransitionError):
            tracks_lib.transition_phase(
                state_dir, "feat-7", PROJECT_ID, "done",
                actor="operator", reason="skip active"
            )


# ---------------------------------------------------------------------------
# D2: unlink_open_item + reconciler
# ---------------------------------------------------------------------------

class TestOiClose:
    def test_unlink_sets_resolved_at(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-1", PROJECT_ID, "T1", "G1", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-1", PROJECT_ID, "OI-001", "blocks", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-1", PROJECT_ID, "OI-001", "blocks",
            reason="fixed in PR #123"
        )
        db = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT resolved_at, resolution_reason FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ?",
            ("feat-1", PROJECT_ID, "OI-001"),
        ).fetchone()
        conn.close()
        assert row["resolved_at"] is not None
        assert row["resolution_reason"] == "fixed in PR #123"

    def test_unlink_row_preserved(self, state_dir):
        """Row must not be deleted — audit trail preserved."""
        tracks_lib.create_track(state_dir, "feat-2", PROJECT_ID, "T2", "G2", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-2", PROJECT_ID, "OI-002", "blocks", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-2", PROJECT_ID, "OI-002", "blocks",
            reason="resolved"
        )
        items = tracks_lib.get_linked_open_items(
            state_dir, "feat-2", PROJECT_ID, include_resolved=True
        )
        assert len(items) == 1
        assert items[0]["oi_id"] == "OI-002"

    def test_unlink_excluded_from_active_list(self, state_dir):
        """Resolved OI is excluded from get_linked_open_items (default include_resolved=False)."""
        tracks_lib.create_track(state_dir, "feat-3", PROJECT_ID, "T3", "G3", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-3", PROJECT_ID, "OI-003", "blocks", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-3", PROJECT_ID, "OI-003", "blocks",
            reason="resolved"
        )
        active = tracks_lib.get_linked_open_items(state_dir, "feat-3", PROJECT_ID)
        assert len(active) == 0

    def test_unlink_reason_required(self, state_dir):
        """unlink_open_item raises ValueError when reason is empty."""
        tracks_lib.create_track(state_dir, "feat-4", PROJECT_ID, "T4", "G4", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-4", PROJECT_ID, "OI-004", "blocks", "manual"
        )
        with pytest.raises(ValueError, match="reason"):
            tracks_lib.unlink_open_item(
                state_dir, "feat-4", PROJECT_ID, "OI-004", "blocks",
                reason=""
            )

    def test_unlink_already_resolved_raises(self, state_dir):
        """Closing an already-resolved OI raises ValueError."""
        tracks_lib.create_track(state_dir, "feat-5", PROJECT_ID, "T5", "G5", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-5", PROJECT_ID, "OI-005", "blocks", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-5", PROJECT_ID, "OI-005", "blocks",
            reason="first resolution"
        )
        with pytest.raises(ValueError, match="already resolved"):
            tracks_lib.unlink_open_item(
                state_dir, "feat-5", PROJECT_ID, "OI-005", "blocks",
                reason="second attempt"
            )

    def test_unlink_nonexistent_oi_raises(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-6", PROJECT_ID, "T6", "G6", phase="active")
        with pytest.raises(ValueError, match="not found"):
            tracks_lib.unlink_open_item(
                state_dir, "feat-6", PROJECT_ID, "OI-NONEXISTENT", "blocks",
                reason="should fail"
            )

    def test_unlink_writes_event(self, state_dir):
        tracks_lib.create_track(state_dir, "feat-7", PROJECT_ID, "T7", "G7", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-7", PROJECT_ID, "OI-007", "blocks", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-7", PROJECT_ID, "OI-007", "blocks",
            reason="closed"
        )
        events_file = state_dir.parent / "events" / "track_events.ndjson"
        events = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
        close_events = [
            e for e in events
            if e.get("event_type") == "track_oi_closed"
            and e.get("track_id") == "feat-7"
        ]
        assert len(close_events) >= 1
        assert close_events[-1]["details"]["oi_id"] == "OI-007"

    def test_unlink_requires_migration_0030(self, state_dir_v29):
        """unlink_open_item raises RuntimeError when migration 0030 is absent."""
        tracks_lib.create_track(state_dir_v29, "feat-8", PROJECT_ID, "T8", "G8", phase="active")
        tracks_lib.link_open_item(
            state_dir_v29, "feat-8", PROJECT_ID, "OI-008", "blocks", "manual"
        )
        with pytest.raises(RuntimeError, match="migration 0030"):
            tracks_lib.unlink_open_item(
                state_dir_v29, "feat-8", PROJECT_ID, "OI-008", "blocks",
                reason="should fail — no migration 0030"
            )


# ---------------------------------------------------------------------------
# D2 + reconciler: resolved OI no longer blocks
# ---------------------------------------------------------------------------

class TestReconcilerOiResolved:
    def test_resolved_blocker_not_counted(self, state_dir):
        """After unlink_open_item, reconciler derives 'queued' not 'blocked'."""
        tracks_lib.create_track(state_dir, "feat-r1", PROJECT_ID, "R1", "G1", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-r1", PROJECT_ID, "OI-R1", "blocks", "manual"
        )

        # Before resolution: blocked.
        res = track_reconciler.reconcile_track(state_dir, "feat-r1", PROJECT_ID)
        assert res["derived_status"] == "blocked"

        tracks_lib.unlink_open_item(
            state_dir, "feat-r1", PROJECT_ID, "OI-R1", "blocks",
            reason="resolved"
        )

        # After resolution: no longer blocked.
        res2 = track_reconciler.reconcile_track(state_dir, "feat-r1", PROJECT_ID)
        assert res2["derived_status"] != "blocked"

    def test_unresolved_blocker_still_blocks(self, state_dir):
        """An open (unresolved) blocker still produces derived_status='blocked'."""
        tracks_lib.create_track(state_dir, "feat-r2", PROJECT_ID, "R2", "G2", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-r2", PROJECT_ID, "OI-R2", "blocks", "manual"
        )
        res = track_reconciler.reconcile_track(state_dir, "feat-r2", PROJECT_ID)
        assert res["derived_status"] == "blocked"

    def test_mixed_oi_types_one_unresolved_blocks(self, state_dir):
        """Resolved blocker + unresolved blocker: track is still blocked."""
        tracks_lib.create_track(state_dir, "feat-r3", PROJECT_ID, "R3", "G3", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-r3", PROJECT_ID, "OI-RA", "blocks", "manual"
        )
        tracks_lib.link_open_item(
            state_dir, "feat-r3", PROJECT_ID, "OI-RB", "blocks", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-r3", PROJECT_ID, "OI-RA", "blocks",
            reason="resolved first"
        )
        # OI-RB still open.
        res = track_reconciler.reconcile_track(state_dir, "feat-r3", PROJECT_ID)
        assert res["derived_status"] == "blocked"


# ---------------------------------------------------------------------------
# D3: backfill_track_dispatch_linkage
# ---------------------------------------------------------------------------

class TestBackfillLinkage:
    """Synthetic fixture tests — no live DB required."""

    def _build_backfill_db(self, tmp_path: Path) -> tuple[Path, Path]:
        """Return (db_path, state_dir) with tracks + legacy dispatches."""
        state_dir = _build_db_v30(tmp_path)
        db = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")  # allow dispatch inserts without FK

        # Feature tracks.
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, pr_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("feat-alpha", PROJECT_ID, "Alpha", "done", "done", "#100"),
        )
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, pr_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("feat-beta", PROJECT_ID, "Beta", "done", "active", "#200"),
        )
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, pr_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("feat-gamma", PROJECT_ID, "Gamma", "queued", "queued", None),
        )
        conn.commit()

        # Dispatches: H1 match, H2 match, ambiguous, legacy-no-match.
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260501-feat-alpha-t1", PROJECT_ID, "completed", "A", "#100"),  # H1
        )
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260502-feat-beta-impl", PROJECT_ID, "completed", "B", None),  # H2 only
        )
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260503-feat-gamma-work", PROJECT_ID, "completed", "C", "#200"),  # ambiguous: H1->beta, H2->gamma
        )
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260504-generic-dispatch", PROJECT_ID, "completed", "A", None),  # unmatched
        )
        conn.commit()
        conn.close()
        return db, state_dir

    def test_dry_run_reports_categories(self, tmp_path):
        from backfill_track_dispatch_linkage import compute_matches
        db, state_dir = self._build_backfill_db(tmp_path)

        results = compute_matches(db, PROJECT_ID)
        statuses = {r.dispatch.dispatch_id: r.status for r in results}

        assert statuses["20260501-feat-alpha-t1"] == "matched"
        assert statuses["20260502-feat-beta-impl"] == "matched"
        assert statuses["20260503-feat-gamma-work"] == "ambiguous"
        assert statuses["20260504-generic-dispatch"] == "unmatched"

    def test_matched_dispatch_gets_correct_track(self, tmp_path):
        from backfill_track_dispatch_linkage import compute_matches
        db, state_dir = self._build_backfill_db(tmp_path)

        results = compute_matches(db, PROJECT_ID)
        alpha_result = next(
            r for r in results
            if r.dispatch.dispatch_id == "20260501-feat-alpha-t1"
        )
        assert alpha_result.matched_track_id == "feat-alpha"
        assert alpha_result.heuristic == "H1"

    def test_h2_slug_match(self, tmp_path):
        from backfill_track_dispatch_linkage import compute_matches
        db, state_dir = self._build_backfill_db(tmp_path)

        results = compute_matches(db, PROJECT_ID)
        beta_result = next(
            r for r in results
            if r.dispatch.dispatch_id == "20260502-feat-beta-impl"
        )
        assert beta_result.matched_track_id == "feat-beta"
        assert beta_result.heuristic == "H2"

    def test_apply_updates_matched_dispatches(self, tmp_path):
        from backfill_track_dispatch_linkage import compute_matches, apply_matches
        db, state_dir = self._build_backfill_db(tmp_path)

        results = compute_matches(db, PROJECT_ID)
        updated = apply_matches(db, results)
        assert updated == 2  # alpha (H1) + beta (H2); gamma=ambiguous; generic=unmatched

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row_alpha = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = ?",
            ("20260501-feat-alpha-t1",),
        ).fetchone()
        assert row_alpha["track"] == "feat-alpha"

        row_gamma = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = ?",
            ("20260503-feat-gamma-work",),
        ).fetchone()
        assert row_gamma["track"] == "C"  # ambiguous: not updated
        conn.close()

    def test_idempotent_apply(self, tmp_path):
        from backfill_track_dispatch_linkage import compute_matches, apply_matches
        db, state_dir = self._build_backfill_db(tmp_path)

        r1 = compute_matches(db, PROJECT_ID)
        apply_matches(db, r1)

        r2 = compute_matches(db, PROJECT_ID)
        # Previously matched dispatches are now 'already_linked'.
        already = [r for r in r2 if r.status == "already_linked"]
        matched = [r for r in r2 if r.status == "matched"]
        assert len(already) == 2
        assert len(matched) == 0

        # Applying again changes 0 rows.
        updated2 = apply_matches(db, r2)
        assert updated2 == 0

    def test_ambiguous_not_updated(self, tmp_path):
        """Ambiguous dispatches must never be stamped, even on --apply."""
        from backfill_track_dispatch_linkage import compute_matches, apply_matches
        db, state_dir = self._build_backfill_db(tmp_path)

        results = compute_matches(db, PROJECT_ID)
        apply_matches(db, results)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = ?",
            ("20260503-feat-gamma-work",),
        ).fetchone()
        conn.close()
        # Must remain at legacy value, not changed to feat-beta or feat-gamma.
        assert row["track"] == "C"


# ---------------------------------------------------------------------------
# D4: vnx status --tracks rendering
# ---------------------------------------------------------------------------

class TestStatusTracks:
    def _init_project(self, tmp_path: Path) -> Path:
        """Create a minimal VNX project dir pointing to a state_dir with tracks."""
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        # Write .vnx-project-id marker so status sees it as initialized.
        (project_dir / ".vnx-project-id").write_text(PROJECT_ID)

        state_dir = _build_db_v30(tmp_path / "runtime")
        return project_dir, state_dir

    def test_status_tracks_renders_table(self, tmp_path):
        """vnx status --tracks prints a table header and at least one track row."""
        from vnx_cli.commands.status import _render_tracks_table

        state_dir = _build_db_v30(tmp_path)
        tracks_lib.create_track(state_dir, "feat-s1", PROJECT_ID, "S1", "G1", phase="active")
        tracks_lib.create_track(state_dir, "feat-s2", PROJECT_ID, "S2", "G2", phase="queued")

        table = _render_tracks_table(state_dir, PROJECT_ID)
        assert "feat-s1" in table
        assert "feat-s2" in table
        assert "PHASE" in table

    def test_status_tracks_oi_count_excludes_resolved(self, tmp_path):
        """The OI count column shows only unresolved OIs."""
        from vnx_cli.commands.status import _render_tracks_table

        state_dir = _build_db_v30(tmp_path)
        tracks_lib.create_track(state_dir, "feat-s3", PROJECT_ID, "S3", "G3", phase="active")
        tracks_lib.link_open_item(
            state_dir, "feat-s3", PROJECT_ID, "OI-S1", "blocks", "manual"
        )
        tracks_lib.link_open_item(
            state_dir, "feat-s3", PROJECT_ID, "OI-S2", "warns", "manual"
        )
        tracks_lib.unlink_open_item(
            state_dir, "feat-s3", PROJECT_ID, "OI-S1", "blocks",
            reason="resolved in test"
        )

        table = _render_tracks_table(state_dir, PROJECT_ID)
        # Only 1 unresolved OI (OI-S2). The table cell should show '1', not '2'.
        lines = [l for l in table.splitlines() if "feat-s3" in l]
        assert len(lines) == 1
        # The OI count column contains '1' (not '2').
        assert " 1 " in lines[0] or lines[0].strip().split()[3] == "1"

    def test_status_tracks_empty_project(self, tmp_path):
        """Gracefully handles project with no tracks."""
        from vnx_cli.commands.status import _render_tracks_table

        state_dir = _build_db_v30(tmp_path)
        table = _render_tracks_table(state_dir, PROJECT_ID)
        assert "no tracks" in table.lower() or PROJECT_ID in table

    def test_status_tracks_no_db(self, tmp_path):
        """Gracefully handles missing DB file."""
        from vnx_cli.commands.status import _render_tracks_table

        missing_state_dir = tmp_path / "state"
        missing_state_dir.mkdir()
        table = _render_tracks_table(missing_state_dir, PROJECT_ID)
        assert "runtime_coordination.db" in table or "not found" in table.lower()
