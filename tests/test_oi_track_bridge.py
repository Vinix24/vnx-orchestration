"""tests/test_oi_track_bridge.py — behavioral tests for the OI→track bridge (PR-C).

Covers the R4 contracts of scripts/import_open_items_to_tracks.py:
  R4.1 (D3)   — DB-authoritative, at-most-once events: all mutations COMMIT, then
                events emit; a post-commit emit failure is logged + non-fatal
                (DB persists, ledger_failed, CLI exit 4). A DB/validation error
                rolls the whole run back (run-level DB atomicity) and resets the
                mutation counters (C-N3); the reconciler recovers a missing event.
  R4.2        — load ALL links by (project_id, oi_id) + supersede/close obsolete
  R4.3 / R8.3 — require 0030 resolution schema; pre-0030 fails (raise + CLI exit 5)
  R4.4        — idempotent (run twice → identical track_open_items)
  R8.1        — reopen invariant (open→close→open: resolved_at NULL + reopen event)
  C-N1        — malformed/absent source fails LOUD (exit 3), never silent-empty
  plus: mapping (pr_id→pr_ref / explicit track), disk-loading, reconciler tie-in.

All DBs are temp (tmp_path); the conftest pins VNX_DATA_DIR_EXPLICIT=1 + tmp
VNX_DATA_DIR so nothing touches the live ~/.vnx-data store (PR-0 guard).

ADR-007: every track_open_items access is (track_id, project_id)-scoped.
ADR-005: mutations carry NDJSON ledger events (asserted via track_events.ndjson).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import schema_migration
import tracks as tracks_lib
import track_reconciler
import import_open_items_to_tracks as bridge

PROJECT_ID = "test-proj"


# ---------------------------------------------------------------------------
# DB fixtures (mirror tests/test_track_oi_lifecycle.py)
# ---------------------------------------------------------------------------

def _build_db_v29(tmp_path: Path) -> Path:
    """State_dir with migrations 0022, 0024, 0027, 0028, 0029 (pre-0030)."""
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
            event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT,
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
    """State_dir with all migrations including 0030 (resolution schema present)."""
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
# Helpers
# ---------------------------------------------------------------------------

def _mk_track(state_dir, track_id, *, pr_ref=None, phase="active"):
    tracks_lib.create_track(state_dir, track_id, PROJECT_ID, track_id, "goal",
                            phase=phase, pr_ref=pr_ref)


def _oi(oi_id, *, severity="blocker", status="open", pr_id=None, track_id=None):
    item = {"id": oi_id, "severity": severity, "status": status, "title": oi_id}
    if pr_id is not None:
        item["pr_id"] = pr_id
    if track_id is not None:
        item["track_id"] = track_id
    return item


def _rows(state_dir, oi_id):
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT track_id, link_type, link_source, resolved_at, resolution_reason "
        "FROM track_open_items WHERE project_id = ? AND oi_id = ? "
        "ORDER BY track_id, link_type", (PROJECT_ID, oi_id),
    )]
    conn.close()
    return rows


def _active_blocks(state_dir):
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    n = conn.execute(
        "SELECT COUNT(*) FROM track_open_items WHERE project_id = ? "
        "AND link_type = 'blocks' AND resolved_at IS NULL", (PROJECT_ID,),
    ).fetchone()[0]
    conn.close()
    return n


def _events(state_dir, event_type):
    f = state_dir.parent / "events" / "track_events.ndjson"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("event_type") == event_type:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# R4.3 / R8.3 — require resolution schema (pre-0030 fails)
# ---------------------------------------------------------------------------

class TestRequireSchema:
    def test_pre_0030_raises(self, state_dir_v29):
        _mk_track(state_dir_v29, "feat-a", pr_ref="#100")
        with pytest.raises(bridge.BridgePreconditionError, match="0030"):
            bridge.import_open_items_to_tracks(
                state_dir_v29, PROJECT_ID,
                open_items=[_oi("OI-1", pr_id="#100")],
            )

    def test_pre_0030_never_mutates(self, state_dir_v29):
        """Schema failure happens before any track_open_items write."""
        _mk_track(state_dir_v29, "feat-a", pr_ref="#100")
        with pytest.raises(bridge.BridgePreconditionError):
            bridge.import_open_items_to_tracks(
                state_dir_v29, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
            )
        # No rows written (query without the absent resolved_at column).
        db = state_dir_v29 / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        n = conn.execute(
            "SELECT COUNT(*) FROM track_open_items WHERE project_id = ? AND oi_id = ?",
            (PROJECT_ID, "OI-1"),
        ).fetchone()[0]
        conn.close()
        assert n == 0

    def test_guard_raises_when_resolution_reason_absent(self):
        """Partial schema (resolved_at present, resolution_reason absent) raises (R8.3)."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, "
            "oi_id TEXT, resolved_at TEXT)"
        )
        with pytest.raises(bridge.BridgePreconditionError, match="resolution_reason"):
            bridge._require_resolution_schema(conn)
        conn.close()

    def test_guard_raises_when_both_columns_absent(self):
        """Both resolution columns absent raises (R8.3 — explicit, no catch-all)."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT)"
        )
        with pytest.raises(bridge.BridgePreconditionError, match="resolved_at"):
            bridge._require_resolution_schema(conn)
        conn.close()

    def test_guard_passes_when_schema_complete(self):
        """The guard does NOT raise when both 0030 columns exist (branch coverage)."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, "
            "oi_id TEXT, resolved_at TEXT, resolution_reason TEXT)"
        )
        bridge._require_resolution_schema(conn)  # must not raise
        conn.close()

    def test_cli_exit_5_on_pre_0030(self, state_dir_v29):
        _mk_track(state_dir_v29, "feat-a", pr_ref="#100")
        code = bridge.main([
            "--project-id", PROJECT_ID, "--state-dir", str(state_dir_v29),
        ])
        assert code == bridge.EXIT_SCHEMA_PRECONDITION == 5


