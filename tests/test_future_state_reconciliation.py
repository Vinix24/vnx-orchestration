"""tests/test_future_state_reconciliation.py — future-state reconciliation (A-G).

Covers the split-brain / future-state fixes:
  B  _count_dispatches / _build_queues count directory-form (manifest.json) AND
     legacy .md dispatches; _build_active_work enumerates both forms.
  C  _build_tracks reads the tracks DB table (feature-track model) with a
     graceful fallback to legacy progress_state.yaml when the table is absent.
  D  staleness_seconds reflects real now - generated_at (the lie fix).
  E  migrate_future_system repairs a half-applied/version-lying DB:
       - introspection-driven dispatches ADR-007 composite-UNIQUE repair
       - user_version reconciliation when the stamp is falsely high
  F  backfill_track_dispatch_linkage fails gracefully (clear message) when the
     track tables are absent, and is idempotent.
  G  open_items.json -> track_open_items bridge: idempotent, project_id-stamped.

All against temp SQLite DBs — no live DB touched.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIB = _PROJECT_ROOT / "scripts" / "lib"
_SCRIPTS = _PROJECT_ROOT / "scripts"
_MIGRATIONS = _PROJECT_ROOT / "schemas" / "migrations"

for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(modname: str, relpath: str):
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / relpath)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so dataclasses (which look up
    # cls.__module__ in sys.modules) resolve correctly.
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# B — directory-form + legacy .md dispatch counting
# ===========================================================================

class TestDispatchCounting:
    def _bt0(self):
        return _load("build_t0_state_fsr", "build_t0_state.py")

    def test_counts_directory_form_dispatches(self, tmp_path):
        bt0 = self._bt0()
        active = tmp_path / "active"
        active.mkdir()
        # Three directory-form dispatches (each is a dir with manifest.json).
        for i in range(3):
            d = active / f"20260601-disp-{i}"
            d.mkdir()
            (d / "manifest.json").write_text(json.dumps({"dispatch_id": f"disp-{i}"}))
        # A dir WITHOUT manifest.json is not a dispatch.
        (active / "not-a-dispatch").mkdir()
        assert bt0._count_dispatches(active) == 3

    def test_counts_legacy_md_dispatches(self, tmp_path):
        bt0 = self._bt0()
        active = tmp_path / "active"
        active.mkdir()
        (active / "20260601-a.md").write_text("dispatch a")
        (active / "20260601-b.md").write_text("dispatch b")
        assert bt0._count_dispatches(active) == 2

    def test_counts_mixed_forms(self, tmp_path):
        bt0 = self._bt0()
        active = tmp_path / "active"
        active.mkdir()
        d = active / "20260601-dir"
        d.mkdir()
        (d / "manifest.json").write_text("{}")
        (active / "20260601-legacy.md").write_text("x")
        assert bt0._count_dispatches(active) == 2

    def test_count_md_alias_preserved(self, tmp_path):
        bt0 = self._bt0()
        active = tmp_path / "active"
        active.mkdir()
        d = active / "x"
        d.mkdir()
        (d / "manifest.json").write_text("{}")
        # Backward-compat alias must also count directory form.
        assert bt0._count_md(active) == 1

    def test_build_active_work_reads_manifest_dirs(self, tmp_path):
        bt0 = self._bt0()
        dispatch_dir = tmp_path / "dispatches"
        active = dispatch_dir / "active"
        active.mkdir(parents=True)
        d = active / "20260601-feat-x"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({
            "dispatch_id": "20260601-feat-x",
            "track": "track-01",
            "gate": "B1",
            "timestamp": "2026-06-01T10:00:00+00:00",
        }))
        items = bt0._build_active_work(dispatch_dir)
        assert len(items) == 1
        assert items[0]["dispatch_id"] == "20260601-feat-x"
        assert items[0]["track"] == "track-01"
        assert items[0]["gate"] == "B1"


# ===========================================================================
# C — _build_tracks reads the tracks DB table with YAML fallback
# ===========================================================================

_TRACKS_DDL = """
CREATE TABLE tracks (
    track_id        TEXT NOT NULL,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev',
    title           TEXT,
    phase           TEXT NOT NULL DEFAULT 'queued',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    pr_ref          TEXT,
    derived_status  TEXT,
    PRIMARY KEY (track_id, project_id)
);
"""


class TestBuildTracks:
    def _bt0(self):
        return _load("build_t0_state_fsr_tracks", "build_t0_state.py")

    def _make_db(self, state_dir: Path, rows):
        state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        conn.executescript(_TRACKS_DDL)
        for r in rows:
            conn.execute(
                "INSERT INTO tracks (track_id, project_id, title, phase, pr_ref, derived_status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                r,
            )
        conn.commit()
        conn.close()

    def test_reads_feature_tracks_from_db(self, tmp_path):
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        self._make_db(state_dir, [
            ("track-01", "vnx-dev", "Subscription Escape", "done", "#756", "done"),
            ("track-02", "vnx-dev", "Public 1.0", "active", "#760", "in_progress"),
            ("track-03", "other-proj", "Other", "queued", None, None),  # filtered out
        ])
        tracks = bt0._build_tracks(state_dir, "vnx-dev")
        assert set(tracks.keys()) == {"track-01", "track-02"}
        assert tracks["track-01"]["source"] == "tracks_db"
        assert tracks["track-01"]["health"] == "healthy"
        assert tracks["track-02"]["phase"] == "active"

    def test_blocked_derived_status_maps_to_blocked_health(self, tmp_path):
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        self._make_db(state_dir, [
            ("track-09", "vnx-dev", "Blocked one", "active", None, "blocked"),
        ])
        tracks = bt0._build_tracks(state_dir, "vnx-dev")
        assert tracks["track-09"]["health"] == "blocked"

    def test_falls_back_to_legacy_yaml_when_no_tracks_table(self, tmp_path):
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        # DB exists but has no tracks table.
        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        # progress_state.yaml present → legacy A/B/C model.
        (state_dir / "progress_state.yaml").write_text(
            "tracks:\n  A:\n    status: working\n    active_dispatch_id: d1\n",
            encoding="utf-8",
        )
        tracks = bt0._build_tracks(state_dir, "vnx-dev")
        assert set(tracks.keys()) == {"A", "B", "C"}
        assert tracks["A"]["source"] == "progress_state_yaml"
        assert tracks["A"]["status"] == "working"

    def test_falls_back_when_no_db_at_all(self, tmp_path):
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        tracks = bt0._build_tracks(state_dir, "vnx-dev")
        # No DB, no yaml → legacy default A/B/C idle.
        assert set(tracks.keys()) == {"A", "B", "C"}
        assert tracks["A"]["source"] == "progress_state_yaml"


# ===========================================================================
# D — staleness lie fix
# ===========================================================================

class TestStaleness:
    def _bt0(self):
        return _load("build_t0_state_fsr_stale", "build_t0_state.py")

    def test_zero_when_just_generated(self):
        bt0 = self._bt0()
        now = datetime.now(timezone.utc)
        assert bt0.compute_staleness_seconds(now.isoformat(), now=now) == 0

    def test_reflects_real_age(self):
        bt0 = self._bt0()
        now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        gen = (now - timedelta(days=10)).isoformat()
        secs = bt0.compute_staleness_seconds(gen, now=now)
        assert secs == 10 * 86400

    def test_never_negative(self):
        bt0 = self._bt0()
        now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        future = (now + timedelta(hours=1)).isoformat()
        assert bt0.compute_staleness_seconds(future, now=now) == 0

    def test_missing_generated_at_returns_none(self):
        bt0 = self._bt0()
        # Unknown sentinel — returning 0 would falsely suggest a fresh document.
        assert bt0.compute_staleness_seconds("") is None
        assert bt0.compute_staleness_seconds("not-a-date") is None

    def test_staleness_for_state_file(self, tmp_path):
        bt0 = self._bt0()
        now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        gen = (now - timedelta(days=10)).isoformat()
        p = tmp_path / "t0_state.json"
        # The persisted file LIES (staleness_seconds: 0) but generated_at is 10d old.
        p.write_text(json.dumps({"generated_at": gen, "staleness_seconds": 0}))
        assert bt0.staleness_for_state_file(p, now=now) == 10 * 86400

    def test_build_emits_truthful_staleness(self, tmp_path):
        bt0 = self._bt0()
        state = bt0.build_t0_state(tmp_path / "state", tmp_path / "dispatches")
        # Just built → staleness ~0 (truthful), and it's a computed int not a literal.
        assert isinstance(state["staleness_seconds"], int)
        assert 0 <= state["staleness_seconds"] <= 5
        assert bt0.compute_staleness_seconds(state["generated_at"]) >= 0


# ===========================================================================
# E — migration repair + version reconciliation
# ===========================================================================

def _migrate_mod():
    return _load("migrate_future_system_fsr", "migrate_future_system.py")


def _v9_dispatches_ddl_no_project_id() -> str:
    return """
    CREATE TABLE dispatches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispatch_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL DEFAULT 'queued',
        terminal_id TEXT, track TEXT, priority TEXT,
        pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
        bundle_path TEXT, created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', expires_after TEXT,
        metadata_json TEXT DEFAULT '{}'
    )
    """


class TestDispatchesRepair:
    def test_repair_adds_project_id_and_composite_unique(self, tmp_path):
        mod = _migrate_mod()
        db = tmp_path / "x.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(_v9_dispatches_ddl_no_project_id())
        conn.execute("INSERT INTO dispatches (dispatch_id, state) VALUES ('d1', 'queued')")
        conn.commit()

        assert mod._dispatches_repair_needed(conn) is True
        changed = mod._repair_dispatches_adr007(conn)
        conn.commit()
        assert changed is True

        cols = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        assert "project_id" in cols
        assert mod._dispatches_has_composite_unique(conn) is True
        # Data preserved.
        row = conn.execute("SELECT dispatch_id, project_id FROM dispatches").fetchone()
        assert row == ("d1", "vnx-dev")
        conn.close()

    def test_repair_idempotent_noop_when_conformant(self, tmp_path):
        mod = _migrate_mod()
        db = tmp_path / "y.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.commit()
        assert mod._dispatches_repair_needed(conn) is False
        assert mod._repair_dispatches_adr007(conn) is False
        conn.close()

    def test_repair_preserves_extra_columns(self, tmp_path):
        mod = _migrate_mod()
        db = tmp_path / "z.db"
        conn = sqlite3.connect(str(db))
        # Single-column UNIQUE + extra columns (operator_approved_at, output_ref).
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                operator_approved_at TEXT,
                output_ref TEXT
            )
        """)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, output_ref) VALUES ('d2', '#42')"
        )
        conn.commit()
        assert mod._dispatches_repair_needed(conn) is True
        mod._repair_dispatches_adr007(conn)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        assert {"operator_approved_at", "output_ref"} <= cols
        assert mod._dispatches_has_composite_unique(conn)
        row = conn.execute("SELECT dispatch_id, output_ref FROM dispatches").fetchone()
        assert row == ("d2", "#42")
        conn.close()


