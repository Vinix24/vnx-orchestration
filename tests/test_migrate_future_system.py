"""tests/test_migrate_future_system.py — tests for scripts/migrate_future_system.py.

Tests:
- Seed 6 tracks from fixture master-roadmap
- Idempotency: re-run produces no duplicate rows
- Dispatch tagging by PR-cluster prefix
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Fixture roadmap content (mirrors the real master-roadmap table structure)
# ---------------------------------------------------------------------------

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


def _init_project(tmp_path: Path, roadmap_content: str = FIXTURE_ROADMAP) -> Path:
    """Create a minimal project with DB + fixture roadmap."""
    project_dir = tmp_path / "project"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    claudedocs = project_dir / "claudedocs"
    claudedocs.mkdir()

    # Write fixture roadmap
    (claudedocs / "VNX-MASTER-ROADMAP-2026-05-28.md").write_text(roadmap_content, encoding="utf-8")

    # Initialize DB with base dispatches + coordination_events tables
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


@pytest.fixture()
def project_dir(tmp_path):
    return _init_project(tmp_path)


# ---------------------------------------------------------------------------
# Import the migration module
# ---------------------------------------------------------------------------

def _get_migrate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_future_system",
        _SCRIPTS / "migrate_future_system.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMigrateFutureSystem:
    def test_seeds_six_tracks(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        conn.close()
        assert count == 6

    def test_all_track_ids_present(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        ids = {r[0] for r in conn.execute("SELECT track_id FROM tracks").fetchall()}
        conn.close()
        assert ids == {"track-01", "track-02", "track-03", "track-04", "track-05", "track-06"}

    def test_idempotent_rerun(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)
        mod.run(project_dir)  # second run must be idempotent

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        conn.close()
        assert count == 6

    def test_dispatch_tagging_by_pr_prefix(self, project_dir):
        # Pre-seed a dispatch with pr_ref = PR-HYG-1 (should map to track-02)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state, pr_ref) "
            "VALUES ('disp-hyg-001', 'queued', 'PR-HYG-1')"
        )
        conn.commit()
        conn.close()

        mod = _get_migrate_module()
        mod.run(project_dir)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = 'disp-hyg-001'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "track-02"

    def test_migration_events_emitted(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM coordination_events WHERE event_type = 'track_created'"
        ).fetchone()[0]
        conn.close()
        assert count == 6

    def test_events_not_duplicated_on_rerun(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM coordination_events WHERE event_type = 'track_created'"
        ).fetchone()[0]
        conn.close()
        assert count == 6

    def test_missing_roadmap_raises(self, tmp_path):
        # Project without roadmap file
        project_dir = tmp_path / "bare"
        state_dir = project_dir / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "runtime_coordination.db"

        conn = sqlite3.connect(str(db_path))
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
        conn.close()

        mod = _get_migrate_module()
        with pytest.raises((FileNotFoundError, ValueError)):
            mod.run(project_dir)

    def test_schema_version_23_set(self, project_dir):
        """Full migration run (0022 + 0023) ends at user_version=23."""
        mod = _get_migrate_module()
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 23


class TestPragmaPreflightAssertion:
    """_assert_dispatches_schema_intact raises RuntimeError when project_id missing."""

    def _v9_style_project(self, tmp_path: Path) -> Path:
        """Create a project with a v9-style dispatches table (no project_id)."""
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


class TestEmitEventsOrdering:
    """emit_events runs BEFORE conn.commit() on the seed step (ADR-005)."""

    def test_emit_failure_rolls_back_seed(self, tmp_path):
        """If emit_events raises, seed tracks are not committed to DB."""
        project_dir = _init_project(tmp_path)
        mod = _get_migrate_module()

        with patch.object(mod, "emit_events", side_effect=RuntimeError("emit failed")):
            with pytest.raises(RuntimeError, match="emit failed"):
                mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        conn.close()
        assert count == 0, "Seed must be rolled back when emit_events fails"


class TestBidirectionalPreflight:
    """_assert_dispatches_schema_intact raises on extra columns AND missing UNIQUE."""

    def _db_with_extra_column(self, tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
        project_dir = tmp_path / "extra_col"
        state_dir = project_dir / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        claudedocs = project_dir / "claudedocs"
        claudedocs.mkdir()
        (claudedocs / "VNX-MASTER-ROADMAP-2026-05-28.md").write_text(
            FIXTURE_ROADMAP, encoding="utf-8"
        )
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

    def test_raises_on_missing_unique(self, tmp_path):
        """A dispatches table without UNIQUE(dispatch_id, project_id) fails preflight."""
        project_dir = tmp_path / "no_unique"
        state_dir = project_dir / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        claudedocs = project_dir / "claudedocs"
        claudedocs.mkdir()
        (claudedocs / "VNX-MASTER-ROADMAP-2026-05-28.md").write_text(
            FIXTURE_ROADMAP, encoding="utf-8"
        )
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
        with pytest.raises(RuntimeError, match="UNIQUE"):
            mod.run(project_dir)


class TestPreflightBefore0023:
    """Second preflight (_assert_dispatches_schema_post_0022) triggers before 0023 rebuild."""

    def test_preflight_0023_registered(self):
        """schema_migration has a preflight hook for version 23 after module import."""
        mod = _get_migrate_module()
        import schema_migration as _sm
        assert 23 in _sm._PREFLIGHT_HOOKS, "preflight for 0023 must be registered"

    def test_preflight_0023_raises_on_missing_operator_approved_at(self, tmp_path):
        """If post-0022 schema is missing operator_approved_at, 0023 is refused."""
        project_dir = _init_project(tmp_path)
        mod = _get_migrate_module()

        original_apply = schema_migration.apply_script_if_below

        def patched_apply(conn, target_version, sql):
            if target_version == 22:
                # Apply 0022 but then DROP operator_approved_at to simulate drift
                result = original_apply(conn, target_version, sql)
                if result:
                    conn.execute("ALTER TABLE dispatches RENAME TO dispatches_tmp_test")
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
                            UNIQUE(dispatch_id, project_id)
                        )
                    """)
                    conn.execute("INSERT INTO dispatches SELECT id, dispatch_id, project_id, state, terminal_id, track, priority, pr_ref, gate, attempt_count, bundle_path, created_at, updated_at, expires_after, metadata_json FROM dispatches_tmp_test")
                    conn.execute("DROP TABLE dispatches_tmp_test")
                return result
            return original_apply(conn, target_version, sql)

        with patch.object(schema_migration, "apply_script_if_below", patched_apply):
            with pytest.raises(RuntimeError, match="operator_approved_at"):
                mod.run(project_dir)


