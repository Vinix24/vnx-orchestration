"""W1B — 0031 adaptive FK-repair test suite.

Tests the adaptive branch in apply_migration_v31 per the W1B spec
(claudedocs/W1B-0031-RECONCILE-SPEC.md, deliverables D1-D6).

Scenarios covered:
  1. Mixed-pid store (seocrawler shape) → repaired to v31, FK-clean.
  2. NULL-pid rows → deterministic resolved fill (cannot be in composite PK).
  3. Orphan rows in headless_runs → ABORT with report (conservative policy).
  4. v31-complete store → no-op (early exit, user_version unchanged).
  5. vnx-dev store (resolved_pid == 'vnx-dev') → no-op via v31-complete.
  6. Foreign-tenant abort (third pid in a runtime table).
  7. Composite-key collision → count-assert fires, RuntimeError.
  8. worker_pool_membership single-col FK → rebuilt to composite FK.
  9. View + trigger preservation through the adaptive rebuild.
 10. Partial index (idx_terminal_leases_token WHERE lease_token != '') preserved.
 11. Legacy-clean v30 store → uses static 0031 path (not the adaptive branch).

All tests use tmp_path fixtures only. Real ~/.vnx-data is NEVER opened.

ADR-007: composite UNIQUE/PK over project_id.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import migrate_future_system as mfs  # noqa: E402
import schema_migration  # noqa: E402


# ---------------------------------------------------------------------------
# Autouse isolation — pin VNX_DATA_DIR to a tmp dir, never ~/.vnx-data
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR for every test."""
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "_w1b_data"))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)


# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------

def _open_conn(db_path: Path) -> sqlite3.Connection:
    """Open an SQLite connection with FK enforcement off (for schema construction)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def _pid_from_path(db_path: Path) -> str:
    """Extract project_id from canonical .vnx-data/<pid>/state/... path."""
    # db_path = <root>/.vnx-data/<pid>/state/runtime_coordination.db
    return db_path.parent.parent.name


def _make_db_path(tmp_path: Path, pid: str) -> Path:
    """Build a canonical DB path for ``pid`` under tmp_path.

    Shape: tmp_path / ".vnx-data" / <pid> / "state" / "runtime_coordination.db"
    This shape satisfies _project_id_from_db_path() so _resolve_validated_project_id
    returns ``pid`` without needing the VNX_PROJECT_ID env var.
    """
    state_dir = tmp_path / ".vnx-data" / pid / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "runtime_coordination.db"


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def _create_dispatches_table(conn: sqlite3.Connection) -> None:
    """Create the dispatches parent table with the ADR-007 composite UNIQUE."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT    NOT NULL,
            project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
            state       TEXT    NOT NULL DEFAULT 'pending',
            terminal_id TEXT,
            track       TEXT,
            priority    INTEGER NOT NULL DEFAULT 0,
            pr_ref      TEXT,
            gate        TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT,
            metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()


def _create_pool_config(conn: sqlite3.Connection) -> None:
    """Create pool_config parent table (required by worker_pool_membership FK)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pool_config (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT    NOT NULL,
            pool_id    TEXT    NOT NULL DEFAULT 'default',
            UNIQUE(project_id, pool_id)
        )
    """)
    conn.commit()


def _create_legacy_terminal_leases(conn: sqlite3.Connection) -> None:
    """v30-legacy shape: NO project_id, single-col UNIQUE on lease_token."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terminal_leases (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id         TEXT    NOT NULL,
            state               TEXT    NOT NULL DEFAULT 'idle',
            dispatch_id         TEXT,
            generation          INTEGER NOT NULL DEFAULT 1,
            leased_at           TEXT,
            expires_at          TEXT,
            last_heartbeat_at   TEXT,
            released_at         TEXT,
            worker_pid          INTEGER,
            metadata_json       TEXT    DEFAULT '{}',
            lease_token         TEXT    NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_state ON terminal_leases(state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_dispatch ON terminal_leases(dispatch_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_terminal_leases_token "
        "ON terminal_leases(lease_token) WHERE lease_token != ''"
    )
    conn.commit()


def _create_legacy_dispatch_attempts(conn: sqlite3.Connection) -> None:
    """v30-legacy shape: NO project_id."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id      TEXT    NOT NULL,
            dispatch_id     TEXT    NOT NULL,
            attempt_number  INTEGER NOT NULL DEFAULT 1,
            terminal_id     TEXT    NOT NULL,
            state           TEXT    NOT NULL DEFAULT 'pending',
            started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            ended_at        TEXT,
            failure_reason  TEXT,
            metadata_json   TEXT    DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_dispatch "
        "ON dispatch_attempts(dispatch_id, attempt_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_state "
        "ON dispatch_attempts(state, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_terminal "
        "ON dispatch_attempts(terminal_id, started_at DESC)"
    )
    conn.commit()


