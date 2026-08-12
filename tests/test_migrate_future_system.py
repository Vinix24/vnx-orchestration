"""tests/test_migrate_future_system.py — tests for scripts/migrate_future_system.py.

Tests:
- PRAGMA preflight raises on missing project_id (v9-style schema)
- PRAGMA preflight passes on v21 schema with proper UNIQUE constraint
- Bidirectional preflight: extra columns are preserved (dynamic rebuild), raises on missing UNIQUE
- Preflight hook triggers via direct apply_script_if_below call
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIB = _PROJECT_ROOT / "scripts" / "lib"
_SCRIPTS = _PROJECT_ROOT / "scripts"
_SCHEMAS = _PROJECT_ROOT / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration


def _init_project(tmp_path: Path) -> Path:
    """Create a minimal project with DB having a v21-style dispatches table."""
    project_dir = tmp_path / "project"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)

    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE coordination_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT,
            event_type  TEXT,
            entity_type TEXT,
            entity_id   TEXT,
            from_state  TEXT,
            to_state    TEXT,
            actor       TEXT,
            reason      TEXT,
            metadata_json TEXT,
            occurred_at TEXT,
            project_id  TEXT
        )
    """)
    conn.commit()
    conn.close()

    # W1 coupled migration is fail-closed: needs a resolvable project_id.
    # Write the marker so _marker_project_id() finds it walking up from the DB.
    (project_dir / ".vnx-project-id").write_text("test-project\n", encoding="utf-8")

    return project_dir


def _get_migrate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_future_system",
        _SCRIPTS / "migrate_future_system.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPragmaPreflightAssertion:
    """_assert_dispatches_schema_intact raises RuntimeError when project_id missing."""

    def _v9_style_project(self, tmp_path: Path) -> Path:
        """Create a project with a v9-style dispatches table (no project_id)."""
        project_dir = tmp_path / "v9project"
        state_dir = project_dir / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)

        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
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
        """)
        conn.execute("""
            CREATE TABLE coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT,
                event_type TEXT, entity_type TEXT, entity_id TEXT,
                from_state TEXT, to_state TEXT, actor TEXT, reason TEXT,
                metadata_json TEXT, occurred_at TEXT, project_id TEXT
            )
        """)
        conn.commit()
        conn.close()
        return project_dir

    def test_preflight_raises_on_missing_project_id(self, tmp_path, monkeypatch):
        project_dir = self._v9_style_project(tmp_path)
        mod = _get_migrate_module()
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.setattr(mod, "_marker_project_id", lambda _db_path: None)
        with pytest.raises(RuntimeError, match="project_id"):
            mod.run(project_dir)

    def test_preflight_passes_on_v21_schema(self, tmp_path):
        """v21 DB with project_id passes the preflight and migration proceeds.

        version == 32, not 31 (OI-1169): run() now ends with a generic
        auto_apply sweep, so a single call carries a fresh store straight
        through the numbered walk (0022->0031) AND everything runner-backed
        above it (0032) in one pass.
        """
        project_dir = _init_project(tmp_path)
        mod = _get_migrate_module()
        mod.run(project_dir)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "project_id" in cols
        assert version == 32


class TestBidirectionalPreflight:
    """_assert_dispatches_schema_intact: allows extra columns, raises on missing UNIQUE."""

    def _db_with_extra_column(self, tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
        project_dir = tmp_path / "extra_col"
        state_dir = project_dir / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                terminal_id TEXT, track TEXT, priority TEXT,
                pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                bundle_path TEXT, created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', expires_after TEXT,
                metadata_json TEXT DEFAULT '{}',
                extra_column TEXT,
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT,
                event_type TEXT, entity_type TEXT, entity_id TEXT,
                from_state TEXT, to_state TEXT, actor TEXT, reason TEXT,
                metadata_json TEXT, occurred_at TEXT, project_id TEXT
            )
        """)
        conn.commit()
        return project_dir, conn

    def test_extra_column_preserved_by_dynamic_rebuild(self, tmp_path):
        """Extra columns beyond the 15-column required set are preserved (not rejected).

        The guard was relaxed (only missing required columns fail): a pre-v22 store
        carrying an extra column uses the dynamic rebuild path, which copies ALL
        non-generated columns so nothing is silently dropped.
        """
        project_dir, conn = self._db_with_extra_column(tmp_path)
        conn.execute("PRAGMA user_version = 20")
        conn.commit()
        conn.close()
        mod = _get_migrate_module()

        # apply_migration directly (bypass run() which requires tenant resolution).
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn2 = _sqlite3.connect(str(db_path))
        try:
            mod.apply_migration(conn2, project_dir)
            conn2.commit()
            cols = {row[1] for row in conn2.execute("PRAGMA table_info('dispatches')")}
            version = conn2.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn2.close()

        assert "extra_column" in cols, "extra_column was dropped by the dynamic rebuild"
        assert "operator_approved_at" in cols, "operator_approved_at missing after 0022"
        assert version == 22

    def test_raises_on_missing_unique(self, tmp_path, monkeypatch):
        """A dispatches table missing UNIQUE(dispatch_id, project_id) must not migrate silently.

        N1 (#859 round-2): a table with neither a solo dispatch_id uniqueness nor the
        composite is now detected as needing the ADR-007 pre-migration repair (it was
        previously a silent no-op). This DB sits at a NON-canonical .vnx-data/state
        path with no .vnx-project-id marker and no VNX_PROJECT_ID, so the tenant is
        unresolvable and the repair fails closed (R3.1) — run() still raises (the
        guard property holds), just earlier and with a precise tenant-resolution
        error instead of the legacy "missing UNIQUE" preflight message.
        """
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        project_dir = tmp_path / "no_unique"
        state_dir = project_dir / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                terminal_id TEXT, track TEXT, priority TEXT,
                pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                bundle_path TEXT, created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', expires_after TEXT,
                metadata_json TEXT DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT,
                event_type TEXT, entity_type TEXT, entity_id TEXT,
                from_state TEXT, to_state TEXT, actor TEXT, reason TEXT,
                metadata_json TEXT, occurred_at TEXT, project_id TEXT
            )
        """)
        conn.commit()
        conn.close()
        mod = _get_migrate_module()
        monkeypatch.setattr(mod, "_marker_project_id", lambda _db_path: None)
        # N1: missing composite → ADR-007 repair fires; unresolvable tenant → fail-closed.
        with pytest.raises(RuntimeError, match="project_id|UNIQUE"):
            mod.run(project_dir)


class TestPreflightThroughApplyScriptIfBelow:
    """Preflight hook triggers even when apply_script_if_below is called directly (Fix 8)."""

    def test_direct_apply_triggers_preflight_for_22(self, tmp_path):
        """Directly calling apply_script_if_below(conn, 22, sql) triggers the v22 preflight."""
        db_path = tmp_path / "direct.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        # Create a dispatches table WITHOUT project_id — should fail preflight
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'queued'
            )
        """)
        conn.commit()

        mod = _get_migrate_module()
        sql_path = _MIGRATIONS / "0022_track_layer.sql"
        sql = sql_path.read_text(encoding="utf-8")
        with pytest.raises(RuntimeError, match="project_id|schema drift"):
            schema_migration.apply_script_if_below(conn, 22, sql)
        conn.close()


