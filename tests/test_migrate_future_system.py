"""tests/test_migrate_future_system.py — tests for scripts/migrate_future_system.py.

Tests:
- PRAGMA preflight raises on missing project_id (v9-style schema)
- PRAGMA preflight passes on v21 schema with proper UNIQUE constraint
- Bidirectional preflight: raises on extra columns, raises on missing UNIQUE
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

    def test_preflight_raises_on_missing_project_id(self, tmp_path):
        project_dir = self._v9_style_project(tmp_path)
        mod = _get_migrate_module()
        with pytest.raises(RuntimeError, match="project_id"):
            mod.run(project_dir)

    def test_preflight_passes_on_v21_schema(self, tmp_path):
        """v21 DB with project_id passes the preflight and migration proceeds."""
        project_dir = _init_project(tmp_path)
        mod = _get_migrate_module()
        mod.run(project_dir)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
        conn.close()
        assert "project_id" in cols


class TestBidirectionalPreflight:
    """_assert_dispatches_schema_intact raises on extra columns AND missing UNIQUE."""

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

    def test_raises_on_extra_column(self, tmp_path):
        project_dir, conn = self._db_with_extra_column(tmp_path)
        conn.close()
        mod = _get_migrate_module()
        with pytest.raises(RuntimeError, match="extra="):
            mod.run(project_dir)

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
