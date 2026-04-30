"""Tests for migration 0010 — project_id column (Phase 0 single-VNX).

Coverage:
  A. Fresh DB → migration adds the column with DEFAULT 'vnx-dev'
  B. Existing rows → migration backfills the DEFAULT
  C. Re-run migration on an already-migrated DB → no-op
  D. NOT NULL constraint is enforced at INSERT time
  E. project_id index exists after migration
  F. ``current_project_id()`` reads the env var or defaults
  G. Schema version bumped to v10 (runtime) / 8.3.0-project-id (qi)
  H. Cross-DB single-runner: tables not present in a DB are silently skipped
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure scripts/lib is on sys.path even when this test module is run alone.
_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from project_id_migration import (  # noqa: E402
    QI_SCHEMA_VERSION,
    QUALITY_INTELLIGENCE_TABLES,
    RUNTIME_COORDINATION_TABLES,
    RUNTIME_SCHEMA_VERSION,
    apply_project_id_migration,
    run_quality_intelligence_migration,
    run_runtime_coordination_migration,
)
from project_scope import (  # noqa: E402
    DEFAULT_PROJECT,
    ENV_VAR,
    current_project_id,
    scoped_query,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_runtime_db(state_dir: Path) -> Path:
    """Initialise a fresh runtime_coordination.db at the canonical path."""
    from runtime_coordination import init_schema  # local import (sys.path set above)

    init_schema(state_dir, _SCHEMAS_DIR / "runtime_coordination.sql")
    return state_dir / "runtime_coordination.db"


def _init_qi_db(state_dir: Path) -> Path:
    """Build a minimal stand-in for quality_intelligence.db with the Phase 0 tables.

    We do not depend on the full QI schema here — only on the subset of
    tables migration 0010 touches. Each table has a single ``id`` column
    plus arbitrary other columns; the migration only cares about
    ``project_id`` semantics.
    """
    qi_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(qi_path))
    try:
        for table in QUALITY_INTELLIGENCE_TABLES:
            conn.execute(
                f"CREATE TABLE {table} ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  payload TEXT"
                ")"
            )
        conn.execute(
            "CREATE TABLE schema_version ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  description TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO schema_version (version, description) "
            "VALUES ('8.2.0-cqs-advisory-oi', 'baseline for test')"
        )
        conn.commit()
    finally:
        conn.close()
    return qi_path


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _index_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Case A: Fresh DB → migration adds the column with DEFAULT 'vnx-dev'
# ---------------------------------------------------------------------------

def test_runtime_migration_adds_project_id_to_fresh_db(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)

    result = run_runtime_coordination_migration(db_path)

    assert result["status"] == "ok"
    conn = sqlite3.connect(str(db_path))
    try:
        for table in RUNTIME_COORDINATION_TABLES:
            cols = _columns(conn, table)
            assert "project_id" in cols, f"{table} missing project_id"
    finally:
        conn.close()


def test_qi_migration_adds_project_id_to_fresh_db(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_path = _init_qi_db(state_dir)

    result = run_quality_intelligence_migration(qi_path)

    assert result["status"] == "ok"
    conn = sqlite3.connect(str(qi_path))
    try:
        for table in QUALITY_INTELLIGENCE_TABLES:
            assert "project_id" in _columns(conn, table), f"{table} missing project_id"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case B: Existing rows are backfilled with DEFAULT
# ---------------------------------------------------------------------------

def test_existing_rows_backfilled_to_default(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)

    # Seed a row before migration.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state) VALUES (?, ?)",
            ("pre-migration-1", "queued"),
        )
        conn.commit()
    finally:
        conn.close()

    run_runtime_coordination_migration(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT dispatch_id, project_id FROM dispatches WHERE dispatch_id = ?",
            ("pre-migration-1",),
        ).fetchall()
        assert rows == [("pre-migration-1", "vnx-dev")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case C: Re-running the migration is a no-op
# ---------------------------------------------------------------------------

def test_runtime_migration_is_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)

    first = run_runtime_coordination_migration(db_path)
    second = run_runtime_coordination_migration(db_path)

    # Every table goes from "added" -> "already_present".
    for table, status in first["results"].items():
        if status in ("added", "already_present"):
            assert second["results"][table] == "already_present"


def test_qi_migration_is_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_path = _init_qi_db(state_dir)

    first = run_quality_intelligence_migration(qi_path)
    second = run_quality_intelligence_migration(qi_path)

    for table, status in first["results"].items():
        if status in ("added", "already_present"):
            assert second["results"][table] == "already_present"


# ---------------------------------------------------------------------------
# Case D: NOT NULL constraint enforced
# ---------------------------------------------------------------------------

def test_not_null_constraint_enforced(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)
    run_runtime_coordination_migration(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, project_id) "
                "VALUES (?, ?, NULL)",
                ("explicit-null", "queued"),
            )
            conn.commit()
    finally:
        conn.close()


def test_default_applies_when_project_id_omitted(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)
    run_runtime_coordination_migration(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state) VALUES (?, ?)",
            ("post-migration-1", "queued"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT project_id FROM dispatches WHERE dispatch_id = ?",
            ("post-migration-1",),
        ).fetchone()
        assert row == ("vnx-dev",)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case E: Index exists after migration
# ---------------------------------------------------------------------------

def test_runtime_indexes_created(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)
    run_runtime_coordination_migration(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        for table in RUNTIME_COORDINATION_TABLES:
            indexes = _index_names(conn, table)
            assert f"idx_{table}_project" in indexes, (
                f"missing idx_{table}_project; have {indexes}"
            )
    finally:
        conn.close()


def test_qi_indexes_created(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_path = _init_qi_db(state_dir)
    run_quality_intelligence_migration(qi_path)

    conn = sqlite3.connect(str(qi_path))
    try:
        for table in QUALITY_INTELLIGENCE_TABLES:
            indexes = _index_names(conn, table)
            assert f"idx_{table}_project" in indexes, (
                f"missing idx_{table}_project; have {indexes}"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case F: current_project_id() reads env or defaults
# ---------------------------------------------------------------------------

def test_current_project_id_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert current_project_id() == DEFAULT_PROJECT


def test_current_project_id_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "mc")
    assert current_project_id() == "mc"


def test_current_project_id_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "Has Spaces")
    with pytest.raises(ValueError):
        current_project_id()


def test_scoped_query_appends_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "mc")
    sql = scoped_query("SELECT * FROM success_patterns WHERE 1=1")
    assert sql.endswith("AND project_id = 'mc'")


def test_scoped_query_explicit_override() -> None:
    sql = scoped_query("SELECT * FROM antipatterns WHERE 1=1", project_id="sales-copilot")
    assert "project_id = 'sales-copilot'" in sql


def test_scoped_query_rejects_unsafe_id() -> None:
    with pytest.raises(ValueError):
        scoped_query("SELECT * FROM x WHERE 1=1", project_id="x'; DROP TABLE x; --")


# ---------------------------------------------------------------------------
# Case G: Schema version bumped
# ---------------------------------------------------------------------------

def test_runtime_schema_version_bumped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)
    run_runtime_coordination_migration(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        latest = conn.execute(
            "SELECT MAX(version) FROM runtime_schema_version"
        ).fetchone()[0]
        assert latest == RUNTIME_SCHEMA_VERSION == 10
    finally:
        conn.close()


def test_qi_schema_version_bumped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_path = _init_qi_db(state_dir)
    run_quality_intelligence_migration(qi_path)

    conn = sqlite3.connect(str(qi_path))
    try:
        rows = conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (QI_SCHEMA_VERSION,),
        ).fetchall()
        assert rows, f"schema_version row missing for {QI_SCHEMA_VERSION}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case H: Cross-DB single-runner behaviour — tables absent from a DB are skipped
# ---------------------------------------------------------------------------

def test_apply_skips_missing_table(tmp_path: Path) -> None:
    db_path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Only one of the runtime tables exists in this minimal DB.
        conn.execute("CREATE TABLE dispatches (id INTEGER, state TEXT)")
        conn.commit()
        results = apply_project_id_migration(conn, RUNTIME_COORDINATION_TABLES)
        conn.commit()
    finally:
        conn.close()

    assert results["dispatches"] == "added"
    for table in RUNTIME_COORDINATION_TABLES:
        if table != "dispatches":
            assert results[table] == "skipped_missing"


def test_apply_rejects_unsafe_default(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE dispatches (id INTEGER)")
        with pytest.raises(ValueError):
            apply_project_id_migration(
                conn, ("dispatches",), default_project_id="evil'; DROP TABLE x; --"
            )
    finally:
        conn.close()


def test_runner_skips_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    res = run_runtime_coordination_migration(missing)
    assert res["status"] == "skipped_no_db"

    res2 = run_quality_intelligence_migration(missing)
    assert res2["status"] == "skipped_no_db"
