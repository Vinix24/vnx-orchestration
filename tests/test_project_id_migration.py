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
        # Migration 0010 stamps RUNTIME_SCHEMA_VERSION (10) into
        # runtime_schema_version. The schema bootstrap (init_schema via
        # runtime_coordination_v10.sql) also inserts version 12 for the
        # Wave 5 PR-5.3 multi-tenant composite UNIQUE changes.
        # Assert that the migration-0010 target version is present AND
        # that the latest version is at least that — accommodating the
        # bootstrap's higher version stamp.
        row = conn.execute(
            "SELECT 1 FROM runtime_schema_version WHERE version = ?",
            (RUNTIME_SCHEMA_VERSION,),
        ).fetchone()
        assert row is not None, (
            f"runtime_schema_version row for migration-0010 target "
            f"(version={RUNTIME_SCHEMA_VERSION}) missing after migration"
        )
        assert latest >= RUNTIME_SCHEMA_VERSION, (
            f"MAX(version) {latest} < RUNTIME_SCHEMA_VERSION {RUNTIME_SCHEMA_VERSION}"
        )
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


# ---------------------------------------------------------------------------
# Case I: worker_states.project_id self-heal (OI-095)
#
# Regression for the OI-095 telemetry gap. The v9 schema creates worker_states
# WITHOUT project_id; v10's CREATE TABLE IF NOT EXISTS is a no-op on a DB that
# already has the v9 table, so the column relied solely on migration 0017.
# 0017 is version-gated AND performs an invasive composite-UNIQUE rebuild, so
# on a desynced DB (e.g. user_version=20, runtime_schema_version >= 12) it does
# not re-run and worker_states.project_id stays missing. heartbeat writes then
# fail with "no such column: project_id" and worker-health telemetry goes blind.
#
# Fix: worker_states is now part of RUNTIME_COORDINATION_TABLES so the
# idempotent init path heals the column + idx_worker_states_project on every
# init, regardless of schema version, and 0017 column-guards its ADD COLUMN so
# the two paths coexist without error.
# ---------------------------------------------------------------------------

_APPLY_0017_SQL = _SCHEMAS_DIR / "migrations" / "0017_multi_tenant_lease_isolation.sql"


def _worker_states_sql(*, with_project_id: bool) -> str:
    """CREATE TABLE for worker_states.

    Without project_id this mirrors the v9 shape (the OI-095 desync); with
    project_id it mirrors the post-0017 / fully-healed shape.
    """
    project_id_line = (
        "            project_id       TEXT    NOT NULL DEFAULT 'vnx-dev',\n"
        if with_project_id
        else ""
    )
    return f"""
        CREATE TABLE worker_states (
            terminal_id      TEXT    NOT NULL PRIMARY KEY,
            dispatch_id      TEXT    NOT NULL,
{project_id_line}            state            TEXT    NOT NULL DEFAULT 'initializing',
            last_output_at   TEXT,
            state_entered_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            stall_count      INTEGER NOT NULL DEFAULT 0,
            blocked_reason   TEXT,
            metadata_json    TEXT,
            created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
    """


