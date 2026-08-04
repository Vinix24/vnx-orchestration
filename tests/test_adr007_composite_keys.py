"""tests/test_adr007_composite_keys.py — ADR-007 composite-key migration tests.

Verifies the non-destructive, defensive composite UNIQUE index migration
(quality_intelligence.db v27 + runtime_coordination.db v11).

Covers:
  - clean fixture DB with project_id columns → all intended indexes created
  - planted duplicate in one table → that table skipped + logged, others created,
    migration completes, no rows deleted
  - idempotent re-run → no-op
  - after migration, duplicate (project_id, key) inserts are rejected
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import quality_db_init as qdi  # noqa: E402
import schema_migration  # noqa: E402
from coordination_db import _migrate_v11_composite_keys  # noqa: E402


# ---------------------------------------------------------------------------
# Quality Intelligence helpers
# ---------------------------------------------------------------------------

_QI_TABLES_AND_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dispatch_experiments", ("project_id", "id")),
    ("success_patterns", ("project_id", "id")),
    ("antipatterns", ("project_id", "id")),
    ("pattern_usage", ("project_id", "pattern_id")),
    ("prevention_rules", ("project_id", "id")),
    ("session_analytics", ("project_id", "id")),
    ("confidence_events", ("project_id", "id")),
    ("dispatch_pattern_offered", ("project_id", "dispatch_id", "pattern_id")),
    ("dream_pattern_archives", ("project_id", "archive_id")),
)


def _qi_index_name(table: str) -> str:
    return f"ux_{table}_pid"


def _make_qi_v26_with_project_id(db_path: Path) -> sqlite3.Connection:
    """Seed a minimal quality_intelligence.db at v26 with project_id on all targets."""
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    conn.executescript(
        """
        CREATE TABLE dispatch_experiments (
            id INTEGER,
            dispatch_id TEXT,
            project_id TEXT
        );
        CREATE TABLE success_patterns (
            id INTEGER,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE antipatterns (
            id INTEGER,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE pattern_usage (
            pattern_id TEXT,
            pattern_title TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE prevention_rules (
            id INTEGER,
            rule_type TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE session_analytics (
            id INTEGER,
            session_id TEXT NOT NULL,
            project_path TEXT NOT NULL,
            session_date DATE NOT NULL,
            project_id TEXT
        );
        CREATE TABLE confidence_events (
            id INTEGER,
            dispatch_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            confidence_change REAL NOT NULL,
            occurred_at TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE dispatch_pattern_offered (
            dispatch_id TEXT NOT NULL,
            pattern_id TEXT NOT NULL,
            pattern_title TEXT NOT NULL,
            offered_at TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE dream_pattern_archives (
            archive_id INTEGER,
            cycle_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            original_pattern_id INTEGER NOT NULL,
            original_table TEXT NOT NULL
        );
        PRAGMA user_version = 26;
        """
    )
    conn.commit()
    return conn


def _qi_index_exists(conn: sqlite3.Connection, table: str) -> bool:
    name = _qi_index_name(table)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Runtime Coordination helpers
# ---------------------------------------------------------------------------

_RC_TABLES_AND_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("coordination_events", ("project_id", "id")),
    ("incident_log", ("project_id", "id")),
    ("intelligence_injections", ("project_id", "id")),
)


def _make_rc_with_project_id(db_path: Path) -> sqlite3.Connection:
    """Seed a minimal runtime_coordination.db with project_id on target tables."""
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    conn.executescript(
        """
        CREATE TABLE coordination_events (
            id INTEGER,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE incident_log (
            id INTEGER,
            incident_id TEXT NOT NULL,
            incident_class TEXT NOT NULL,
            severity TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE intelligence_injections (
            id INTEGER,
            injection_id TEXT NOT NULL,
            dispatch_id TEXT NOT NULL,
            injection_point TEXT NOT NULL,
            injected_at TEXT NOT NULL,
            project_id TEXT
        );
        PRAGMA user_version = 10;
        """
    )
    conn.commit()
    return conn


def _rc_index_exists(conn: sqlite3.Connection, table: str) -> bool:
    name = f"ux_{table}_pid"
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Quality Intelligence tests
# ---------------------------------------------------------------------------

class TestQiCompositeKeysClean:
    def test_all_indexes_created_on_clean_db(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)

        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)

        for table, _ in _QI_TABLES_AND_KEYS:
            assert _qi_index_exists(conn, table), f"missing index for {table}"
        conn.close()

    def test_user_version_bumped_to_27(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)

        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)

        assert schema_migration.get_user_version(conn) == 27
        conn.close()


class TestQiCompositeKeysDuplicate:
    def test_skips_only_violating_table_and_continues(self, tmp_path, capsys):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)
        # Plant a duplicate (project_id, id) in success_patterns
        conn.execute(
            "INSERT INTO success_patterns (title, description, project_id) VALUES (?, ?, ?)",
            ("p1", "d1", "proj-a"),
        )
        conn.execute(
            "INSERT INTO success_patterns (title, description, project_id) VALUES (?, ?, ?)",
            ("p2", "d2", "proj-a"),
        )
        # Force both rows to share id=1 for (project_id, id) duplicate
        conn.execute("UPDATE success_patterns SET id = 1")
        conn.commit()

        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)
        captured = capsys.readouterr()

        # success_patterns skipped due to duplicate
        assert not _qi_index_exists(conn, "success_patterns")
        assert "composite-keys: skipped success_patterns" in captured.out

        # other indexes still created
        for table, _ in _QI_TABLES_AND_KEYS:
            if table == "success_patterns":
                continue
            assert _qi_index_exists(conn, table), f"missing index for {table}"

        # no rows deleted
        count = conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        assert count == 2
        conn.close()


class TestQiCompositeKeysIdempotent:
    def test_second_run_is_noop(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)

        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)
        assert schema_migration.get_user_version(conn) == 27

        # Second apply_if_below must be a no-op and not raise
        applied = schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)
        assert applied is False
        conn.close()


class TestQiCompositeKeysConstraint:
    def test_rejects_duplicate_after_migration(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)
        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)

        conn.execute(
            "INSERT INTO prevention_rules (id, rule_type, project_id) VALUES (?, ?, ?)",
            (1, "rt", "proj-x"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO prevention_rules (id, rule_type, project_id) VALUES (?, ?, ?)",
                (1, "rt2", "proj-x"),
            )
        conn.close()


class TestQiCompositeKeysDown:
    def test_down_drops_indexes(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)
        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)

        qdi._migrate_v27_down(conn)

        for table, _ in _QI_TABLES_AND_KEYS:
            assert not _qi_index_exists(conn, table)
        conn.close()


# ---------------------------------------------------------------------------
# Runtime Coordination tests
# ---------------------------------------------------------------------------

class TestRcCompositeKeysClean:
    def test_all_indexes_created_on_clean_db(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)

        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        for table, _ in _RC_TABLES_AND_KEYS:
            assert _rc_index_exists(conn, table), f"missing index for {table}"
        conn.close()

    def test_user_version_bumped_to_11(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)

        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        assert schema_migration.get_user_version(conn) == 11
        conn.close()


class TestRcCompositeKeysDuplicate:
    def test_skips_only_violating_table_and_continues(self, tmp_path, caplog):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        # Plant duplicate (project_id, id) in incident_log
        conn.execute(
            "INSERT INTO incident_log (incident_id, incident_class, severity, entity_type, entity_id, occurred_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("i1", "class-a", "high", "dispatch", "d1", "2026-01-01T00:00:00Z", "proj-a"),
        )
        conn.execute(
            "INSERT INTO incident_log (incident_id, incident_class, severity, entity_type, entity_id, occurred_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("i2", "class-a", "high", "dispatch", "d2", "2026-01-01T00:00:00Z", "proj-a"),
        )
        conn.execute("UPDATE incident_log SET id = 10")
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="coordination_db"):
            schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        assert not _rc_index_exists(conn, "incident_log")
        assert any(
            "composite-keys: skipped incident_log" in rec.message
            for rec in caplog.records
        )

        for table, _ in _RC_TABLES_AND_KEYS:
            if table == "incident_log":
                continue
            assert _rc_index_exists(conn, table), f"missing index for {table}"

        count = conn.execute("SELECT COUNT(*) FROM incident_log").fetchone()[0]
        assert count == 2
        conn.close()


class TestRcCompositeKeysIdempotent:
    def test_second_run_is_noop(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)

        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)
        assert schema_migration.get_user_version(conn) == 11

        applied = schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)
        assert applied is False
        conn.close()


class TestRcCompositeKeysConstraint:
    def test_rejects_duplicate_after_migration(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        conn.execute(
            "INSERT INTO coordination_events (id, event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "e1", "test", "dispatch", "d1", "2026-01-01T00:00:00Z", "proj-y"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO coordination_events (id, event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "e2", "test", "dispatch", "d2", "2026-01-01T00:00:00Z", "proj-y"),
            )
        conn.close()


class TestRcCompositeKeysDown:
    def test_down_drops_indexes(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        from coordination_db import _migrate_v11_composite_keys_down
        _migrate_v11_composite_keys_down(conn)

        for table, _ in _RC_TABLES_AND_KEYS:
            assert not _rc_index_exists(conn, table)
        conn.close()


# ---------------------------------------------------------------------------
# OI-563: unexpected per-table error must be durably tracked (not just ERROR-logged)
# ---------------------------------------------------------------------------

class TestRcCompositeKeysUnexpectedError:
    def test_unexpected_error_is_durably_tracked(self, tmp_path, caplog):
        """An unexpected (non-IntegrityError) per-table error must leave a durable
        marker — user_version advancing must not silently drop the missing index."""
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        # A table whose name collides with the would-be index makes
        # CREATE UNIQUE INDEX raise sqlite3.OperationalError (a sqlite3.Error,
        # NOT an IntegrityError) — the unexpected-error path of the migration.
        conn.execute("CREATE TABLE ux_incident_log_pid (x INTEGER)")
        conn.commit()

        with caplog.at_level(logging.ERROR, logger="coordination_db"):
            schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        # The migration still does not abort (existing non-destructive design).
        assert schema_migration.get_user_version(conn) == 11
        # The index on incident_log is genuinely missing (shadowed by a table).
        assert not _rc_index_exists(conn, "incident_log")
        # ...but the failure IS durably recorded.
        failures = conn.execute(
            "SELECT table_name, index_name, columns FROM composite_key_failures"
        ).fetchall()
        assert [(r[0], r[1]) for r in failures] == [("incident_log", "ux_incident_log_pid")]
        conn.close()

    def test_audit_surfaces_under_enforcement(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        conn.execute("CREATE TABLE ux_incident_log_pid (x INTEGER)")
        conn.commit()

        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        from coordination_db import composite_index_audit
        audit = composite_index_audit(conn)
        missing_tables = {m["table"] for m in audit["missing"]}
        assert "incident_log" in missing_tables
        # The unaffected tables still got their index.
        assert "coordination_events" not in missing_tables
        assert "intelligence_injections" not in missing_tables
        # The durable marker is surfaced by the audit.
        assert any(f["table"] == "incident_log" for f in audit["failures"])
        conn.close()


class TestRcCompositeKeysAudit:
    def test_clean_db_has_no_missing_indexes(self, tmp_path):
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        from coordination_db import composite_index_audit
        audit = composite_index_audit(conn)
        assert audit["missing"] == []
        assert audit["tables_checked"] == 3
        conn.close()

    def test_detects_index_shadowed_on_wrong_table(self, tmp_path):
        """IF NOT EXISTS silently no-ops when an index of the same name already
        sits on a different table — the audit must catch that shadowed state."""
        db_path = tmp_path / "rc.db"
        conn = _make_rc_with_project_id(db_path)
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.execute("CREATE UNIQUE INDEX ux_coordination_events_pid ON other (x)")
        conn.commit()

        schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        from coordination_db import composite_index_audit
        audit = composite_index_audit(conn)
        missing_tables = {m["table"] for m in audit["missing"]}
        assert "coordination_events" in missing_tables
        conn.close()


# ---------------------------------------------------------------------------
# OI-563 (QI mirror): unexpected per-table error must be durably tracked
# ---------------------------------------------------------------------------

class TestQiCompositeKeysUnexpectedError:
    def test_unexpected_error_is_durably_tracked(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)
        conn.execute("CREATE TABLE ux_success_patterns_pid (x INTEGER)")
        conn.commit()

        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)

        assert schema_migration.get_user_version(conn) == 27
        assert not _qi_index_exists(conn, "success_patterns")
        failures = conn.execute(
            "SELECT table_name, index_name FROM composite_key_failures"
        ).fetchall()
        assert [(r[0], r[1]) for r in failures] == [("success_patterns", "ux_success_patterns_pid")]
        conn.close()

    def test_audit_surfaces_under_enforcement(self, tmp_path):
        db_path = tmp_path / "qi.db"
        conn = _make_qi_v26_with_project_id(db_path)
        conn.execute("CREATE TABLE ux_success_patterns_pid (x INTEGER)")
        conn.commit()

        schema_migration.apply_if_below(conn, 27, qdi._migrate_v27)

        from quality_db_init import composite_index_audit as qi_audit
        audit = qi_audit(conn)
        missing_tables = {m["table"] for m in audit["missing"]}
        assert "success_patterns" in missing_tables
        assert "dispatch_experiments" not in missing_tables
        conn.close()