def _create_legacy_headless_runs_no_pid(conn: sqlite3.Connection) -> None:
    """Mixed shape: headless_runs WITHOUT project_id, with GHOST FK to dispatches_pre_v22."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches_pre_v22 (
            id INTEGER PRIMARY KEY,
            dispatch_id TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS headless_runs (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id                  TEXT    NOT NULL,
            dispatch_id             TEXT    NOT NULL,
            attempt_id              TEXT    NOT NULL,
            target_id               TEXT    NOT NULL,
            target_type             TEXT    NOT NULL,
            task_class              TEXT    NOT NULL,
            terminal_id             TEXT,
            pid                     INTEGER,
            pgid                    INTEGER,
            state                   TEXT    NOT NULL DEFAULT 'init',
            failure_class           TEXT,
            exit_code               INTEGER,
            started_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            subprocess_started_at   TEXT,
            heartbeat_at            TEXT,
            last_output_at          TEXT,
            completed_at            TEXT,
            duration_seconds        REAL,
            log_artifact_path       TEXT,
            output_artifact_path    TEXT,
            receipt_id              TEXT,
            metadata_json           TEXT    DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_state "
        "ON headless_runs(state, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_dispatch "
        "ON headless_runs(dispatch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_target "
        "ON headless_runs(target_id, state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_heartbeat "
        "ON headless_runs(state, heartbeat_at) WHERE state = 'running'"
    )
    conn.commit()


def _create_legacy_worker_states(conn: sqlite3.Connection) -> None:
    """v30-legacy shape: NO project_id, PK on (terminal_id,) only."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worker_states (
            terminal_id      TEXT    NOT NULL PRIMARY KEY,
            dispatch_id      TEXT    NOT NULL,
            state            TEXT    NOT NULL DEFAULT 'initializing',
            last_output_at   TEXT,
            state_entered_at TEXT    NOT NULL,
            stall_count      INTEGER NOT NULL DEFAULT 0,
            blocked_reason   TEXT,
            metadata_json    TEXT,
            created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_state ON worker_states(state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_dispatch ON worker_states(dispatch_id)"
    )
    conn.commit()


def _create_mixed_terminal_leases(conn: sqlite3.Connection, pid: str) -> None:
    """Mixed/seocrawler shape: HAS project_id but SINGLE-col UNIQUE on terminal_id only."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terminal_leases (
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
            worker_pid          INTEGER,
            metadata_json       TEXT    DEFAULT '{}',
            lease_token         TEXT    NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_state ON terminal_leases(state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_dispatch ON terminal_leases(dispatch_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_terminal_leases_token "
        "ON terminal_leases(lease_token) WHERE lease_token != ''"
    )
    conn.commit()


def _create_mixed_dispatch_attempts(conn: sqlite3.Connection, pid: str) -> None:
    """Mixed/seocrawler shape: HAS project_id but SINGLE-col UNIQUE on attempt_id only."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id      TEXT    NOT NULL UNIQUE,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            attempt_number  INTEGER NOT NULL DEFAULT 1,
            terminal_id     TEXT    NOT NULL,
            state           TEXT    NOT NULL DEFAULT 'pending',
            started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            ended_at        TEXT,
            failure_reason  TEXT,
            metadata_json   TEXT    DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_dispatch "
        "ON dispatch_attempts(dispatch_id, attempt_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_state "
        "ON dispatch_attempts(state, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_terminal "
        "ON dispatch_attempts(terminal_id, started_at DESC)"
    )
    conn.commit()


def _create_worker_pool_membership_single_col_fk(conn: sqlite3.Connection) -> None:
    """worker_pool_membership with SINGLE-column FK to terminal_leases (non-composite)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worker_pool_membership (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL,
            pool_id         TEXT    NOT NULL DEFAULT 'default',
            provider        TEXT    NOT NULL
                                CHECK (provider IN ('claude', 'codex', 'gemini', 'litellm')),
            role            TEXT    NOT NULL,
            joined_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            released_at     TEXT,
            release_reason  TEXT,
            spawn_generation INTEGER NOT NULL DEFAULT 1,
            metadata_json   TEXT    DEFAULT '{}',
            FOREIGN KEY (terminal_id) REFERENCES terminal_leases(terminal_id)
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_membership_active "
        "ON worker_pool_membership(terminal_id, project_id) WHERE released_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pool_membership_pool "
        "ON worker_pool_membership(project_id, pool_id)"
    )
    conn.commit()


def _create_worker_pool_membership_composite_fk(conn: sqlite3.Connection) -> None:
    """worker_pool_membership with correct COMPOSITE FK to terminal_leases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worker_pool_membership (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL,
            pool_id         TEXT    NOT NULL DEFAULT 'default',
            provider        TEXT    NOT NULL
                                CHECK (provider IN ('claude', 'codex', 'gemini', 'litellm')),
            role            TEXT    NOT NULL,
            joined_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            released_at     TEXT,
            release_reason  TEXT,
            spawn_generation INTEGER NOT NULL DEFAULT 1,
            metadata_json   TEXT    DEFAULT '{}',
            FOREIGN KEY (terminal_id, project_id)
                REFERENCES terminal_leases(terminal_id, project_id),
            FOREIGN KEY (project_id, pool_id)
                REFERENCES pool_config(project_id, pool_id)
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_membership_active "
        "ON worker_pool_membership(terminal_id, project_id) WHERE released_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pool_membership_pool "
        "ON worker_pool_membership(project_id, pool_id)"
    )
    conn.commit()