def _build_desynced_runtime_db(
    db_path: Path,
    *,
    worker_states_has_project_id: bool = False,
    schema_version: int = 11,
    user_version: int = 11,
) -> None:
    """Build a runtime_coordination.db that mirrors a real desynced/pre-0017 DB.

    dispatches/terminal_leases/dispatch_attempts carry project_id (from 0010)
    with single-column UNIQUE constraints. worker_states is created WITHOUT
    project_id unless ``worker_states_has_project_id`` is True. The
    runtime_schema_version table and PRAGMA user_version are stamped to the
    given values so version-independence can be asserted.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE runtime_schema_version (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                description TEXT NOT NULL
            );

            CREATE TABLE dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL UNIQUE,
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
                metadata_json   TEXT    DEFAULT '{}'
            );

            CREATE TABLE terminal_leases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id         TEXT    NOT NULL UNIQUE,
                project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
                state               TEXT    NOT NULL DEFAULT 'idle',
                dispatch_id         TEXT,
                generation          INTEGER NOT NULL DEFAULT 1,
                leased_at           TEXT,
                expires_at          TEXT,
                last_heartbeat_at   TEXT,
                released_at         TEXT,
                metadata_json       TEXT    DEFAULT '{}'
            );
            INSERT INTO terminal_leases (terminal_id, state, generation)
                VALUES ('T1', 'idle', 1), ('T2', 'idle', 1), ('T3', 'idle', 1);

            CREATE TABLE dispatch_attempts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id      TEXT    NOT NULL UNIQUE,
                dispatch_id     TEXT    NOT NULL REFERENCES dispatches (dispatch_id),
                attempt_number  INTEGER NOT NULL DEFAULT 1,
                terminal_id     TEXT    NOT NULL,
                state           TEXT    NOT NULL DEFAULT 'pending',
                started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                ended_at        TEXT,
                failure_reason  TEXT,
                metadata_json   TEXT    DEFAULT '{}',
                project_id      TEXT    NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )

        conn.executescript(
            _worker_states_sql(with_project_id=worker_states_has_project_id)
        )

        conn.execute(
            "INSERT INTO runtime_schema_version (version, description) VALUES (?, ?)",
            (schema_version, "desynced test baseline"),
        )
        conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
    finally:
        conn.close()


def _project_id_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return sum(
            1
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if r[1] == "project_id"
        )
    finally:
        conn.close()


def test_worker_states_in_runtime_tables() -> None:
    """worker_states must be a runtime-coordination target table now."""
    assert "worker_states" in RUNTIME_COORDINATION_TABLES


def test_self_heal_adds_worker_states_project_id_version_independent(tmp_path: Path) -> None:
    """A desynced DB (high version, no worker_states.project_id) is healed."""
    db_path = tmp_path / "runtime_coordination.db"
    _build_desynced_runtime_db(
        db_path,
        worker_states_has_project_id=False,
        schema_version=20,
        user_version=20,
    )

    # Pre-condition: column genuinely missing.
    conn = sqlite3.connect(str(db_path))
    try:
        assert "project_id" not in _columns(conn, "worker_states")
    finally:
        conn.close()

    result = run_runtime_coordination_migration(db_path)
    assert result["status"] == "ok"
    assert result["results"]["worker_states"] == "added"

    conn = sqlite3.connect(str(db_path))
    try:
        assert "project_id" in _columns(conn, "worker_states")
        assert "idx_worker_states_project" in _index_names(conn, "worker_states")
    finally:
        conn.close()


def test_self_heal_is_idempotent(tmp_path: Path) -> None:
    """Running the heal twice is a clean no-op the second time."""
    db_path = tmp_path / "runtime_coordination.db"
    _build_desynced_runtime_db(
        db_path, worker_states_has_project_id=False, schema_version=20, user_version=20
    )

    first = run_runtime_coordination_migration(db_path)
    second = run_runtime_coordination_migration(db_path)

    assert first["results"]["worker_states"] == "added"
    assert second["results"]["worker_states"] == "already_present"
    # Exactly one project_id column — no duplicate work.
    assert _project_id_count(db_path, "worker_states") == 1


def test_apply_0017_after_init_does_not_raise(tmp_path: Path) -> None:
    """0017 must coexist with an init that already healed worker_states.

    init adds worker_states.project_id; a later 0017 (version still < 12) must
    not fail on its bare ADD COLUMN and must leave a single project_id column.
    """
    db_path = tmp_path / "runtime_coordination.db"
    # schema_version 11 → 0017 (target v12) will still run after init.
    _build_desynced_runtime_db(
        db_path, worker_states_has_project_id=False, schema_version=11, user_version=11
    )

    # Init heals worker_states first.
    run_runtime_coordination_migration(db_path)
    assert _project_id_count(db_path, "worker_states") == 1

    from migrations.apply_0017 import apply_migration  # noqa: E402

    applied = apply_migration(db_path, _APPLY_0017_SQL, vnx_data_dir=tmp_path)
    assert applied is True  # ran (was below v12)

    conn = sqlite3.connect(str(db_path))
    try:
        assert "project_id" in _columns(conn, "worker_states")
        max_version = conn.execute(
            "SELECT MAX(version) FROM runtime_schema_version"
        ).fetchone()[0]
        assert max_version == 12
    finally:
        conn.close()
    assert _project_id_count(db_path, "worker_states") == 1


def test_init_after_apply_0017_does_not_raise(tmp_path: Path) -> None:
    """init must coexist with a DB already migrated by 0017.

    0017 adds worker_states.project_id; a later init must treat it as
    already_present and leave a single column.
    """
    db_path = tmp_path / "runtime_coordination.db"
    _build_desynced_runtime_db(
        db_path, worker_states_has_project_id=False, schema_version=11, user_version=11
    )

    from migrations.apply_0017 import apply_migration  # noqa: E402

    apply_migration(db_path, _APPLY_0017_SQL, vnx_data_dir=tmp_path)
    assert _project_id_count(db_path, "worker_states") == 1

    result = run_runtime_coordination_migration(db_path)
    assert result["results"]["worker_states"] == "already_present"
    assert _project_id_count(db_path, "worker_states") == 1

    conn = sqlite3.connect(str(db_path))
    try:
        assert "idx_worker_states_project" in _index_names(conn, "worker_states")
    finally:
        conn.close()


def test_complete_db_unaffected(tmp_path: Path) -> None:
    """A DB that already has worker_states.project_id is left untouched."""
    db_path = tmp_path / "runtime_coordination.db"
    _build_desynced_runtime_db(
        db_path, worker_states_has_project_id=True, schema_version=12, user_version=12
    )

    result = run_runtime_coordination_migration(db_path)
    assert result["status"] == "ok"
    assert result["results"]["worker_states"] == "already_present"
    assert _project_id_count(db_path, "worker_states") == 1
