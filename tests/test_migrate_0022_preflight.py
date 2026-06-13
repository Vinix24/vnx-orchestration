"""tests/test_migrate_0022_preflight.py — PRAGMA pre-flight for migration 0022.

Verifies:
1. _assert_dispatches_schema_intact raises RuntimeError on v9-style DB (no project_id)
2. apply_migration() directly (without run()) also triggers the preflight
3. apply_script_if_below is never called when the preflight fails
4. No SQL schema changes happen when the preflight raises
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIB = _PROJECT_ROOT / "scripts" / "lib"
_SCRIPTS = _PROJECT_ROOT / "scripts"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration


FIXTURE_ROADMAP = """\
# VNX Master Roadmap — fixture

## 1. Feature-tracks

| Track | Goal | Doel-state |
|---|---|---|
| **track-01** | **15-juni Subscription Escape** | Alle Claude-workers default via tmux-leaseless lane. |
| **track-02** | **Public 1.0 Launch** | `pip install vnx-orchestration` werkend. |
| **track-03** | **Observability Activation** | Command-centre live, per-provider token-capture. |
| **track-04** | **Routing Hardening** | `provider_constraints.yaml` enforced. |
| **track-05** | **Governance Self-Monitoring** | Health-heartbeat-laag actief. |
| **track-06** | **Dimitri Adoption** | Track-layer + Context-composer-assembly. |

## 2. Phase — ACTIVE

(content elided for test)
"""


def _make_v9_project(tmp_path: Path) -> Path:
    """Create a project with a v9-style dispatches table (no project_id column)."""
    project_dir = tmp_path / "v9project"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    claudedocs = project_dir / "claudedocs"
    claudedocs.mkdir()
    (claudedocs / "VNX-MASTER-ROADMAP-2026-05-28.md").write_text(
        FIXTURE_ROADMAP, encoding="utf-8"
    )

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


def _get_migrate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_future_system",
        _SCRIPTS / "migrate_future_system.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPreflight0022Blocking:
    """PRAGMA pre-flight raises before 0022 SQL executes when project_id is missing."""

    def test_run_repairs_v9_schema_then_migrates(self, tmp_path):
        """Full run() REPAIRS a v9 schema (adds project_id + composite UNIQUE) then migrates.

        Future-state reconciliation E: run() now detect-and-repairs the
        half-applied/legacy dispatches schema via _repair_dispatches_adr007
        before the version-gated migrations, so the preflight passes on the
        repaired schema. The preflight remains a hard guard for DIRECT callers
        (apply_migration / apply_script_if_below) — covered by the two tests
        below, which still raise.
        """
        project_dir = _make_v9_project(tmp_path)
        mod = _get_migrate_module()
        mod.run(project_dir)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "project_id" in cols
        assert version == 30

    def test_apply_migration_direct_raises_on_v9_schema(self, tmp_path):
        """apply_migration() directly (bypassing run()) still triggers the preflight."""
        project_dir = _make_v9_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        mod = _get_migrate_module()
        try:
            with pytest.raises(RuntimeError, match="project_id"):
                mod.apply_migration(conn, project_dir)
        finally:
            conn.close()

    def test_preflight_fires_before_apply_script_if_below(self, tmp_path):
        """apply_script_if_below is never called when the preflight fails."""
        project_dir = _make_v9_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        mod = _get_migrate_module()
        try:
            with patch.object(schema_migration, "apply_script_if_below") as mock_apply:
                with pytest.raises(RuntimeError, match="project_id"):
                    mod.apply_migration(conn, project_dir)
                mock_apply.assert_not_called()
        finally:
            conn.close()

    def test_no_schema_change_after_preflight_failure(self, tmp_path):
        """After the preflight raises, dispatches table is unchanged (no SQL ran)."""
        project_dir = _make_v9_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        mod = _get_migrate_module()
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with pytest.raises(RuntimeError):
                mod.apply_migration(conn, project_dir)
        finally:
            conn.close()

        conn2 = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn2.execute("PRAGMA table_info('dispatches')")}
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        conn2.close()
        assert "project_id" not in cols
        assert version == 0


class TestDirectApplyScriptPreflight:
    """apply_script_if_below(22, ...) triggers the preflight — no bypass possible.

    Importing migrate_future_system registers a pre-hook for migration 22 in
    schema_migration._PREFLIGHT_HOOKS. Once registered, any caller of
    apply_script_if_below(22, ...) runs the PRAGMA check, even without going
    through apply_migration() or run().
    """

    _MIGRATIONS = _PROJECT_ROOT / "schemas" / "migrations"

    def test_direct_apply_script_raises_on_v9_db(self, tmp_path):
        """Direct apply_script_if_below(22, sql) raises when project_id is missing."""
        # Importing the module registers the hook — this is the proof that there is no bypass.
        _get_migrate_module()

        project_dir = _make_v9_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        sql = (self._MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")

        conn = sqlite3.connect(str(db_path))
        try:
            with pytest.raises(RuntimeError, match="project_id"):
                schema_migration.apply_script_if_below(conn, 22, sql)
        finally:
            conn.close()

    def test_direct_apply_script_passes_on_v21_db(self, tmp_path):
        """Direct apply_script_if_below(22, sql) succeeds when all expected columns exist."""
        _get_migrate_module()

        state_dir = tmp_path / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                terminal_id TEXT, track TEXT,
                priority TEXT DEFAULT 'P2',
                pr_ref TEXT, gate TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                bundle_path TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                expires_after TEXT,
                metadata_json TEXT DEFAULT '{}',
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
        sql = (self._MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
        # Should not raise — project_id is present, preflight passes.
        schema_migration.apply_script_if_below(conn, 22, sql)
        conn.close()

    def test_no_sql_executed_when_preflight_raises(self, tmp_path):
        """No schema changes after a pre-hook raises — SAVEPOINT never opened."""
        _get_migrate_module()

        project_dir = _make_v9_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        sql = (self._MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")

        conn = sqlite3.connect(str(db_path))
        try:
            try:
                schema_migration.apply_script_if_below(conn, 22, sql)
            except RuntimeError:
                pass
        finally:
            conn.close()

        conn2 = sqlite3.connect(str(db_path))
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn2.close()
        assert version == 0
        assert "tracks" not in tables