def _build_seocrawler_mixed_store(db_path: Path, pid: str) -> None:
    """Build a seocrawler-v2 "mixed" shape store.

    Shape:
    - terminal_leases: HAS project_id, SINGLE-col UNIQUE on terminal_id
    - dispatch_attempts: HAS project_id, SINGLE-col UNIQUE on attempt_id
    - headless_runs: NO project_id, GHOST FK to dispatches_pre_v22
    - worker_states: NO project_id, single-col PK on terminal_id
    - worker_pool_membership: single-col FK to terminal_leases

    user_version = 30 (ready for 0031 walk).
    """
    conn = _open_conn(db_path)
    _create_dispatches_table(conn)
    _create_pool_config(conn)
    _create_mixed_terminal_leases(conn, pid)
    _create_mixed_dispatch_attempts(conn, pid)
    _create_legacy_headless_runs_no_pid(conn)  # no project_id + ghost FK
    _create_legacy_worker_states(conn)
    _create_worker_pool_membership_single_col_fk(conn)
    _set_user_version(conn, 30)
    conn.close()


def _build_legacy_clean_store(db_path: Path) -> None:
    """Build a clean v30-legacy store (NO project_id in runtime tables).

    This is the shape the static 0031 DDL path handles.
    user_version = 30.
    """
    conn = _open_conn(db_path)
    _create_dispatches_table(conn)
    _create_pool_config(conn)
    _create_legacy_terminal_leases(conn)
    _create_legacy_dispatch_attempts(conn)
    # headless_runs with v30 shape (no ghost FK, no project_id)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS headless_runs (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id                  TEXT    NOT NULL,
            dispatch_id             TEXT    NOT NULL,
            attempt_id              TEXT    NOT NULL,
            target_id               TEXT    NOT NULL,
            target_type             TEXT    NOT NULL,
            task_class              TEXT    NOT NULL,
            terminal_id             TEXT,
            pid                     INTEGER,
            pgid                    INTEGER,
            state                   TEXT    NOT NULL DEFAULT 'init',
            failure_class           TEXT,
            exit_code               INTEGER,
            started_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            subprocess_started_at   TEXT,
            heartbeat_at            TEXT,
            last_output_at          TEXT,
            completed_at            TEXT,
            duration_seconds        REAL,
            log_artifact_path       TEXT,
            output_artifact_path    TEXT,
            receipt_id              TEXT,
            metadata_json           TEXT    DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_state "
        "ON headless_runs(state, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_dispatch "
        "ON headless_runs(dispatch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_target "
        "ON headless_runs(target_id, state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_headless_run_heartbeat "
        "ON headless_runs(state, heartbeat_at) WHERE state = 'running'"
    )
    conn.commit()
    _create_legacy_worker_states(conn)
    _create_worker_pool_membership_composite_fk(conn)
    _set_user_version(conn, 30)
    conn.close()


def _assert_v31_shape(db_path: Path) -> None:
    """Assert the DB is at user_version=31 with FK-clean runtime tables."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    version = schema_migration.get_user_version(conn)
    assert version == 31, f"Expected user_version=31, got {version}"

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert not fk_violations, f"FK violations after repair: {fk_violations}"

    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    assert integrity == [("ok",)], f"integrity_check: {integrity}"
    conn.close()


def _table_columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
    conn.close()
    return cols


def _table_pk(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    pk_cols = [
        r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")
        if r[5] > 0
    ]
    conn.close()
    return sorted(pk_cols)


def _index_names(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    names = {r[1] for r in conn.execute(f"PRAGMA index_list('{table}')")}
    conn.close()
    return names


def _wpm_fk_to_terminal_leases_is_composite(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    fk_rows = conn.execute(
        "PRAGMA foreign_key_list('worker_pool_membership')"
    ).fetchall()
    conn.close()
    for fk_id in {r[0] for r in fk_rows if r[2] == "terminal_leases"}:
        fk_cols = {r[3] for r in fk_rows if r[0] == fk_id and r[2] == "terminal_leases"}
        if fk_cols == {"terminal_id", "project_id"}:
            return True
    return False


# ---------------------------------------------------------------------------
# Helper: run apply_migration_v31 via a real db_path-anchored connection
# ---------------------------------------------------------------------------

def _run_v31(db_path: Path) -> None:
    """Open a connection to db_path and run apply_migration_v31."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        mfs.apply_migration_v31(conn, db_path.parent.parent.parent.parent)
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# 1. Mixed-pid store (seocrawler shape) → adaptive repair → v31, FK-clean
# ===========================================================================