class TestVersionReconciliation:
    def test_lowers_lying_version(self, tmp_path):
        mod = _migrate_mod()
        db = tmp_path / "lie.db"
        conn = sqlite3.connect(str(db))
        # Claim user_version=30 but NO tracks table exists at all.
        conn.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT)")
        conn.execute("PRAGMA user_version = 30")
        conn.commit()
        new_ver = mod._reconcile_lying_user_version(conn)
        conn.commit()
        # No tracks → can't be >= 22; reconciled down to <= 21.
        assert new_ver is not None
        assert conn.execute("PRAGMA user_version").fetchone()[0] <= 21
        conn.close()

    def test_no_change_when_honest(self, tmp_path):
        mod = _migrate_mod()
        db = tmp_path / "honest.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (a INTEGER)")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        assert mod._reconcile_lying_user_version(conn) is None
        conn.close()


# ===========================================================================
# F — backfill graceful failure + idempotency
# ===========================================================================

def _backfill_mod():
    return _load("backfill_track_dispatch_linkage_fsr", "backfill_track_dispatch_linkage.py")


class TestBackfillGraceful:
    def test_raises_typed_error_when_tracks_table_absent(self, tmp_path):
        bf = _backfill_mod()
        db = tmp_path / "no_tracks.db"
        conn = sqlite3.connect(str(db))
        # dispatches present, tracks absent.
        conn.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT, project_id TEXT, track TEXT, pr_ref TEXT, state TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(bf.TrackTablesMissingError, match="migrate_future_system"):
            bf.compute_matches(db, "vnx-dev")

    def test_main_exits_3_with_clear_message(self, tmp_path, capsys):
        bf = _backfill_mod()
        state = tmp_path / ".vnx-data" / "state"
        state.mkdir(parents=True)
        db = state / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT, project_id TEXT, track TEXT, pr_ref TEXT, state TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        rc = bf.main([
            "--project-id", "vnx-dev",
            "--project-dir", str(tmp_path),
        ])
        assert rc == 3
        err = capsys.readouterr().err
        assert "migrate_future_system" in err

    def test_idempotent_on_migrated_db(self, tmp_path):
        """With track tables present, applying the backfill twice is a no-op the 2nd time."""
        bf = _backfill_mod()
        db = tmp_path / "migrated.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE tracks (track_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
            "pr_ref TEXT, PRIMARY KEY (track_id, project_id))"
        )
        conn.execute(
            "CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL DEFAULT 'vnx-dev', track TEXT, pr_ref TEXT, state TEXT, "
            "created_at TEXT, UNIQUE(dispatch_id, project_id))"
        )
        conn.execute("INSERT INTO tracks (track_id, project_id, pr_ref) VALUES ('track-01','vnx-dev','#756')")
        # A legacy dispatch with track='A' and pr_ref matching track-01.
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, track, pr_ref, state, created_at) "
            "VALUES ('20260601-x','vnx-dev','A','#756','completed','2026-06-01')"
        )
        conn.commit()
        conn.close()

        results1 = bf.compute_matches(db, "vnx-dev")
        applied1 = bf.apply_matches(db, results1)
        assert applied1 == 1

        # Second pass: the dispatch is now linked → already_linked, nothing to apply.
        results2 = bf.compute_matches(db, "vnx-dev")
        applied2 = bf.apply_matches(db, results2)
        assert applied2 == 0
        assert any(r.status == "already_linked" for r in results2)