# ---------------------------------------------------------------------------
# Mapping + basic link creation
# ---------------------------------------------------------------------------

class TestMapping:
    def test_pr_id_maps_to_track_pr_ref(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
        )
        assert res.linked == 1 and res.ok
        rows = _rows(state_dir, "OI-1")
        assert len(rows) == 1
        assert rows[0]["track_id"] == "feat-a"
        assert rows[0]["link_type"] == "blocks"
        assert rows[0]["resolved_at"] is None

    def test_severity_maps_link_type(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID,
            open_items=[_oi("OI-w", severity="warn", pr_id="#100")],
        )
        assert _rows(state_dir, "OI-w")[0]["link_type"] == "warns"

    def test_explicit_track_field_wins(self, state_dir):
        _mk_track(state_dir, "feat-x", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", track_id="feat-x")],
        )
        assert _rows(state_dir, "OI-1")[0]["track_id"] == "feat-x"

    def test_unmappable_open_oi_recorded(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-z", pr_id="#999")],
        )
        assert "OI-z" in res.unmappable
        assert _rows(state_dir, "OI-z") == []

    def test_ambiguous_pr_is_unmappable(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b", pr_ref="#100")
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
        )
        assert "OI-1" in res.unmappable
        assert _rows(state_dir, "OI-1") == []

    def test_loads_open_items_from_disk(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text(json.dumps(
            {"items": [_oi("OI-disk", pr_id="#100")]}), encoding="utf-8")
        res = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)
        assert res.linked == 1
        assert _rows(state_dir, "OI-disk")[0]["track_id"] == "feat-a"


# ---------------------------------------------------------------------------
# R4.2 — load ALL links + supersede obsolete (remap + closure)
# ---------------------------------------------------------------------------

