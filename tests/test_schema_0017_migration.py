"""Tests for migration 0017 — multi-tenant lease isolation (schema v12).

Covers:
- Apply from pre-migration state (original 15-col dispatches)
- Regression: apply when dispatches has 20 columns (full live schema)
- Regression: apply when terminal_leases has worker_pid column
- Minimal-column dispatches still rebuilds correctly (dynamic, not hardcoded 20)
- Idempotency (clean second-run no-op)
- Rollback on internal error (DB left unchanged)
- Composite UNIQUE enforcement after migration
- worker_states.project_id presence
- ADR-005 audit event emission
- Composite FK correctness
- Ordering guarantee (dispatches rebuilt before terminal_leases)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/lib importable
_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import migrations.apply_0017 as _mod
from migrations.apply_0017 import apply_migration

MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "migrations"
    / "0017_multi_tenant_lease_isolation.sql"
)

# Full 20-column set for dispatches as documented in the bug report
_DISPATCHES_20_COLS = [
    "id", "dispatch_id", "state", "terminal_id", "track", "priority",
    "pr_ref", "gate", "attempt_count", "bundle_path", "created_at",
    "updated_at", "expires_after", "metadata_json", "task_class",
    "target_type", "target_id", "channel_origin", "intelligence_payload",
    "project_id",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_pre_migration_db(db_path: Path) -> None:
    """Build a minimal DB that represents the pre-0017 state (v11).

    terminal_leases and dispatches have project_id (from 0010) but only
    single-column UNIQUE constraints. worker_states has no project_id.
    dispatch_attempts has project_id (from 0010) with single-column FK.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            PRAGMA journal_mode = WAL;

            CREATE TABLE runtime_schema_version (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                description TEXT NOT NULL
            );
            INSERT INTO runtime_schema_version VALUES (1, datetime('now'), 'initial');
            INSERT INTO runtime_schema_version VALUES (9, datetime('now'), 'worker_states');
            INSERT INTO runtime_schema_version VALUES (10, datetime('now'), 'project_id phase 0');
            INSERT INTO runtime_schema_version VALUES (11, datetime('now'), 'project_id phase 4');

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

            CREATE TABLE worker_states (
                terminal_id      TEXT    NOT NULL PRIMARY KEY,
                dispatch_id      TEXT    NOT NULL,
                state            TEXT    NOT NULL DEFAULT 'initializing',
                last_output_at   TEXT,
                state_entered_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                stall_count      INTEGER NOT NULL DEFAULT 0,
                blocked_reason   TEXT,
                metadata_json    TEXT,
                created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

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
        """)
    finally:
        conn.close()


def _create_20col_dispatches_db(db_path: Path) -> None:
    """Build a DB where dispatches has the full 20-column live schema.

    Simulates user_version=10 with all 5 extra columns already present via
    ALTER TABLE ADD COLUMN (as happens in the production runtime). This is the
    exact state that caused the 'table dispatches_v10 has 15 columns but 20
    values were supplied' crash in the original static SQL migration.
    """
    _create_pre_migration_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        # Add the 5 columns that were added by later migrations in production
        for col, typedef in [
            ("task_class", "TEXT"),
            ("target_type", "TEXT"),
            ("target_id", "TEXT"),
            ("channel_origin", "TEXT"),
            ("intelligence_payload", "TEXT"),
        ]:
            conn.execute(f"ALTER TABLE dispatches ADD COLUMN {col} {typedef}")

        # Insert a row to verify data is preserved after rebuild
        conn.execute(
            "INSERT INTO dispatches"
            " (dispatch_id, project_id, state, task_class, channel_origin)"
            " VALUES ('d-test-1', 'vnx-dev', 'queued', 'codex_gate', 'T1')"
        )
        conn.commit()
    finally:
        conn.close()


# Single-line DDL (no newlines) — the exact shape stored in sqlite_master that
# defeated the old start-of-line regex. The single-column UNIQUE(dispatch_id) /
# UNIQUE(terminal_id) must still be dropped by the PRAGMA-built rebuild.
_DISPATCHES_DDL_SINGLE_LINE = (
    "CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "dispatch_id TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
    "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
    "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
    "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    "expires_after TEXT, metadata_json TEXT DEFAULT '{}')"
)
_TERMINAL_LEASES_DDL_SINGLE_LINE = (
    "CREATE TABLE terminal_leases (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "terminal_id TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
    "state TEXT NOT NULL DEFAULT 'idle', dispatch_id TEXT, "
    "generation INTEGER NOT NULL DEFAULT 1, leased_at TEXT, expires_at TEXT, "
    "last_heartbeat_at TEXT, released_at TEXT, metadata_json TEXT DEFAULT '{}')"
)

# Comma-PREFIXED column style — the comma leads each line, so the column name is
# never at the start of a line either. Also defeated the old regex.
_DISPATCHES_DDL_COMMA_PREFIXED = """CREATE TABLE dispatches (
      id INTEGER PRIMARY KEY AUTOINCREMENT
    , dispatch_id TEXT NOT NULL UNIQUE
    , project_id TEXT NOT NULL DEFAULT 'vnx-dev'
    , state TEXT NOT NULL DEFAULT 'queued'
    , terminal_id TEXT
    , track TEXT
    , priority TEXT DEFAULT 'P2'
    , pr_ref TEXT
    , gate TEXT
    , attempt_count INTEGER NOT NULL DEFAULT 0
    , bundle_path TEXT
    , created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    , updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    , expires_after TEXT
    , metadata_json TEXT DEFAULT '{}'
)"""
_TERMINAL_LEASES_DDL_COMMA_PREFIXED = """CREATE TABLE terminal_leases (
      id INTEGER PRIMARY KEY AUTOINCREMENT
    , terminal_id TEXT NOT NULL UNIQUE
    , project_id TEXT NOT NULL DEFAULT 'vnx-dev'
    , state TEXT NOT NULL DEFAULT 'idle'
    , dispatch_id TEXT
    , generation INTEGER NOT NULL DEFAULT 1
    , leased_at TEXT
    , expires_at TEXT
    , last_heartbeat_at TEXT
    , released_at TEXT
    , metadata_json TEXT DEFAULT '{}'
)"""

_DISPATCHES_15_COLS = [
    "id", "dispatch_id", "project_id", "state", "terminal_id", "track",
    "priority", "pr_ref", "gate", "attempt_count", "bundle_path",
    "created_at", "updated_at", "expires_after", "metadata_json",
]


def _create_styled_pre_migration_db(
    db_path: Path, dispatches_ddl: str, terminal_leases_ddl: str
) -> None:
    """Build the pre-migration DB but with dispatches + terminal_leases declared
    using the given DDL style (single-line or comma-prefixed).

    Seeds one dispatches row and two terminal_leases rows so data preservation
    through the rebuild can be asserted. The stored sqlite_master.sql keeps the
    exact formatting of *dispatches_ddl* / *terminal_leases_ddl*, which is what
    reproduces the regex false-positive the PRAGMA-built rebuild closes.
    """
    _create_pre_migration_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        # FK enforcement defaults OFF, so dropping a referenced table is allowed.
        conn.execute("DROP TABLE dispatches")
        conn.execute("DROP TABLE terminal_leases")
        conn.execute(dispatches_ddl)
        conn.execute(terminal_leases_ddl)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state)"
            " VALUES ('styled-d1', 'vnx-dev', 'queued')"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, state, generation)"
            " VALUES ('T1', 'idle', 1), ('T2', 'idle', 1)"
        )
        conn.commit()
    finally:
        conn.close()


def _create_terminal_leases_with_worker_pid_db(db_path: Path) -> None:
    """Build a DB where terminal_leases has the worker_pid column (added by #636).

    The static SQL migration would silently DROP worker_pid because its
    INSERT did not include it. The dynamic rebuild must preserve it.
    """
    _create_pre_migration_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("ALTER TABLE terminal_leases ADD COLUMN worker_pid INTEGER")
        # Set a PID on T1 to verify the value survives the rebuild
        conn.execute(
            "UPDATE terminal_leases SET worker_pid = 12345 WHERE terminal_id = 'T1'"
        )
        conn.commit()
    finally:
        conn.close()


def _has_composite_unique(db_path: Path, table: str, columns: frozenset) -> bool:
    """Return True if the table has a UNIQUE index over exactly the given columns."""
    conn = sqlite3.connect(str(db_path))
    try:
        indices = conn.execute(f"PRAGMA index_list({table})").fetchall()
        for idx in indices:
            if not idx[2]:  # unique flag
                continue
            info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
            idx_cols = frozenset(row[2] for row in info)
            if idx_cols == columns:
                return True
    finally:
        conn.close()
    return False


def _max_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT MAX(version) FROM runtime_schema_version").fetchone()
        return int(row[0]) if (row and row[0] is not None) else 0
    finally:
        conn.close()


def _get_columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


def _read_ndjson_events(events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Core migration tests (original 15-col dispatches)
# ---------------------------------------------------------------------------

def test_apply_migration_from_v9_succeeds(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    result = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert result is True
    assert _max_version(db) == 12
    assert _has_composite_unique(db, "terminal_leases", frozenset({"terminal_id", "project_id"}))
    assert _has_composite_unique(db, "dispatches", frozenset({"dispatch_id", "project_id"}))


def test_apply_migration_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    first = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)
    second = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert first is True
    assert second is False
    assert _max_version(db) == 12


def test_terminal_leases_unique_constraint(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        # (T1, proj-a) and (T1, proj-b) in the same table are allowed
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, generation)"
            " VALUES ('T1', 'proj-a', 'idle', 1)"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, generation)"
            " VALUES ('T1', 'proj-b', 'idle', 1)"
        )
        conn.commit()

        # Duplicate (T1, proj-a) must raise IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminal_leases (terminal_id, project_id, state, generation)"
                " VALUES ('T1', 'proj-a', 'idle', 2)"
            )
            conn.commit()
    finally:
        conn.close()


def test_worker_states_has_project_id_after_migration(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    cols = set(_get_columns(db, "worker_states"))
    assert "project_id" in cols


def test_migration_emits_started_completed_events(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    events_path = tmp_path / "events" / "schema_migrations.ndjson"
    events = _read_ndjson_events(events_path)
    assert len(events) == 2
    assert events[0]["event_type"] == "migration_started"
    assert events[1]["event_type"] == "migration_completed"
    assert events[0]["migration"] == "0017_multi_tenant_lease_isolation"
    assert events[1]["migration"] == "0017_multi_tenant_lease_isolation"


def test_composite_fk_referencing_dispatches(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state)"
            " VALUES ('d1', 'proj-a', 'queued')"
        )
        conn.commit()

        # terminal_leases: valid (dispatch_id, project_id) pair → success
        conn.execute(
            "INSERT INTO terminal_leases"
            " (terminal_id, project_id, state, dispatch_id, generation)"
            " VALUES ('T4', 'proj-a', 'leased', 'd1', 1)"
        )
        conn.commit()

        # terminal_leases: unknown dispatch_id → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminal_leases"
                " (terminal_id, project_id, state, dispatch_id, generation)"
                " VALUES ('T5', 'proj-a', 'leased', 'nonexistent', 1)"
            )
            conn.commit()

        # dispatch_attempts: valid (dispatch_id, project_id) pair → success
        conn.execute(
            "INSERT INTO dispatch_attempts"
            " (attempt_id, dispatch_id, project_id, terminal_id)"
            " VALUES ('a1', 'd1', 'proj-a', 'T1')"
        )
        conn.commit()

        # dispatch_attempts: unknown dispatch → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatch_attempts"
                " (attempt_id, dispatch_id, project_id, terminal_id)"
                " VALUES ('a2', 'nonexistent', 'proj-a', 'T1')"
            )
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regression: 20-column dispatches (the crash this fix addresses)
# ---------------------------------------------------------------------------

def test_apply_migration_20col_dispatches_succeeds(tmp_path: Path) -> None:
    """Regression: apply_0017 must NOT crash when dispatches has 20 columns.

    Verifies the exact failure mode: 'table dispatches_v10 has 15 columns but
    20 values were supplied'. The dynamic rebuild reads the actual column list
    from PRAGMA table_info instead of using a hardcoded SELECT *.
    """
    db = tmp_path / "coord.db"
    _create_20col_dispatches_db(db)

    result = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert result is True
    assert _max_version(db) == 12


def test_apply_migration_20col_dispatches_preserves_all_columns(tmp_path: Path) -> None:
    """After migration, all 20 original columns are present in dispatches."""
    db = tmp_path / "coord.db"
    _create_20col_dispatches_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    cols = set(_get_columns(db, "dispatches"))
    for col in _DISPATCHES_20_COLS:
        assert col in cols, f"column '{col}' missing from dispatches after migration"


def test_apply_migration_20col_dispatches_has_composite_unique(tmp_path: Path) -> None:
    """After migration, dispatches has UNIQUE(dispatch_id, project_id)."""
    db = tmp_path / "coord.db"
    _create_20col_dispatches_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert _has_composite_unique(
        db, "dispatches", frozenset({"dispatch_id", "project_id"})
    )


def test_apply_migration_20col_dispatches_preserves_row_data(tmp_path: Path) -> None:
    """Row data inserted before migration is preserved after the rebuild."""
    db = tmp_path / "coord.db"
    _create_20col_dispatches_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT dispatch_id, task_class, channel_origin"
            " FROM dispatches WHERE dispatch_id = 'd-test-1'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "pre-migration row was lost during rebuild"
    assert row[0] == "d-test-1"
    assert row[1] == "codex_gate"
    assert row[2] == "T1"


def test_apply_migration_20col_dispatches_idempotent(tmp_path: Path) -> None:
    """Second run is a clean no-op — does not re-apply or crash."""
    db = tmp_path / "coord.db"
    _create_20col_dispatches_db(db)

    first = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)
    second = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert first is True
    assert second is False
    assert _max_version(db) == 12
    # All 20 cols still present after second run
    cols = set(_get_columns(db, "dispatches"))
    for col in _DISPATCHES_20_COLS:
        assert col in cols


# ---------------------------------------------------------------------------
# Regression: terminal_leases with worker_pid (data-loss fix)
# ---------------------------------------------------------------------------

def test_apply_migration_terminal_leases_worker_pid_preserved(tmp_path: Path) -> None:
    """Regression: worker_pid must NOT be silently dropped from terminal_leases.

    The static SQL migration did not include worker_pid in the column list,
    causing silent data loss. The dynamic rebuild preserves all existing columns.
    """
    db = tmp_path / "coord.db"
    _create_terminal_leases_with_worker_pid_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    cols = set(_get_columns(db, "terminal_leases"))
    assert "worker_pid" in cols, "worker_pid column was lost during terminal_leases rebuild"


def test_apply_migration_terminal_leases_worker_pid_value_preserved(tmp_path: Path) -> None:
    """The worker_pid value written before migration survives the rebuild."""
    db = tmp_path / "coord.db"
    _create_terminal_leases_with_worker_pid_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT worker_pid FROM terminal_leases WHERE terminal_id = 'T1'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == 12345, f"worker_pid was not preserved; got {row[0]}"


def test_apply_migration_terminal_leases_worker_pid_has_composite_unique(tmp_path: Path) -> None:
    """Composite UNIQUE(terminal_id, project_id) is present after migration with worker_pid."""
    db = tmp_path / "coord.db"
    _create_terminal_leases_with_worker_pid_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert _has_composite_unique(
        db, "terminal_leases", frozenset({"terminal_id", "project_id"})
    )


# ---------------------------------------------------------------------------
# Dynamic (not hardcoded): minimal-column dispatches also works
# ---------------------------------------------------------------------------

def test_apply_migration_minimal_dispatches_succeeds(tmp_path: Path) -> None:
    """The rebuild is dynamic — a dispatches table with fewer than 20 columns works.

    Uses 16 columns (standard 15 + one extra: task_class) to prove the fix is
    not hardcoded to the 20-column live schema either. The dynamic rebuild
    adapts to whatever columns are actually present.
    """
    # Start from the standard 15-col pre-migration schema, then add one extra column.
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    conn = sqlite3.connect(str(db))
    try:
        # Add ONE extra column — 16 cols total, not 20, not 15
        conn.execute("ALTER TABLE dispatches ADD COLUMN task_class TEXT")
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, task_class)"
            " VALUES ('min-d1', 'vnx-dev', 'queued', 'codex_gate')"
        )
        conn.commit()
    finally:
        conn.close()

    result = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert result is True
    assert _max_version(db) == 12
    assert _has_composite_unique(db, "dispatches", frozenset({"dispatch_id", "project_id"}))
    assert _has_composite_unique(db, "terminal_leases", frozenset({"terminal_id", "project_id"}))

    # Extra column is preserved
    cols = set(_get_columns(db, "dispatches"))
    assert "task_class" in cols

    # Pre-migration row survived with all data intact
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT dispatch_id, task_class FROM dispatches WHERE dispatch_id = 'min-d1'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "min-d1" and row[1] == "codex_gate"


# ---------------------------------------------------------------------------
# Rollback on internal error
# ---------------------------------------------------------------------------

def test_apply_migration_rollback_on_error(tmp_path: Path, monkeypatch) -> None:
    """If a rebuild step fails, the entire transaction is rolled back.

    Verifies that worker_states.project_id (added before the failing step)
    is NOT visible after the rollback — the ADD COLUMN was rolled back too.
    """
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    # Patch _rebuild_dispatches to raise after the migration has already
    # executed the worker_states ADD COLUMN step.
    original_rebuild = _mod._rebuild_dispatches

    def _failing_rebuild(conn):
        raise sqlite3.OperationalError("simulated failure mid-migration for test")

    monkeypatch.setattr(_mod, "_rebuild_dispatches", _failing_rebuild)

    with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
        apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    # Transaction must have been rolled back — worker_states.project_id absent
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(worker_states)")}
    finally:
        conn.close()
    assert "project_id" not in cols, (
        "worker_states.project_id visible after rollback — ADD COLUMN not rolled back"
    )

    # DB version must still be pre-migration
    assert _max_version(db) == 11


def test_migration_emits_failed_event_on_rollback(tmp_path: Path, monkeypatch) -> None:
    """A migration_failed event is emitted when the migration raises."""
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    def _always_raise(conn):
        raise sqlite3.OperationalError("forced failure for audit event test")

    monkeypatch.setattr(_mod, "_rebuild_dispatches", _always_raise)

    with pytest.raises(sqlite3.OperationalError):
        apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    events_path = tmp_path / "events" / "schema_migrations.ndjson"
    events = _read_ndjson_events(events_path)
    event_types = [e["event_type"] for e in events]
    assert "migration_failed" in event_types
    failed = next(e for e in events if e["event_type"] == "migration_failed")
    assert "error" in failed


# ---------------------------------------------------------------------------
# Ordering guarantee (dispatches rebuilt before terminal_leases)
# ---------------------------------------------------------------------------

def test_migration_dispatches_rebuilt_before_leases(tmp_path: Path) -> None:
    """Migration must rebuild dispatches before terminal_leases.

    terminal_leases carries FK → dispatches(dispatch_id, project_id). The
    composite UNIQUE on dispatches must exist before terminal_leases is
    rebuilt with the composite FK. This test verifies the post-migration
    FK constraint actually works (would fail at INSERT time if ordering wrong).
    """
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # Insert a dispatch first
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state)"
            " VALUES ('ord-d1', 'proj-a', 'queued')"
        )
        conn.commit()

        # Insert a lease referencing that dispatch — must succeed
        conn.execute(
            "INSERT INTO terminal_leases"
            " (terminal_id, project_id, state, dispatch_id, generation)"
            " VALUES ('T-ord', 'proj-a', 'leased', 'ord-d1', 1)"
        )
        conn.commit()

        # Verify composite UNIQUE on dispatches exists (prerequisite for FK)
        assert _has_composite_unique(
            db, "dispatches", frozenset({"dispatch_id", "project_id"})
        )
        assert _has_composite_unique(
            db, "terminal_leases", frozenset({"terminal_id", "project_id"})
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regex false-positive closure: single-line + comma-prefixed DDL
#
# The old rebuild regex-stripped the single-column UNIQUE only when the column
# definition was anchored at the start of a line. Single-line DDL (as stored in
# sqlite_master) and comma-prefixed column styles slipped through, leaving the
# pre-migration single-column UNIQUE in place while the composite UNIQUE was
# added on top — a false-positive "migrated" stamp. The PRAGMA-built rebuild
# constructs the new DDL from table_info/index_list, so the old single-column
# UNIQUE is provably absent (never copied). These tests fail under the old regex
# approach and pass under the PRAGMA-built rebuild.
# ---------------------------------------------------------------------------


def _assert_styled_rebuild_correct(db: Path, tmp_path: Path) -> None:
    """Shared assertions for a styled-DDL rebuild: composite present, the old
    single-column UNIQUE gone, all columns + seeded data preserved."""
    result = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)
    assert result is True
    assert _max_version(db) == 12

    # Composite UNIQUE present on both hot tables.
    assert _has_composite_unique(db, "dispatches", frozenset({"dispatch_id", "project_id"}))
    assert _has_composite_unique(
        db, "terminal_leases", frozenset({"terminal_id", "project_id"})
    )

    # The old single-column UNIQUE is GONE (this is what the false-positive kept).
    # _has_composite_unique matches an index over EXACTLY the given column set, so
    # a single-element frozenset detects a surviving single-column UNIQUE.
    assert not _has_composite_unique(db, "dispatches", frozenset({"dispatch_id"})), (
        "single-column UNIQUE(dispatch_id) survived the rebuild — false positive open"
    )
    assert not _has_composite_unique(db, "terminal_leases", frozenset({"terminal_id"})), (
        "single-column UNIQUE(terminal_id) survived the rebuild — false positive open"
    )

    # All 15 columns preserved on dispatches.
    cols = set(_get_columns(db, "dispatches"))
    for col in _DISPATCHES_15_COLS:
        assert col in cols, f"column '{col}' missing from dispatches after rebuild"

    # Seeded rows preserved.
    conn = sqlite3.connect(str(db))
    try:
        d_row = conn.execute(
            "SELECT dispatch_id, state FROM dispatches WHERE dispatch_id = 'styled-d1'"
        ).fetchone()
        lease_count = conn.execute("SELECT COUNT(*) FROM terminal_leases").fetchone()[0]
    finally:
        conn.close()
    assert d_row == ("styled-d1", "queued"), "dispatches row lost during rebuild"
    assert lease_count == 2, f"terminal_leases rows lost during rebuild (got {lease_count})"


def test_apply_migration_single_line_ddl(tmp_path: Path) -> None:
    """Single-line DDL: rebuild succeeds, old single-column UNIQUE gone, data kept."""
    db = tmp_path / "coord.db"
    _create_styled_pre_migration_db(
        db, _DISPATCHES_DDL_SINGLE_LINE, _TERMINAL_LEASES_DDL_SINGLE_LINE
    )
    _assert_styled_rebuild_correct(db, tmp_path)


def test_apply_migration_comma_prefixed_ddl(tmp_path: Path) -> None:
    """Comma-prefixed DDL: rebuild succeeds, old single-column UNIQUE gone, data kept."""
    db = tmp_path / "coord.db"
    _create_styled_pre_migration_db(
        db, _DISPATCHES_DDL_COMMA_PREFIXED, _TERMINAL_LEASES_DDL_COMMA_PREFIXED
    )
    _assert_styled_rebuild_correct(db, tmp_path)


def test_single_line_ddl_constraint_list_has_no_single_column_unique(tmp_path: Path) -> None:
    """Prove the false positive is closed: after a single-line-DDL rebuild, NO
    UNIQUE index over exactly {dispatch_id} (or {terminal_id}) remains."""
    db = tmp_path / "coord.db"
    _create_styled_pre_migration_db(
        db, _DISPATCHES_DDL_SINGLE_LINE, _TERMINAL_LEASES_DDL_SINGLE_LINE
    )
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        for table, single_col in (("dispatches", "dispatch_id"), ("terminal_leases", "terminal_id")):
            unique_column_sets = []
            for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
                if not idx[2]:  # unique flag
                    continue
                info = conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()
                unique_column_sets.append(frozenset(r[2] for r in info))
            assert frozenset({single_col}) not in unique_column_sets, (
                f"{table}: single-column UNIQUE({single_col}) still in constraint list"
            )
            assert frozenset({single_col, "project_id"}) in unique_column_sets, (
                f"{table}: composite UNIQUE({single_col}, project_id) missing"
            )
    finally:
        conn.close()


def test_comma_prefixed_ddl_preserves_non_replaced_unique(tmp_path: Path) -> None:
    """The rebuild drops ONLY the replaced single-column UNIQUE — a non-replaced
    UNIQUE (dispatch_attempts.attempt_id) must survive."""
    db = tmp_path / "coord.db"
    _create_styled_pre_migration_db(
        db, _DISPATCHES_DDL_COMMA_PREFIXED, _TERMINAL_LEASES_DDL_COMMA_PREFIXED
    )
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    # attempt_id UNIQUE must still be enforced after dispatch_attempts rebuild.
    assert _has_composite_unique(db, "dispatch_attempts", frozenset({"attempt_id"})), (
        "attempt_id UNIQUE was dropped — non-replaced constraint lost in rebuild"
    )
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state)"
            " VALUES ('dup-d', 'vnx-dev', 'queued')"
        )
        conn.execute(
            "INSERT INTO dispatch_attempts (attempt_id, dispatch_id, project_id, terminal_id)"
            " VALUES ('att-dup', 'dup-d', 'vnx-dev', 'T1')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatch_attempts (attempt_id, dispatch_id, project_id, terminal_id)"
                " VALUES ('att-dup', 'dup-d', 'vnx-dev', 'T2')"
            )
            conn.commit()
    finally:
        conn.close()