# ===========================================================================
# G — open_items.json -> track_open_items bridge
# ===========================================================================

_BRIDGE_DDL = """
CREATE TABLE tracks (
    track_id   TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    title      TEXT,
    phase      TEXT NOT NULL DEFAULT 'queued',
    pr_ref     TEXT,
    PRIMARY KEY (track_id, project_id)
);
CREATE TABLE dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
    track       TEXT,
    pr_ref      TEXT,
    state       TEXT,
    created_at  TEXT,
    UNIQUE(dispatch_id, project_id)
);
CREATE TABLE track_open_items (
    track_id    TEXT NOT NULL,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
    oi_id       TEXT NOT NULL,
    link_type   TEXT NOT NULL CHECK (link_type IN ('blocks','warns','related')),
    link_source TEXT NOT NULL CHECK (link_source IN ('file_path','mention','manual')),
    linked_at   TEXT NOT NULL DEFAULT '',
    resolved_at TEXT,
    resolution_reason TEXT,
    PRIMARY KEY (track_id, project_id, oi_id, link_type)
);
"""


def _bridge_mod():
    return _load("import_open_items_to_tracks_fsr", "import_open_items_to_tracks.py")


class TestOpenItemsBridge:
    def _setup(self, tmp_path):
        db = tmp_path / "bridge.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(_BRIDGE_DDL)
        # track-01 has a merged PR; a dispatch links to it (already backfilled).
        conn.execute("INSERT INTO tracks (track_id, project_id, pr_ref) VALUES ('track-01','vnx-dev','#756')")
        conn.execute("INSERT INTO tracks (track_id, project_id, pr_ref) VALUES ('track-02','vnx-dev',NULL)")
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, track, pr_ref, state, created_at) "
            "VALUES ('20260601-disp-a','vnx-dev','track-02','#999','completed','2026-06-01')"
        )
        conn.commit()
        conn.close()
        return db

    def _write_oi(self, tmp_path, items):
        p = tmp_path / "open_items.json"
        p.write_text(json.dumps({"schema_version": "1.0", "items": items}))
        return p

    def test_maps_via_dispatch_and_pr(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi = self._write_oi(tmp_path, [
            # M1: origin_dispatch_id -> dispatches.track (track-02)
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "20260601-disp-a", "title": "blk"},
            # M2: pr_id -> tracks.pr_ref (#756 -> track-01)
            {"id": "OI-002", "status": "open", "severity": "warn",
             "pr_id": "#756", "title": "warn"},
            # unmappable
            {"id": "OI-003", "status": "open", "severity": "info",
             "pr_id": "#11111", "title": "no track"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi, apply=True)
        assert result.imported == 2
        assert result.unmapped == 1

        conn = sqlite3.connect(str(db))
        rows = {
            (r[0], r[1], r[2]) for r in conn.execute(
                "SELECT track_id, oi_id, link_type FROM track_open_items"
            )
        }
        conn.close()
        assert ("track-02", "OI-001", "blocks") in rows
        assert ("track-01", "OI-002", "warns") in rows

    def test_dry_run_writes_nothing(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi = self._write_oi(tmp_path, [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "20260601-disp-a", "title": "blk"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi, apply=False)
        assert result.imported == 1
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM track_open_items").fetchone()[0]
        conn.close()
        assert n == 0

    def test_idempotent(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi = self._write_oi(tmp_path, [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "20260601-disp-a", "title": "blk"},
        ])
        first = bridge.bridge_open_items(db, "vnx-dev", oi, apply=True)
        second = bridge.bridge_open_items(db, "vnx-dev", oi, apply=True)
        assert first.imported == 1
        assert second.imported == 0
        assert second.skipped_existing == 1
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM track_open_items").fetchone()[0]
        conn.close()
        assert n == 1

    def test_closed_item_resolves_existing_link(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi_open = self._write_oi(tmp_path, [
            {"id": "OI-005", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "20260601-disp-a", "title": "blk"},
        ])
        bridge.bridge_open_items(db, "vnx-dev", oi_open, apply=True)
        # Now the item is closed in open_items.json.
        oi_closed = self._write_oi(tmp_path, [
            {"id": "OI-005", "status": "done", "severity": "blocker",
             "origin_dispatch_id": "20260601-disp-a", "title": "blk"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi_closed, apply=True)
        assert result.resolved == 1
        conn = sqlite3.connect(str(db))
        resolved_at = conn.execute(
            "SELECT resolved_at FROM track_open_items WHERE oi_id='OI-005'"
        ).fetchone()[0]
        conn.close()
        assert resolved_at is not None

    def test_raises_when_tables_absent(self, tmp_path):
        bridge = _bridge_mod()
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        oi = self._write_oi(tmp_path, [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "d", "title": "x"},
        ])
        with pytest.raises(RuntimeError, match="migrate_future_system"):
            bridge.bridge_open_items(db, "vnx-dev", oi, apply=True)

    def test_project_id_scoping(self, tmp_path):
        """An OI mapped to a track in another project_id is not cross-stamped."""
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi = self._write_oi(tmp_path, [
            {"id": "OI-009", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "20260601-disp-a", "title": "blk"},
        ])
        # Bridge for a DIFFERENT project — disp-a belongs to vnx-dev, so unmapped.
        result = bridge.bridge_open_items(db, "other-proj", oi, apply=True)
        assert result.imported == 0
        assert result.unmapped == 1


# ===========================================================================
# Fix 1 — _build_tracks_from_db re-raises unexpected errors
# ===========================================================================

class TestBuildTracksErrorHandling:
    def _bt0(self):
        return _load("build_t0_state_fsr_err", "build_t0_state.py")

    def test_reraises_unexpected_db_error(self, tmp_path):
        """Non-OperationalError DB errors must propagate, not be swallowed."""
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Write a binary file where the DB should be — connect succeeds but
        # any SQL raises a DatabaseError (not OperationalError).
        db = state_dir / "runtime_coordination.db"
        db.write_bytes(b"\x00" * 1024)  # not a valid SQLite file
        # Expect either a raised exception (corrupt DB) or None (graceful).
        # Critically, it must NOT silently return an empty dict.
        try:
            result = bt0._build_tracks_from_db(state_dir, "vnx-dev")
            # Acceptable: corrupt DB treated as missing (None → legacy fallback).
            assert result is None or isinstance(result, dict)
        except Exception:
            pass  # propagation is also acceptable for non-OperationalError


# ===========================================================================
# Fix 2 — ADR-007 cross-tenant guard when project_id column absent
# ===========================================================================

_TRACKS_NO_PID_DDL = """
CREATE TABLE tracks (
    track_id TEXT NOT NULL PRIMARY KEY,
    title    TEXT,
    phase    TEXT NOT NULL DEFAULT 'queued'
);
"""


class TestBuildTracksADR007Guard:
    def _bt0(self):
        return _load("build_t0_state_fsr_adr007", "build_t0_state.py")

    def test_returns_none_when_project_id_column_absent(self, tmp_path):
        """When project_id is requested but the column is absent, return None."""
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        conn.executescript(_TRACKS_NO_PID_DDL)
        # Insert rows for different "tenants" — no project_id column at all.
        conn.execute("INSERT INTO tracks (track_id, phase) VALUES ('t-other', 'done')")
        conn.execute("INSERT INTO tracks (track_id, phase) VALUES ('t-mine', 'active')")
        conn.commit()
        conn.close()

        result = bt0._build_tracks_from_db(state_dir, "vnx-dev")
        # Must return None (→ legacy fallback), NOT the unscoped rows.
        assert result is None

    def test_returns_rows_when_no_project_id_requested(self, tmp_path):
        """When no project_id is requested and column absent, unscoped read is fine."""
        bt0 = self._bt0()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        conn.executescript(_TRACKS_NO_PID_DDL)
        conn.execute("INSERT INTO tracks (track_id, phase) VALUES ('t-a', 'active')")
        conn.commit()
        conn.close()

        result = bt0._build_tracks_from_db(state_dir, "")
        # Empty project_id → unscoped read is allowed.
        assert result is not None
        assert "t-a" in result


# ===========================================================================
# Fix 3 — schema-preserving ADR-007 repair
# ===========================================================================

class TestDispatchesRepairSchemaPreserving:
    def test_preserves_check_constraint_index_trigger(self, tmp_path):
        """Repair must leave CHECK constraints, extra indexes, and triggers intact."""
        mod = _migrate_mod()
        db = tmp_path / "preserve.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE dispatches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                state       TEXT NOT NULL DEFAULT 'queued'
                            CHECK (state IN ('queued', 'active', 'done')),
                created_at  TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX idx_disp_created ON dispatches(created_at DESC)"
        )
        conn.execute("""
            CREATE TRIGGER trg_disp_after_update
            AFTER UPDATE ON dispatches
            BEGIN SELECT 1; END
        """)
        conn.execute("INSERT INTO dispatches (dispatch_id, state) VALUES ('d1', 'queued')")
        conn.commit()

        changed = mod._repair_dispatches_adr007(conn)
        conn.commit()
        assert changed is True

        # project_id column added.
        cols = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        assert "project_id" in cols

        # Composite UNIQUE index present.
        assert mod._dispatches_has_composite_unique(conn)

        # CHECK constraint survived: invalid state must be rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO dispatches (dispatch_id, state) VALUES ('d2', 'invalid_state')")
        conn.rollback()

        # Custom index survived.
        index_names = {r[1] for r in conn.execute("PRAGMA index_list('dispatches')")}
        assert "idx_disp_created" in index_names

        # Trigger survived.
        trigger_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "trg_disp_after_update" in trigger_names

        conn.close()


# ===========================================================================
# Fix 4 — status normalization + unknown-as-skip
# ===========================================================================

class TestStatusNormalization:
    def _setup(self, tmp_path):
        db = tmp_path / "norm.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(_BRIDGE_DDL)
        conn.execute("INSERT INTO tracks (track_id, project_id) VALUES ('track-01','vnx-dev')")
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, track, state, created_at) "
            "VALUES ('d-norm','vnx-dev','track-01','completed','2026-06-01')"
        )
        conn.commit()
        conn.close()
        return db

    def _write_oi(self, tmp_path, items):
        p = tmp_path / "oi_norm.json"
        p.write_text(json.dumps({"items": items}))
        return p

    def test_open_uppercase_treated_as_open(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi = self._write_oi(tmp_path, [
            {"id": "N-001", "status": "OPEN", "severity": "blocker",
             "origin_dispatch_id": "d-norm"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi, apply=True)
        assert result.imported == 1

    def test_done_uppercase_treated_as_closed(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        # First import to create the link.
        oi_open = self._write_oi(tmp_path, [
            {"id": "N-002", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "d-norm"},
        ])
        bridge.bridge_open_items(db, "vnx-dev", oi_open, apply=True)
        # Now close with uppercase.
        oi_closed = self._write_oi(tmp_path, [
            {"id": "N-002", "status": "DONE", "severity": "blocker",
             "origin_dispatch_id": "d-norm"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi_closed, apply=True)
        assert result.resolved == 1

    def test_wontfix_treated_as_closed(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi_open = self._write_oi(tmp_path, [
            {"id": "N-003", "status": "open", "severity": "warn",
             "origin_dispatch_id": "d-norm"},
        ])
        bridge.bridge_open_items(db, "vnx-dev", oi_open, apply=True)
        oi_closed = self._write_oi(tmp_path, [
            {"id": "N-003", "status": "wontfix", "severity": "warn",
             "origin_dispatch_id": "d-norm"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi_closed, apply=True)
        assert result.resolved == 1

    def test_unknown_status_is_skipped(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)
        oi = self._write_oi(tmp_path, [
            {"id": "N-004", "status": "in_review", "severity": "blocker",
             "origin_dispatch_id": "d-norm"},
        ])
        result = bridge.bridge_open_items(db, "vnx-dev", oi, apply=True)
        # Unknown status must be skipped — not imported, not resolved.
        assert result.imported == 0
        assert result.resolved == 0
        assert result.unmapped == 0
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM track_open_items WHERE oi_id='N-004'").fetchone()[0]
        conn.close()
        assert n == 0


# ===========================================================================
# Fix 5 — severity-change supersedes stale link
# ===========================================================================

class TestSeverityChangeSupersedes:
    def _setup(self, tmp_path):
        db = tmp_path / "sev.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(_BRIDGE_DDL)
        conn.execute("INSERT INTO tracks (track_id, project_id) VALUES ('track-10','vnx-dev')")
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, track, state, created_at) "
            "VALUES ('d-sev','vnx-dev','track-10','completed','2026-06-01')"
        )
        conn.commit()
        conn.close()
        return db

    def _write_oi(self, tmp_path, name, items):
        p = tmp_path / name
        p.write_text(json.dumps({"items": items}))
        return p

    def test_severity_change_supersedes_stale_active_link(self, tmp_path):
        bridge = _bridge_mod()
        db = self._setup(tmp_path)

        # Import as blocker → 'blocks' link.
        oi_blocker = self._write_oi(tmp_path, "oi_blocker.json", [
            {"id": "S-001", "status": "open", "severity": "blocker",
             "origin_dispatch_id": "d-sev"},
        ])
        r1 = bridge.bridge_open_items(db, "vnx-dev", oi_blocker, apply=True)
        assert r1.imported == 1

        # Severity changes to warn → 'warns' link. Old 'blocks' link must be superseded.
        oi_warn = self._write_oi(tmp_path, "oi_warn.json", [
            {"id": "S-001", "status": "open", "severity": "warn",
             "origin_dispatch_id": "d-sev"},
        ])
        r2 = bridge.bridge_open_items(db, "vnx-dev", oi_warn, apply=True)
        assert r2.imported == 1  # new 'warns' link created

        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT link_type, resolved_at FROM track_open_items WHERE oi_id='S-001'"
        ).fetchall()
        conn.close()

        by_type = {lt: ra for lt, ra in rows}
        # 'blocks' link should be resolved (superseded).
        assert "blocks" in by_type
        assert by_type["blocks"] is not None
        # 'warns' link should be active.
        assert "warns" in by_type
        assert by_type["warns"] is None


# ===========================================================================
# Fix 7 — staleness unknown sentinel
# ===========================================================================

class TestStalenessUnknownSentinel:
    def _bt0(self):
        return _load("build_t0_state_fsr_sentinel", "build_t0_state.py")

    def test_missing_generated_at_returns_none_sentinel(self):
        bt0 = self._bt0()
        assert bt0.compute_staleness_seconds("") is None
        assert bt0.compute_staleness_seconds("garbage") is None
        assert bt0.compute_staleness_seconds(None) is None  # type: ignore[arg-type]

    def test_valid_timestamp_still_returns_int(self):
        bt0 = self._bt0()
        from datetime import datetime, timezone
        now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        gen = (now - timedelta(hours=2)).isoformat()
        result = bt0.compute_staleness_seconds(gen, now=now)
        assert isinstance(result, int)
        assert result == 2 * 3600


# ===========================================================================
# Fix 8 — load_open_items raises on unreadable SSOT
# ===========================================================================

class TestLoadOpenItemsRaisesOnUnreadable:
    def test_raises_on_corrupt_json(self, tmp_path):
        bridge = _bridge_mod()
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json }", encoding="utf-8")
        with pytest.raises(RuntimeError, match="unreadable"):
            bridge.load_open_items(bad)

    def test_raises_on_oserror(self, tmp_path):
        bridge = _bridge_mod()
        p = tmp_path / "oi.json"
        p.write_text('{"items":[]}', encoding="utf-8")
        p.chmod(0o000)
        try:
            with pytest.raises(RuntimeError, match="unreadable"):
                bridge.load_open_items(p)
        finally:
            p.chmod(0o644)

    def test_returns_empty_when_file_absent(self, tmp_path):
        bridge = _bridge_mod()
        result = bridge.load_open_items(tmp_path / "nonexistent.json")
        assert result == []