class TestSupersede:
    def test_remap_resolves_old_link(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b", pr_ref="#200")
        # Initial mapping: OI-1 → feat-a.
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
        )
        # Remap: OI-1's PR now points at feat-b.
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#200")],
        )
        assert res.unlinked == 1 and res.linked == 1
        rows = {r["track_id"]: r for r in _rows(state_dir, "OI-1")}
        assert rows["feat-a"]["resolved_at"] is not None  # old link closed
        assert rows["feat-b"]["resolved_at"] is None       # new link active
        # No stale active blocks left on feat-a.
        assert _active_blocks(state_dir) == 1

    def test_closure_when_oi_no_longer_open(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
        )
        # OI closed upstream → no current mapping → existing link resolved.
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID,
            open_items=[_oi("OI-1", status="done", pr_id="#100")],
        )
        assert res.unlinked == 1
        assert _rows(state_dir, "OI-1")[0]["resolved_at"] is not None
        assert _active_blocks(state_dir) == 0

    def test_loads_all_links_supersedes_every_obsolete(self, state_dir):
        """Seed TWO active links for one OI; bridge closes every non-desired one."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b")
        _mk_track(state_dir, "feat-c")
        tracks_lib.link_open_item(state_dir, "feat-b", PROJECT_ID, "OI-1", "blocks", "manual")
        tracks_lib.link_open_item(state_dir, "feat-c", PROJECT_ID, "OI-1", "blocks", "manual")
        # Desired (via PR) is feat-a; feat-b and feat-c are obsolete.
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
        )
        assert res.unlinked == 2 and res.linked == 1
        rows = {r["track_id"]: r for r in _rows(state_dir, "OI-1")}
        assert rows["feat-b"]["resolved_at"] is not None
        assert rows["feat-c"]["resolved_at"] is not None
        assert rows["feat-a"]["resolved_at"] is None
        assert _active_blocks(state_dir) == 1

    def test_unmappable_open_oi_closes_existing(self, state_dir):
        """An open OI that becomes unmappable still has its active links closed."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")],
        )
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#404")],
        )
        assert res.unlinked == 1
        assert _active_blocks(state_dir) == 0


# ---------------------------------------------------------------------------
# R4.4 — idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_run_twice_identical(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        items = [_oi("OI-1", pr_id="#100")]
        r1 = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID, open_items=items)
        snap1 = _rows(state_dir, "OI-1")
        r2 = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID, open_items=items)
        snap2 = _rows(state_dir, "OI-1")
        assert r1.linked == 1
        assert r2.linked == 0 and r2.skipped == 1  # second run is a no-op
        assert snap1 == snap2  # identical rows, no duplicates
        assert len(snap2) == 1

    def test_no_integrity_error_on_repeat(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        items = [_oi("OI-1", pr_id="#100"), _oi("OI-2", severity="warn", pr_id="#100")]
        for _ in range(3):
            res = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID, open_items=items)
            assert res.ok
        assert len(_rows(state_dir, "OI-1")) == 1
        assert len(_rows(state_dir, "OI-2")) == 1


# ---------------------------------------------------------------------------
# R8.1 — reopen invariant
# ---------------------------------------------------------------------------

class TestReopen:
    def test_open_close_open_clears_resolved_at(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        # open
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        # close
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID,
            open_items=[_oi("OI-1", status="done", pr_id="#100")])
        assert _rows(state_dir, "OI-1")[0]["resolved_at"] is not None
        # reopen
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        assert res.reopened == 1
        row = _rows(state_dir, "OI-1")[0]
        assert row["resolved_at"] is None  # cleared again
        assert row["resolution_reason"] is None

    def test_reopen_emits_event(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID,
            open_items=[_oi("OI-1", status="done", pr_id="#100")])
        before = len(_events(state_dir, "track_oi_reopened"))
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        after = _events(state_dir, "track_oi_reopened")
        assert len(after) == before + 1
        assert after[-1]["details"]["oi_id"] == "OI-1"