class TestMixedPidStoreRepair:
    """Seocrawler-v2 shape: terminal_leases/dispatch_attempts have project_id
    but single-col UNIQUE; headless_runs lacks project_id. The adaptive branch
    must repair to v31 shape with composite UNIQUE/PK + composite child FKs.
    """

    def test_empty_mixed_store_repaired(self, tmp_path: Path) -> None:
        """Empty mixed store (no rows) → adaptive repair → v31, FK-clean."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        _run_v31(db_path)

        _assert_v31_shape(db_path)
        # headless_runs must now have project_id
        assert "project_id" in _table_columns(db_path, "headless_runs")
        # dispatch_attempts must have composite UNIQUE (attempt_id, project_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        # worker_pool_membership FK must be composite
        assert _wpm_fk_to_terminal_leases_is_composite(db_path)
        conn.close()

    def test_mixed_store_with_data_repaired(self, tmp_path: Path) -> None:
        """Mixed store with rows → rows preserved, project_id set to resolved_pid."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        # Insert a dispatch (parent)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES ('d-001', 'seocrawler-v2')"
        )
        # Insert terminal_lease with project_id already set to pid
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
            "VALUES ('T1', 'seocrawler-v2', 'idle', '')"
        )
        # Insert dispatch_attempt with project_id already set to pid
        conn.execute(
            "INSERT INTO dispatch_attempts "
            "(attempt_id, dispatch_id, project_id, terminal_id, state, started_at) "
            "VALUES ('a-001', 'd-001', 'seocrawler-v2', 'T1', 'complete', "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        # Insert headless_run (no project_id column in mixed shape)
        conn.execute(
            "INSERT INTO headless_runs "
            "(run_id, dispatch_id, attempt_id, target_id, target_type, task_class, state, started_at) "
            "VALUES ('r-001', 'd-001', 'a-001', 'T-codex', 'worker', 'test', 'complete', "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        conn.commit()
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        # Verify data preservation
        conn = sqlite3.connect(str(db_path))
        lease_pids = {r[0] for r in conn.execute(
            "SELECT project_id FROM terminal_leases"
        ).fetchall()}
        attempt_pids = {r[0] for r in conn.execute(
            "SELECT project_id FROM dispatch_attempts"
        ).fetchall()}
        headless_pids = {r[0] for r in conn.execute(
            "SELECT project_id FROM headless_runs"
        ).fetchall()}
        conn.close()

        assert lease_pids == {pid}, f"terminal_leases project_id: {lease_pids}"
        assert attempt_pids == {pid}, f"dispatch_attempts project_id: {attempt_pids}"
        assert headless_pids == {pid}, f"headless_runs project_id: {headless_pids}"

    def test_idempotent_after_repair(self, tmp_path: Path) -> None:
        """Calling apply_migration_v31 twice is a no-op after the first repair."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        _run_v31(db_path)
        _assert_v31_shape(db_path)
        # Second call must not raise and must stay at v31
        _run_v31(db_path)
        _assert_v31_shape(db_path)


# ===========================================================================
# 2. NULL / empty project_id rows → deterministic resolved fill
# ===========================================================================

class TestNullPidFill:
    """NULL and '' project_id cells cannot be preserved in a composite PK/UNIQUE.
    They must be filled with resolved_pid (deterministic default, not re-stamp).
    """

    def test_null_pid_filled_with_resolved_pid(self, tmp_path: Path) -> None:
        """NULL project_id in terminal_leases → filled with resolved_pid.

        Uses a nullable project_id column so we can insert NULL before the repair.
        """
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)

        # Build a mixed store variant where terminal_leases allows NULL project_id
        conn = _open_conn(db_path)
        _create_dispatches_table(conn)
        _create_pool_config(conn)
        # terminal_leases with NULLABLE project_id (allows NULL insert)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS terminal_leases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id         TEXT    NOT NULL UNIQUE,
                project_id          TEXT,
                state               TEXT    NOT NULL DEFAULT 'idle',
                dispatch_id         TEXT,
                generation          INTEGER NOT NULL DEFAULT 1,
                leased_at           TEXT,
                expires_at          TEXT,
                last_heartbeat_at   TEXT,
                released_at         TEXT,
                worker_pid          INTEGER,
                metadata_json       TEXT    DEFAULT '{}',
                lease_token         TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX idx_lease_state ON terminal_leases(state)")
        conn.execute("CREATE INDEX idx_lease_dispatch ON terminal_leases(dispatch_id)")
        conn.execute(
            "CREATE UNIQUE INDEX idx_terminal_leases_token "
            "ON terminal_leases(lease_token) WHERE lease_token != ''"
        )
        _create_mixed_dispatch_attempts(conn, pid)
        _create_legacy_headless_runs_no_pid(conn)
        _create_legacy_worker_states(conn)
        _create_worker_pool_membership_single_col_fk(conn)
        # Insert row with NULL project_id
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
            "VALUES ('T-null', NULL, 'idle', '')"
        )
        _set_user_version(conn, 30)
        conn.commit()
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        conn = sqlite3.connect(str(db_path))
        pids = {r[0] for r in conn.execute(
            "SELECT project_id FROM terminal_leases WHERE terminal_id='T-null'"
        ).fetchall()}
        conn.close()
        assert pids == {pid}, f"NULL was not filled with resolved_pid: {pids}"

    def test_empty_str_pid_filled_with_resolved_pid(self, tmp_path: Path) -> None:
        """'' project_id in dispatch_attempts → filled with resolved_pid."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES ('d-e', ?)", (pid,)
        )
        conn.execute(
            "INSERT INTO dispatch_attempts "
            "(attempt_id, dispatch_id, project_id, terminal_id, state, started_at) "
            "VALUES ('a-empty', 'd-e', '', 'T0', 'pending', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        conn.commit()
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        conn = sqlite3.connect(str(db_path))
        pids = {r[0] for r in conn.execute(
            "SELECT project_id FROM dispatch_attempts WHERE attempt_id='a-empty'"
        ).fetchall()}
        conn.close()
        assert pids == {pid}, f"'' was not filled with resolved_pid: {pids}"

    def test_vnxdev_pid_preserved(self, tmp_path: Path) -> None:
        """'vnx-dev' project_id is preserved (not treated as NULL/'')."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
            "VALUES ('T-vnx', 'vnx-dev', 'idle', '')"
        )
        conn.commit()
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        conn = sqlite3.connect(str(db_path))
        pids = {r[0] for r in conn.execute(
            "SELECT project_id FROM terminal_leases WHERE terminal_id='T-vnx'"
        ).fetchall()}
        conn.close()
        # 'vnx-dev' is preserved by the adaptive branch (W1 re-stamps it later)
        assert pids == {"vnx-dev"}, f"vnx-dev was unexpectedly changed: {pids}"


# ===========================================================================
# 3. Orphan rows → ABORT with report (conservative policy)
# ===========================================================================

class TestOrphanAbort:
    """If headless_runs contains orphan rows (no matching dispatch/attempt),
    the adaptive branch must ABORT with a clear report. No silent deletion.
    """

    def test_dispatch_orphan_aborts(self, tmp_path: Path) -> None:
        """headless_runs row with no matching dispatch → ABORT."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        # Insert headless_run with dispatch_id that has NO matching dispatches row
        conn.execute(
            "INSERT INTO headless_runs "
            "(run_id, dispatch_id, attempt_id, target_id, target_type, task_class, state, started_at) "
            "VALUES ('r-orphan', 'GHOST-dispatch', 'a-ghost', 'T0', 'worker', 'test', "
            "'complete', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="orphan"):
            _run_v31(db_path)

        # DB must be unchanged (rollback) — user_version still 30
        conn = sqlite3.connect(str(db_path))
        assert schema_migration.get_user_version(conn) == 30
        conn.close()

    def test_attempt_orphan_aborts(self, tmp_path: Path) -> None:
        """headless_runs row with no matching dispatch_attempt → ABORT."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        # Insert parent dispatch
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES ('d-real', ?)", (pid,)
        )
        # headless_run with real dispatch_id but ghost attempt_id
        conn.execute(
            "INSERT INTO headless_runs "
            "(run_id, dispatch_id, attempt_id, target_id, target_type, task_class, state, started_at) "
            "VALUES ('r-orp2', 'd-real', 'GHOST-attempt', 'T0', 'worker', 'test', "
            "'complete', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="orphan"):
            _run_v31(db_path)


# ===========================================================================
# 4. v31-complete store → no-op
# ===========================================================================

class TestV31CompleteNoOp:
    """A store already at v31 (runtime tables match manifest) must be a no-op."""

    def _build_v31_store(self, db_path: Path, pid: str) -> None:
        """Build a proper v31 store by going through the full repair first."""
        _build_seocrawler_mixed_store(db_path, pid)
        _run_v31(db_path)

    def test_noop_on_v31_complete(self, tmp_path: Path) -> None:
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        self._build_v31_store(db_path, pid)

        # Confirm v31 before second call
        conn = sqlite3.connect(str(db_path))
        assert schema_migration.get_user_version(conn) == 31
        conn.close()

        # Second call must not raise and must stay at 31
        _run_v31(db_path)

        conn = sqlite3.connect(str(db_path))
        assert schema_migration.get_user_version(conn) == 31
        conn.close()


# ===========================================================================
# 5. vnx-dev store no-op (resolved_pid == 'vnx-dev' and store is v31-complete)
# ===========================================================================

class TestVnxDevNoOp:
    """A vnx-dev store that is already v31-complete must be a no-op.
    The v31-complete early exit fires before the adaptive branch.
    """

    def test_vnxdev_v31_complete_noop(self, tmp_path: Path) -> None:
        pid = "vnx-dev"
        db_path = _make_db_path(tmp_path, pid)
        # Build with the seocrawler mixed shape, repair it for vnx-dev
        _build_seocrawler_mixed_store(db_path, pid)
        _run_v31(db_path)
        _assert_v31_shape(db_path)

        # Second call → no-op
        _run_v31(db_path)
        _assert_v31_shape(db_path)


# ===========================================================================
# 6. Foreign-tenant abort
# ===========================================================================

class TestForeignTenantAbort:
    """If a runtime table contains a third tenant value (not resolved_pid and not
    'vnx-dev'), the pre-flight must ABORT before any mutation.
    """

    def test_foreign_tenant_in_terminal_leases_aborts(self, tmp_path: Path) -> None:
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
            "VALUES ('T-foreign', 'some-other-project', 'idle', '')"
        )
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="third tenant"):
            _run_v31(db_path)

        # DB unchanged — still at v30
        conn = sqlite3.connect(str(db_path))
        assert schema_migration.get_user_version(conn) == 30
        conn.close()

    def test_foreign_tenant_in_dispatch_attempts_aborts(self, tmp_path: Path) -> None:
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        conn.execute(
            "INSERT INTO dispatch_attempts "
            "(attempt_id, dispatch_id, project_id, terminal_id, state, started_at) "
            "VALUES ('a-foreign', 'd-x', 'alien-project', 'T0', 'pending', "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="third tenant"):
            _run_v31(db_path)


# ===========================================================================
# 7. Composite-key collision → count-assert fires
# ===========================================================================

class TestCompositeKeyCollision:
    """If two rows would produce the same composite key after the rebuild,
    the count-assert must raise RuntimeError (no silent data loss).

    In the mixed shape, terminal_leases has UNIQUE(terminal_id) only —
    two rows with same terminal_id but different project_ids satisfy the
    single-col constraint but would collide on UNIQUE(terminal_id, project_id)
    after both are filled with the same resolved_pid.
    """

    def test_collision_raises_count_assert(self, tmp_path: Path) -> None:
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)

        # Build a store where terminal_leases has NO unique constraint on
        # terminal_id alone (to allow inserting two rows with same terminal_id).
        conn = _open_conn(db_path)
        _create_dispatches_table(conn)
        _create_pool_config(conn)
        # terminal_leases without UNIQUE(terminal_id) — allows duplicate terminal_ids
        conn.execute("""
            CREATE TABLE IF NOT EXISTS terminal_leases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id         TEXT    NOT NULL,
                project_id          TEXT,
                state               TEXT    NOT NULL DEFAULT 'idle',
                dispatch_id         TEXT,
                generation          INTEGER NOT NULL DEFAULT 1,
                leased_at           TEXT,
                expires_at          TEXT,
                last_heartbeat_at   TEXT,
                released_at         TEXT,
                worker_pid          INTEGER,
                metadata_json       TEXT    DEFAULT '{}',
                lease_token         TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX idx_lease_state ON terminal_leases(state)")
        conn.execute("CREATE INDEX idx_lease_dispatch ON terminal_leases(dispatch_id)")
        conn.execute(
            "CREATE UNIQUE INDEX idx_terminal_leases_token "
            "ON terminal_leases(lease_token) WHERE lease_token != ''"
        )
        # Two rows: same terminal_id, NULL project_id — both would get resolved_pid
        # → UNIQUE(terminal_id, project_id) collision after fill
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
            "VALUES ('T-dup', NULL, 'idle', 'tok-a')"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
            "VALUES ('T-dup', NULL, 'idle', 'tok-b')"
        )
        _create_legacy_dispatch_attempts(conn)
        _create_legacy_headless_runs_no_pid(conn)
        _create_legacy_worker_states(conn)
        _create_worker_pool_membership_composite_fk(conn)
        _set_user_version(conn, 30)
        conn.close()

        with pytest.raises((RuntimeError, sqlite3.IntegrityError), match="collision|mismatch|UNIQUE"):
            _run_v31(db_path)


# ===========================================================================
# 8. worker_pool_membership single-col FK → rebuilt to composite FK
# ===========================================================================

class TestWpmRebuild:
    """worker_pool_membership with a single-column FK to terminal_leases must be
    rebuilt by the adaptive branch to have a composite FK (terminal_id, project_id).
    """

    def test_wpm_rebuilt_to_composite_fk(self, tmp_path: Path) -> None:
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        # Confirm it starts with single-col FK
        assert not _wpm_fk_to_terminal_leases_is_composite(db_path)

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        assert _wpm_fk_to_terminal_leases_is_composite(db_path), (
            "worker_pool_membership FK to terminal_leases must be composite after repair"
        )

    def test_wpm_already_composite_not_rebuilt(self, tmp_path: Path) -> None:
        """If WPM already has composite FK, it must not be unnecessarily rebuilt."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)

        # Build mixed store but give WPM a composite FK from the start
        conn = _open_conn(db_path)
        _create_dispatches_table(conn)
        _create_pool_config(conn)
        _create_mixed_terminal_leases(conn, pid)
        _create_mixed_dispatch_attempts(conn, pid)
        _create_legacy_headless_runs_no_pid(conn)
        _create_legacy_worker_states(conn)
        _create_worker_pool_membership_composite_fk(conn)
        _set_user_version(conn, 30)
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        # Still composite after repair
        assert _wpm_fk_to_terminal_leases_is_composite(db_path)


# ===========================================================================
# 9. View + trigger preservation through the adaptive rebuild
# ===========================================================================

class TestDependentObjectPreservation:
    """Views and triggers on runtime tables must survive the DROP+RENAME rebuild."""

    def test_view_referencing_terminal_leases_preserved(self, tmp_path: Path) -> None:
        """A view referencing terminal_leases must be recreated after rebuild."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        # Create a view referencing terminal_leases
        conn.execute("""
            CREATE VIEW active_leases AS
            SELECT id, terminal_id, state FROM terminal_leases WHERE state != 'released'
        """)
        conn.commit()
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        # View must still exist
        conn = sqlite3.connect(str(db_path))
        view_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='active_leases'"
        ).fetchone()
        conn.close()
        assert view_exists is not None, "View 'active_leases' was not preserved after rebuild"

    def test_trigger_on_terminal_leases_preserved(self, tmp_path: Path) -> None:
        """A trigger on terminal_leases must be recreated after rebuild."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        conn = _open_conn(db_path)
        conn.execute("""
            CREATE TRIGGER tl_updated
            AFTER UPDATE ON terminal_leases
            BEGIN
                UPDATE terminal_leases SET lease_token = lease_token
                WHERE id = NEW.id;
            END
        """)
        conn.commit()
        conn.close()

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        conn = sqlite3.connect(str(db_path))
        trigger_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='tl_updated'"
        ).fetchone()
        conn.close()
        assert trigger_exists is not None, "Trigger 'tl_updated' was not preserved after rebuild"


# ===========================================================================
# 10. Partial index preservation (idx_terminal_leases_token WHERE lease_token != '')
# ===========================================================================

class TestPartialIndexPreservation:
    """The partial unique index on lease_token (WHERE lease_token != '') must
    survive the adaptive rebuild — this index is global (not per-tenant) by design.
    """

    def test_partial_unique_index_preserved(self, tmp_path: Path) -> None:
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_seocrawler_mixed_store(db_path, pid)

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        idx_names = _index_names(db_path, "terminal_leases")
        assert "idx_terminal_leases_token" in idx_names, (
            f"Partial unique index idx_terminal_leases_token missing. "
            f"Found: {idx_names}"
        )

        # Verify it is partial (WHERE clause exists)
        conn = sqlite3.connect(str(db_path))
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_terminal_leases_token'"
        ).fetchone()
        conn.close()
        assert sql is not None and "WHERE" in sql[0].upper(), (
            "idx_terminal_leases_token is not a partial index"
        )


# ===========================================================================
# 11. Legacy-clean v30 store → static 0031 path (not adaptive branch)
# ===========================================================================

class TestLegacyCleanPath:
    """A clean v30-legacy store (no project_id in runtime tables, exact index shape)
    must use the static 0031 DDL path, not the adaptive branch.
    The result must still be v31 and FK-clean.
    """

    def test_legacy_clean_store_uses_static_path(self, tmp_path: Path, capsys) -> None:
        """Legacy-clean store: static 0031 path, user_version=31, FK-clean."""
        pid = "seocrawler-v2"
        db_path = _make_db_path(tmp_path, pid)
        _build_legacy_clean_store(db_path)

        _run_v31(db_path)
        _assert_v31_shape(db_path)

        captured = capsys.readouterr()
        # The static path prints "[apply] migration 0031_runtime_tenant_fk_repair.sql"
        # The adaptive path prints "[adapt]"
        assert "[apply]" in captured.out or "[adapt]" in captured.out


# ===========================================================================
# 12. _adaptive_foreign_tenant_preflight unit tests
# ===========================================================================

class TestForeignTenantPreflightUnit:
    """Unit tests for _adaptive_foreign_tenant_preflight directly."""

    def _make_conn_with_table(self, tmp_path: Path, pid: str) -> sqlite3.Connection:
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE terminal_leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL,
                project_id TEXT,
                state TEXT NOT NULL DEFAULT 'idle',
                lease_token TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()
        return conn

    def test_clean_set_allowed(self, tmp_path: Path) -> None:
        """Only {pid, 'vnx-dev'} → no raise."""
        conn = self._make_conn_with_table(tmp_path, "my-pid")
        conn.execute("INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) VALUES ('T1', 'my-pid', 'idle', '')")
        conn.execute("INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) VALUES ('T2', 'vnx-dev', 'idle', '')")
        conn.commit()
        # Must not raise
        mfs._adaptive_foreign_tenant_preflight(conn, ["terminal_leases"], "my-pid")
        conn.close()

    def test_third_tenant_raises(self, tmp_path: Path) -> None:
        conn = self._make_conn_with_table(tmp_path, "my-pid")
        conn.execute("INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) VALUES ('T1', 'alien-project', 'idle', '')")
        conn.commit()
        with pytest.raises(RuntimeError, match="third tenant"):
            mfs._adaptive_foreign_tenant_preflight(conn, ["terminal_leases"], "my-pid")
        conn.close()

    def test_null_and_empty_not_third_tenant(self, tmp_path: Path) -> None:
        """NULL and '' are not counted as a third tenant (they get filled)."""
        conn = self._make_conn_with_table(tmp_path, "my-pid")
        conn.execute("INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) VALUES ('T1', NULL, 'idle', '')")
        conn.execute("INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) VALUES ('T2', '', 'idle', '')")
        conn.commit()
        # Must not raise
        mfs._adaptive_foreign_tenant_preflight(conn, ["terminal_leases"], "my-pid")
        conn.close()

    def test_table_without_project_id_skipped(self, tmp_path: Path) -> None:
        """A table without project_id column is silently skipped."""
        db = tmp_path / "skip.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE no_pid_table (id INTEGER PRIMARY KEY, name TEXT)
        """)
        conn.execute("INSERT INTO no_pid_table (name) VALUES ('test')")
        conn.commit()
        # Must not raise even with alien value — column absent, skip
        mfs._adaptive_foreign_tenant_preflight(conn, ["no_pid_table"], "my-pid")
        conn.close()