class TestMigrationAuditNDJSON:
    """Migration UPDATE blocks emit structured events to migration_audit.ndjson."""

    def test_full_run_creates_migration_audit_file(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)
        audit_path = project_dir / ".vnx-data" / "events" / "migration_audit.ndjson"
        assert audit_path.exists(), "migration_audit.ndjson must be created by run()"

    def test_audit_contains_next_up_set_event(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)
        audit_path = project_dir / ".vnx-data" / "events" / "migration_audit.ndjson"
        content = audit_path.read_text(encoding="utf-8")
        import json as _json
        events = [_json.loads(line) for line in content.splitlines() if line.strip()]
        types = {e["event_type"] for e in events}
        assert "track_next_up_set" in types or "tracks_next_up_cleared" in types, (
            f"Expected next_up audit event, got event types: {types}"
        )

    def test_audit_events_have_record_id(self, project_dir):
        mod = _get_migrate_module()
        mod.run(project_dir)
        audit_path = project_dir / ".vnx-data" / "events" / "migration_audit.ndjson"
        content = audit_path.read_text(encoding="utf-8")
        import json as _json
        events = [_json.loads(line) for line in content.splitlines() if line.strip()]
        for evt in events:
            assert "record_id" in evt, f"Event missing record_id: {evt}"


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