# ---------------------------------------------------------------------------
# R4.1 (D3 deviation) — post-commit emit failure: DB persists, logged, exit 4
# ---------------------------------------------------------------------------

class TestLedgerFailure:
    def test_fresh_link_emit_failure_db_committed(self, state_dir, monkeypatch):
        """D3: events emit AFTER commit, so an emit failure leaves the DB row PRESENT
        (committed) + ledger_failed + exit 4 — NOT zero-changes."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")

        def _boom(*a, **k):
            raise RuntimeError("ledger boom")

        monkeypatch.setattr(tracks_lib, "_emit_track_event", _boom)
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        assert res.ledger_failed is True
        assert res.errors and "ledger emit failed" in res.errors[0]
        assert res.exit_code == bridge.EXIT_LEDGER_FAILURE == 4
        # DB is authoritative: the link IS committed despite the emit failure.
        rows = _rows(state_dir, "OI-1")
        assert len(rows) == 1
        assert rows[0]["track_id"] == "feat-a"
        assert rows[0]["resolved_at"] is None

    def test_remap_emit_failure_db_committed(self, state_dir, monkeypatch):
        """D3: a remap whose post-commit emit fails STILL commits the remap
        (feat-a resolved, feat-b active) + ledger_failed + exit 4."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b", pr_ref="#200")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])

        def _boom(*a, **k):
            raise RuntimeError("ledger boom")

        monkeypatch.setattr(tracks_lib, "_emit_track_event", _boom)
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#200")])
        assert res.ledger_failed is True
        assert res.exit_code == 4
        # The remap committed: feat-a closed, feat-b active (DB authoritative).
        rows = {r["track_id"]: r for r in _rows(state_dir, "OI-1")}
        assert rows["feat-a"]["resolved_at"] is not None
        assert rows["feat-b"]["resolved_at"] is None
        assert _active_blocks(state_dir) == 1

    def test_cli_exit_4_on_emit_failure_row_persists(self, state_dir, monkeypatch):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text(json.dumps(
            {"items": [_oi("OI-1", pr_id="#100")]}), encoding="utf-8")

        def _boom(*a, **k):
            raise RuntimeError("ledger boom")

        monkeypatch.setattr(tracks_lib, "_emit_track_event", _boom)
        code = bridge.main(["--project-id", PROJECT_ID, "--state-dir", str(state_dir)])
        assert code == 4
        # D3: exit 4 (ledger-emit-warning) but the DB mutation persists.
        assert _rows(state_dir, "OI-1")[0]["track_id"] == "feat-a"


# ---------------------------------------------------------------------------
# Reconciler tie-in — bridge state drives derived_status (proves the bridge works)
# ---------------------------------------------------------------------------