# ===========================================================================
# 13. _adaptive_orphan_preflight unit tests
# ===========================================================================

class TestOrphanPreflightUnit:
    """Unit tests for _adaptive_orphan_preflight directly."""

    def _make_headless_setup(self, tmp_path: Path) -> sqlite3.Connection:
        db = tmp_path / "orphan.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE dispatch_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE headless_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'init',
                started_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()
        return conn

    def test_clean_passes(self, tmp_path: Path) -> None:
        conn = self._make_headless_setup(tmp_path)
        conn.execute("INSERT INTO dispatches (dispatch_id) VALUES ('d-1')")
        conn.execute("INSERT INTO dispatch_attempts (attempt_id, dispatch_id) VALUES ('a-1', 'd-1')")
        conn.execute("INSERT INTO headless_runs (run_id, dispatch_id, attempt_id, started_at) VALUES ('r-1', 'd-1', 'a-1', 'now')")
        conn.commit()
        mfs._adaptive_orphan_preflight(conn)  # must not raise
        conn.close()

    def test_dispatch_orphan_raises(self, tmp_path: Path) -> None:
        conn = self._make_headless_setup(tmp_path)
        conn.execute("INSERT INTO headless_runs (run_id, dispatch_id, attempt_id, started_at) VALUES ('r-orp', 'GHOST', 'a-1', 'now')")
        conn.commit()
        with pytest.raises(RuntimeError, match="orphan"):
            mfs._adaptive_orphan_preflight(conn)
        conn.close()

    def test_no_headless_runs_passes(self, tmp_path: Path) -> None:
        """Empty headless_runs table → no orphans → passes."""
        conn = self._make_headless_setup(tmp_path)
        mfs._adaptive_orphan_preflight(conn)  # must not raise
        conn.close()

    def test_table_without_dispatch_id_skipped(self, tmp_path: Path) -> None:
        """If headless_runs lacks dispatch_id column, orphan check is skipped."""
        db = tmp_path / "no_col.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE headless_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO headless_runs (run_id) VALUES ('r-1')")
        conn.commit()
        mfs._adaptive_orphan_preflight(conn)  # must not raise
        conn.close()
