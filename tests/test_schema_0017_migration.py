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