class TestReconcilerTieIn:
    def test_bridge_blocker_makes_track_blocked(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        res = track_reconciler.reconcile_track(state_dir, "feat-a", PROJECT_ID)
        assert res["derived_status"] == "blocked"

    def test_bridge_closure_unblocks_track(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID,
            open_items=[_oi("OI-1", status="done", pr_id="#100")])
        res = track_reconciler.reconcile_track(state_dir, "feat-a", PROJECT_ID)
        assert res["derived_status"] != "blocked"


# ---------------------------------------------------------------------------
# CLI happy path — exit 0 + --open-items file branch
# ---------------------------------------------------------------------------

class TestCliHappyPath:
    def test_cli_exit_0_links_from_file(self, state_dir, tmp_path):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        oi_file = tmp_path / "items.json"
        oi_file.write_text(json.dumps({"items": [_oi("OI-1", pr_id="#100")]}),
                           encoding="utf-8")
        code = bridge.main([
            "--project-id", PROJECT_ID, "--state-dir", str(state_dir),
            "--open-items", str(oi_file),
        ])
        assert code == bridge.EXIT_OK == 0
        assert _rows(state_dir, "OI-1")[0]["track_id"] == "feat-a"


# ---------------------------------------------------------------------------
# C-N1 — fail-loud on absent OI source (never close all links from a missing file)
# ---------------------------------------------------------------------------

class TestSourceFailLoud:
    def test_missing_source_raises_zero_closures(self, state_dir):
        """ABSENT on-disk source → explicit error + ZERO link closures (destructive guard)."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        assert _active_blocks(state_dir) == 1
        assert not (state_dir / "open_items.json").exists()
        with pytest.raises(bridge.BridgeSourceError):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)  # None → disk load
        # Fail-loud must NOT close any existing link.
        assert _active_blocks(state_dir) == 1
        assert _rows(state_dir, "OI-1")[0]["resolved_at"] is None

    def test_unreadable_source_raises(self, state_dir):
        """A present-but-corrupt (invalid JSON) source also fails loud, not [] ."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(bridge.BridgeSourceError):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)

    def test_malformed_scalar_source_raises_zero_closures(self, state_dir):
        """C2-N1: a parseable scalar (42) is WRONG-SHAPE → fail loud, NO closures."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        assert _active_blocks(state_dir) == 1
        (state_dir / "open_items.json").write_text("42", encoding="utf-8")
        with pytest.raises(bridge.BridgeSourceError, match="malformed"):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)
        # Wrong-shape must NOT silent-empty → no active link closed.
        assert _active_blocks(state_dir) == 1
        assert _rows(state_dir, "OI-1")[0]["resolved_at"] is None

    def test_malformed_dict_without_items_raises(self, state_dir):
        """C2-N1: a dict that loads but lacks an 'items' key is wrong-shape."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        (state_dir / "open_items.json").write_text(
            json.dumps({"schema_version": "1.0", "next_id": 1}), encoding="utf-8")
        with pytest.raises(bridge.BridgeSourceError, match="items"):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)
        assert _active_blocks(state_dir) == 1  # destructive guard held

    def test_malformed_items_not_a_list_raises(self, state_dir):
        """C2-N1: {"items": <non-list>} is wrong-shape → fail loud."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text(
            json.dumps({"items": "nope"}), encoding="utf-8")
        with pytest.raises(bridge.BridgeSourceError, match="list"):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)

    def test_malformed_list_with_non_object_raises(self, state_dir):
        """C2-N1: a list whose elements are not objects is wrong-shape."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text(
            json.dumps(["OI-1", "OI-2"]), encoding="utf-8")
        with pytest.raises(bridge.BridgeSourceError, match="object"):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)

    def test_bare_list_form_is_legitimate(self, state_dir):
        """A top-level list of item objects is an accepted shape (not malformed)."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text(
            json.dumps([_oi("OI-1", pr_id="#100")]), encoding="utf-8")
        res = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)
        assert res.linked == 1
        assert _rows(state_dir, "OI-1")[0]["track_id"] == "feat-a"

    def test_cli_exit_3_on_malformed_source(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text("42", encoding="utf-8")
        code = bridge.main(["--project-id", PROJECT_ID, "--state-dir", str(state_dir)])
        assert code == bridge.EXIT_SOURCE_MISSING == 3

    def test_present_empty_store_is_legitimate(self, state_dir):
        """A PRESENT but empty store IS a legitimate empty desired state (closes obsolete)."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        (state_dir / "open_items.json").write_text(
            json.dumps({"items": []}), encoding="utf-8")
        res = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID)
        assert res.unlinked == 1
        assert _active_blocks(state_dir) == 0

    def test_cli_exit_3_on_missing_source(self, state_dir):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        assert not (state_dir / "open_items.json").exists()
        code = bridge.main(["--project-id", PROJECT_ID, "--state-dir", str(state_dir)])
        assert code == bridge.EXIT_SOURCE_MISSING == 3


# ---------------------------------------------------------------------------
# Run-level DB atomicity — a DB error on a NON-FIRST item rolls back ALL mutations
# ---------------------------------------------------------------------------