class TestAutoApplySweep:
    """OI-1169: run() must end with a generic auto_apply sweep so anything above
    the numbered 0022->0031 walk (e.g. 0032) is picked up without a new
    hardcoded step here. Before the fix a store already at user_version 31
    stayed at 31 forever, no matter how many times `vnx migrate` ran, because
    run() never called migrations.auto_apply.auto_apply() at all — the ONLY
    wiring for that function was scripts/build_t0_state.py's SessionStart
    bootstrap, which most central stores never go through (T0-measurement
    2026-08-12: every central store but vnx-dev sat at 31)."""

    def test_store_at_31_reaches_highest_available_after_run(self, tmp_path):
        """A store already at user_version 31 (the numbered-walk terminal
        version, simulating a store migrated before 0032 existed) must land
        on the highest runner-backed migration (32) after a fresh run().

        Fails on the pre-fix code: run() stops at 31 and never advances
        further, so the final assertion (version == 32) fails.
        """
        project_dir = _init_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"

        # Land the store at 31 first, with the sweep disabled on THIS module
        # instance — deterministically reproduces "a store at 31" regardless
        # of how the numbered walk itself evolves.
        setup_mod = _get_migrate_module()
        setup_mod.auto_apply = lambda *_a, **_kw: []
        setup_mod.run(project_dir)

        conn = sqlite3.connect(str(db_path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 31, "fixture setup must land the store at 31 before testing the sweep"

        # A fresh `vnx migrate` run (real auto_apply, not the disabled stand-in
        # above) against that already-31 store is the exact scenario OI-1169
        # is about: the numbered walk (A-D) has nothing left to do, only the
        # generic sweep (F) can advance the store further.
        real_mod = _get_migrate_module()
        real_mod.run(project_dir)

        conn = sqlite3.connect(str(db_path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_track_pr_delivery = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_pr_delivery'"
        ).fetchone()
        conn.close()

        assert version == 32, "store must land on the highest available runner-backed migration"
        assert has_track_pr_delivery is not None, "0032's track_pr_delivery table must exist"