class TestRunLevelAtomicity:
    def test_later_item_db_error_rolls_back_all(self, state_dir, monkeypatch):
        """DB error on item 2 → run-level rollback → ZERO net track_open_items
        changes (the earlier item's mutation is undone too), propagated by type."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b", pr_ref="#200")
        items = [_oi("OI-1", pr_id="#100"), _oi("OI-2", pr_id="#200")]

        real_link = tracks_lib.link_open_item
        calls = {"n": 0}

        def _link(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:  # item 1 mutates in-tx, item 2's DB op fails
                raise sqlite3.OperationalError("db boom on later item")
            return real_link(*a, **k)

        monkeypatch.setattr(tracks_lib, "link_open_item", _link)
        with pytest.raises(sqlite3.OperationalError):
            bridge.import_open_items_to_tracks(state_dir, PROJECT_ID, open_items=items)
        # Run-level rollback: NO rows committed for either item.
        assert _rows(state_dir, "OI-1") == []
        assert _rows(state_dir, "OI-2") == []
        assert _active_blocks(state_dir) == 0


# ---------------------------------------------------------------------------
# C2-N2 (D3) — post-commit emit failure: DB authoritative, reconcile compensates
# ---------------------------------------------------------------------------

class TestPostCommitEmit:
    def test_later_item_emit_failure_all_committed(self, state_dir, monkeypatch):
        """Emit succeeds on item 1, FAILS on item 2 — but both DB mutations are
        already committed (D3): the DB is authoritative, exit 4, no rollback."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b", pr_ref="#200")
        items = [_oi("OI-1", pr_id="#100"), _oi("OI-2", pr_id="#200")]

        real_emit = tracks_lib._emit_track_event
        calls = {"n": 0}

        def _emit(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:  # commit already happened; later event fails
                raise RuntimeError("ledger boom on later item")
            return real_emit(*a, **k)

        monkeypatch.setattr(tracks_lib, "_emit_track_event", _emit)
        res = bridge.import_open_items_to_tracks(state_dir, PROJECT_ID, open_items=items)
        assert res.ledger_failed is True
        assert res.exit_code == bridge.EXIT_LEDGER_FAILURE == 4
        # D3: both mutations persist (committed before emit), no run-level rollback.
        assert _rows(state_dir, "OI-1")[0]["track_id"] == "feat-a"
        assert _rows(state_dir, "OI-2")[0]["track_id"] == "feat-b"
        assert _active_blocks(state_dir) == 2

    def test_post_commit_emit_failure_reconcile_recovers(self, state_dir, monkeypatch):
        """The instruction's D3 acceptance test: a post-commit emit failure leaves
        the DB committed (exit 4), and a follow-up reconcile derives the correct
        derived_status from track_open_items (the missing event is recoverable)."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")

        def _boom(*a, **k):
            raise RuntimeError("ledger boom")

        monkeypatch.setattr(tracks_lib, "_emit_track_event", _boom)
        res = bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        assert res.ledger_failed is True and res.exit_code == 4
        # DB mutation persisted despite the emit failure.
        assert _rows(state_dir, "OI-1")[0]["resolved_at"] is None
        # Reconcile re-derives from track_open_items — recovers the correct status.
        monkeypatch.undo()  # restore real emit for the reconciler
        rec = track_reconciler.reconcile_track(state_dir, "feat-a", PROJECT_ID)
        assert rec["derived_status"] == "blocked"


# ---------------------------------------------------------------------------
# C2-N3 — mutation counters reset on a run-level rollback (count only committed)
# ---------------------------------------------------------------------------

class TestCounterResetOnRollback:
    def test_counters_reset_after_run_level_rollback(self, state_dir, monkeypatch):
        """Item 1 increments linked, item 2's DB op fails → run-level rollback.
        The in-place result counters MUST reset to zero (only committed counts)."""
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        _mk_track(state_dir, "feat-b", pr_ref="#200")
        items = [_oi("OI-1", pr_id="#100"), _oi("OI-2", pr_id="#200")]

        conn = bridge._open_conn(state_dir)
        try:
            bridge._require_resolution_schema(conn)
            result = bridge.BridgeResult(project_id=PROJECT_ID)
            by_pr, track_ids = bridge._load_tracks_by_pr(conn, PROJECT_ID)
            existing = bridge._load_links_grouped(conn, PROJECT_ID)
            desired = bridge._build_desired(items, by_pr, track_ids, result)
            all_ids = sorted(set(desired) | set(existing))

            real_link = tracks_lib.link_open_item
            calls = {"n": 0}

            def _link(*a, **k):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise sqlite3.OperationalError("db boom on item 2")
                return real_link(*a, **k)

            monkeypatch.setattr(tracks_lib, "link_open_item", _link)
            with pytest.raises(sqlite3.OperationalError):
                bridge._run_mutations(
                    state_dir, conn, PROJECT_ID, all_ids, desired, existing,
                    "mention", result,
                )
            # C-N3: item 1 had incremented linked to 1; the rollback resets it.
            assert result.linked == 0
            assert result.unlinked == 0
            assert result.reopened == 0
            assert result.skipped == 0
        finally:
            conn.close()
        # DB confirms the run-level rollback: nothing committed.
        assert _rows(state_dir, "OI-1") == []
        assert _rows(state_dir, "OI-2") == []


# ---------------------------------------------------------------------------
# C-N3 — reopen event emitted AFTER the mutation (no orphan on mutation failure)
# ---------------------------------------------------------------------------

class TestReopenOrdering:
    def test_mutation_failure_leaves_no_orphan_reopen_event(self, state_dir, monkeypatch):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        # open then close → a resolved link exists; next open is a REOPEN.
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        bridge.import_open_items_to_tracks(
            state_dir, PROJECT_ID, open_items=[_oi("OI-1", status="done", pr_id="#100")])
        before = len(_events(state_dir, "track_oi_reopened"))

        def _boom_link(*a, **k):
            raise sqlite3.OperationalError("mutation boom")

        monkeypatch.setattr(tracks_lib, "link_open_item", _boom_link)
        with pytest.raises(sqlite3.OperationalError):
            bridge.import_open_items_to_tracks(
                state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])
        # Reopen event is emitted AFTER the mutation → a mutation failure orphans nothing.
        assert len(_events(state_dir, "track_oi_reopened")) == before


# ---------------------------------------------------------------------------
# C-N4 — exception classification (DB error is NOT misclassified as ledger/exit 4)
# ---------------------------------------------------------------------------

class TestExceptionClassification:
    def test_db_error_propagates_with_own_type(self, state_dir, monkeypatch):
        _mk_track(state_dir, "feat-a", pr_ref="#100")

        def _boom_link(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(tracks_lib, "link_open_item", _boom_link)
        # A DB error must surface as sqlite3.Error with its own type (CLI exit 6),
        # NOT misclassified as a ledger-emit warning (exit 4).
        with pytest.raises(sqlite3.OperationalError):
            bridge.import_open_items_to_tracks(
                state_dir, PROJECT_ID, open_items=[_oi("OI-1", pr_id="#100")])

    def test_cli_db_error_exit_is_6_not_4(self, state_dir, monkeypatch):
        _mk_track(state_dir, "feat-a", pr_ref="#100")
        (state_dir / "open_items.json").write_text(
            json.dumps({"items": [_oi("OI-1", pr_id="#100")]}), encoding="utf-8")

        def _boom_link(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(tracks_lib, "link_open_item", _boom_link)
        code = bridge.main(["--project-id", PROJECT_ID, "--state-dir", str(state_dir)])
        assert code != bridge.EXIT_LEDGER_FAILURE  # not misclassified as ledger
        assert code == bridge.EXIT_DB_ERROR == 6
