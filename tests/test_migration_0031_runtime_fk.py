"""Migration 0031: ADR-007 runtime tenant/FK repair.

Builds the malformed central v30 shape in a temp DB, applies the numbered walk,
and verifies lossless/idempotent convergence at v31. The live central DB is never
opened by this test.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _path in (_SCRIPTS, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import migrate_future_system as mfs  # noqa: E402
import schema_manifest  # noqa: E402
import schema_migration  # noqa: E402


_LEGACY_RUNTIME_SQL = """
CREATE TABLE dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    state TEXT NOT NULL DEFAULT 'queued',
    terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_after TEXT, metadata_json TEXT DEFAULT '{}',
    UNIQUE(dispatch_id, project_id)
);

CREATE TABLE dispatch_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id),
    attempt_number INTEGER NOT NULL DEFAULT 1,
    terminal_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at TEXT, failure_reason TEXT, metadata_json TEXT DEFAULT '{}'
);
CREATE INDEX idx_attempt_dispatch ON dispatch_attempts(dispatch_id, attempt_number);
CREATE INDEX idx_attempt_state ON dispatch_attempts(state, started_at DESC);
CREATE INDEX idx_attempt_terminal ON dispatch_attempts(terminal_id, started_at DESC);

CREATE TABLE terminal_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'idle',
    dispatch_id TEXT REFERENCES dispatches(dispatch_id),
    generation INTEGER NOT NULL DEFAULT 1,
    leased_at TEXT, expires_at TEXT, last_heartbeat_at TEXT, released_at TEXT,
    worker_pid INTEGER, metadata_json TEXT DEFAULT '{}',
    lease_token TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_lease_state ON terminal_leases(state);
CREATE INDEX idx_lease_dispatch ON terminal_leases(dispatch_id);
CREATE UNIQUE INDEX idx_terminal_leases_token
    ON terminal_leases(lease_token) WHERE lease_token != '';

CREATE TABLE headless_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id),
    attempt_id TEXT NOT NULL REFERENCES dispatch_attempts(attempt_id),
    target_id TEXT NOT NULL, target_type TEXT NOT NULL, task_class TEXT NOT NULL,
    terminal_id TEXT, pid INTEGER, pgid INTEGER,
    state TEXT NOT NULL DEFAULT 'init',
    failure_class TEXT, exit_code INTEGER,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    subprocess_started_at TEXT, heartbeat_at TEXT, last_output_at TEXT,
    completed_at TEXT, duration_seconds REAL, log_artifact_path TEXT,
    output_artifact_path TEXT, receipt_id TEXT, metadata_json TEXT DEFAULT '{}'
);
CREATE INDEX idx_headless_run_state ON headless_runs(state, started_at DESC);
CREATE INDEX idx_headless_run_dispatch ON headless_runs(dispatch_id);
CREATE INDEX idx_headless_run_target ON headless_runs(target_id, state);
CREATE INDEX idx_headless_run_heartbeat
    ON headless_runs(state, heartbeat_at) WHERE state = 'running';

CREATE TABLE worker_states (
    terminal_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'initializing',
    last_output_at TEXT,
    state_entered_at TEXT NOT NULL,
    stall_count INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT, metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (terminal_id),
    FOREIGN KEY (terminal_id) REFERENCES terminal_leases(terminal_id),
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id)
);
CREATE INDEX idx_worker_state ON worker_states(state);
CREATE INDEX idx_worker_dispatch ON worker_states(dispatch_id);

CREATE TABLE pool_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    pool_id TEXT NOT NULL DEFAULT 'default',
    UNIQUE(project_id, pool_id)
);
CREATE TABLE worker_pool_membership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    pool_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL CHECK (provider IN ('claude','codex','gemini','litellm')),
    role TEXT NOT NULL,
    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    released_at TEXT, release_reason TEXT,
    spawn_generation INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT DEFAULT '{}',
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id),
    FOREIGN KEY (project_id, pool_id)
        REFERENCES pool_config(project_id, pool_id)
);
CREATE UNIQUE INDEX idx_pool_membership_active
    ON worker_pool_membership(terminal_id, project_id) WHERE released_at IS NULL;
CREATE INDEX idx_pool_membership_pool
    ON worker_pool_membership(project_id, pool_id);

CREATE TABLE coordination_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT, event_type TEXT NOT NULL, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, occurred_at TEXT NOT NULL
);

INSERT INTO terminal_leases(terminal_id, state, generation)
VALUES ('T1','idle',1), ('T2','idle',1), ('T3','idle',1);
INSERT INTO pool_config(project_id, pool_id) VALUES ('vnx-dev', 'default');
"""

_TRACK_CHAIN = (
    (22, "apply_migration"),
    (24, "apply_migration_v24"),
    (27, "apply_migration_v27"),
    (28, "apply_migration_v28"),
    (29, "apply_migration_v29"),
    (30, "apply_migration_v30"),
)


def _build_v30_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    project_root = tmp_path / "project"
    state_dir = project_root / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(_LEGACY_RUNTIME_SQL)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    for _version, function_name in _TRACK_CHAIN:
        getattr(mfs, function_name)(conn, project_root)
        conn.commit()
    assert schema_migration.get_user_version(conn) == 30
    return project_root, conn


def _unique_keys(conn: sqlite3.Connection, table: str) -> set[frozenset[str]]:
    keys: set[frozenset[str]] = set()
    for index in conn.execute(f"PRAGMA index_list('{table}')"):
        if index[2]:
            cols = frozenset(
                row[2] for row in conn.execute(f"PRAGMA index_info('{index[1]}')")
            )
            keys.add(cols)
    return keys


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple]:
    grouped: dict[int, list[tuple]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list('{table}')"):
        grouped.setdefault(row[0], []).append(row)
    return {
        (
            tuple(row[3] for row in sorted(rows, key=lambda item: item[1])),
            rows[0][2],
            tuple(row[4] for row in sorted(rows, key=lambda item: item[1])),
        )
        for rows in grouped.values()
    }


def _runtime_snapshot(conn: sqlite3.Connection) -> tuple:
    targets = (*mfs._RUNTIME_V31_TABLES, "dispatches", "tracks", "coordination_events")
    placeholders = ",".join("?" for _ in targets)
    schema = tuple(conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        f"WHERE tbl_name IN ({placeholders}) ORDER BY type, name",
        targets,
    ))
    rows = tuple(
        (table, tuple(conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')))
        for table in mfs._RUNTIME_V31_TABLES
    )
    sequences = tuple(conn.execute(
        "SELECT name, seq FROM sqlite_sequence "
        "WHERE name IN ('terminal_leases', 'dispatch_attempts', 'headless_runs', "
        "'worker_states', 'worker_pool_membership') ORDER BY name"
    ))
    return schema, rows, sequences


def test_migration_0031_repairs_runtime_tenant_fks_losslessly_and_idempotently(
    tmp_path: Path,
) -> None:
    project_root, conn = _build_v30_db(tmp_path)
    try:
        untouched_before = {
            table: conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            for table in ("dispatches", "tracks", "coordination_events")
        }
        lease_rows_before = tuple(conn.execute(
            "SELECT id, terminal_id, state, generation, worker_pid, metadata_json, lease_token "
            "FROM terminal_leases ORDER BY id"
        ))
        assert len(lease_rows_before) == 3
        with pytest.raises(sqlite3.OperationalError, match="foreign key mismatch"):
            conn.execute("PRAGMA foreign_key_check").fetchall()

        mfs._run_numbered_walk(conn, project_root)

        assert schema_migration.get_user_version(conn) == 31
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert schema_manifest.validate_db_at_version(conn, 31) == []
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        mfs._assert_manifest_converged(conn)

        leases_after = tuple(conn.execute(
            "SELECT id, terminal_id, state, generation, worker_pid, metadata_json, "
            "lease_token, project_id FROM terminal_leases ORDER BY id"
        ))
        assert tuple(row[:-1] for row in leases_after) == lease_rows_before
        assert {row[-1] for row in leases_after} == {"vnx-dev"}
        assert frozenset({"terminal_id", "project_id"}) in _unique_keys(
            conn, "terminal_leases")
        assert frozenset({"attempt_id", "project_id"}) in _unique_keys(
            conn, "dispatch_attempts")
        assert frozenset({"run_id", "project_id"}) in _unique_keys(conn, "headless_runs")
        assert mfs._table_pk(conn, "worker_states") == ("terminal_id", "project_id")

        assert _foreign_keys(conn, "terminal_leases") == {
            (("dispatch_id", "project_id"), "dispatches", ("dispatch_id", "project_id"))
        }
        assert _foreign_keys(conn, "dispatch_attempts") == {
            (("dispatch_id", "project_id"), "dispatches", ("dispatch_id", "project_id"))
        }
        assert _foreign_keys(conn, "headless_runs") == {
            (("dispatch_id", "project_id"), "dispatches", ("dispatch_id", "project_id")),
            (("attempt_id", "project_id"), "dispatch_attempts", ("attempt_id", "project_id")),
        }
        assert _foreign_keys(conn, "worker_states") == {
            (("terminal_id", "project_id"), "terminal_leases", ("terminal_id", "project_id")),
            (("dispatch_id", "project_id"), "dispatches", ("dispatch_id", "project_id")),
        }
        assert all(
            "dispatches_pre_v22" not in row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
        )

        untouched_after = {
            table: conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            for table in ("dispatches", "tracks", "coordination_events")
        }
        assert untouched_after == untouched_before

        snapshot = _runtime_snapshot(conn)
        conn.execute("PRAGMA user_version=30")
        conn.commit()
        mfs._run_numbered_walk(conn, project_root)
        assert schema_migration.get_user_version(conn) == 31
        assert _runtime_snapshot(conn) == snapshot
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        conn.close()


def test_migration_0031_preserves_runtime_autoincrement_high_water_marks(
    tmp_path: Path,
) -> None:
    project_root, conn = _build_v30_db(tmp_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO dispatches(dispatch_id, project_id, state) "
            "VALUES ('d-sequence', 'vnx-dev', 'queued')"
        )
        conn.executemany(
            "INSERT INTO dispatch_attempts(attempt_id, dispatch_id, terminal_id) "
            "VALUES (?, 'd-sequence', 'T1')",
            (("a-sequence-1",), ("a-sequence-2",), ("a-sequence-3",)),
        )
        conn.executemany(
            "INSERT INTO headless_runs("
            "run_id, dispatch_id, attempt_id, target_id, target_type, task_class"
            ") VALUES (?, 'd-sequence', ?, 'target', 'test', 'test')",
            (
                ("r-sequence-1", "a-sequence-1"),
                ("r-sequence-2", "a-sequence-2"),
                ("r-sequence-3", "a-sequence-3"),
            ),
        )
        conn.execute("DELETE FROM terminal_leases WHERE id=3")
        conn.execute("DELETE FROM headless_runs WHERE id=3")
        conn.execute("DELETE FROM dispatch_attempts WHERE id=3")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

        deleted_high_water = dict(conn.execute(
            "SELECT name, seq FROM sqlite_sequence "
            "WHERE name IN ('terminal_leases', 'dispatch_attempts', 'headless_runs')"
        ))
        assert deleted_high_water == {
            "terminal_leases": 3,
            "dispatch_attempts": 3,
            "headless_runs": 3,
        }
        assert conn.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name='worker_states'"
        ).fetchone() is None

        mfs._run_numbered_walk(conn, project_root)

        new_lease_id = conn.execute(
            "INSERT INTO terminal_leases(terminal_id, project_id) VALUES ('T4', 'vnx-dev')"
        ).lastrowid
        new_attempt_id = conn.execute(
            "INSERT INTO dispatch_attempts("
            "attempt_id, dispatch_id, project_id, terminal_id"
            ") VALUES ('a-sequence-4', 'd-sequence', 'vnx-dev', 'T1')"
        ).lastrowid
        new_run_id = conn.execute(
            "INSERT INTO headless_runs("
            "run_id, dispatch_id, project_id, attempt_id, target_id, target_type, task_class"
            ") VALUES ("
            "'r-sequence-4', 'd-sequence', 'vnx-dev', 'a-sequence-1', "
            "'target', 'test', 'test'"
            ")"
        ).lastrowid

        assert new_lease_id > deleted_high_water["terminal_leases"]
        assert new_attempt_id > deleted_high_water["dispatch_attempts"]
        assert new_run_id > deleted_high_water["headless_runs"]
        assert conn.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name='worker_states'"
        ).fetchone() is None
    finally:
        conn.close()


def test_migration_0031_refuses_same_name_different_index_definition(
    tmp_path: Path,
) -> None:
    project_root, conn = _build_v30_db(tmp_path)
    try:
        conn.execute("DROP INDEX idx_lease_state")
        conn.execute(
            "CREATE UNIQUE INDEX idx_lease_state "
            "ON terminal_leases(terminal_id, generation)"
        )
        conn.commit()

        with pytest.raises(RuntimeError, match="index 'idx_lease_state'.*definition differs"):
            mfs.apply_migration_v31(conn, project_root)

        assert schema_migration.get_user_version(conn) == 30
        assert "CREATE UNIQUE INDEX" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_lease_state'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_v31_manifest_and_convergence_reject_nonunique_pool_config_parent(
    tmp_path: Path,
) -> None:
    project_root, conn = _build_v30_db(tmp_path)
    try:
        mfs._run_numbered_walk(conn, project_root)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE pool_config")
        conn.execute(
            "CREATE TABLE pool_config ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, "
            "pool_id TEXT NOT NULL DEFAULT 'default')"
        )
        conn.execute(
            "INSERT INTO pool_config(project_id, pool_id) VALUES ('vnx-dev', 'default')"
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

        violations = schema_manifest.validate_db_at_version(conn, 31)
        assert any("pool_config: missing UNIQUE('project_id', 'pool_id')" in v
                   for v in violations), violations
        with pytest.raises(
            schema_manifest.SchemaReconciliationError,
            match="foreign_key_check failed structurally",
        ):
            mfs._assert_manifest_converged(conn)
    finally:
        conn.close()


def test_v31_convergence_foreign_key_guard_is_independent_of_manifest(
    tmp_path: Path,
) -> None:
    project_root, conn = _build_v30_db(tmp_path)
    try:
        mfs._run_numbered_walk(conn, project_root)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO worker_pool_membership("
            "terminal_id, project_id, pool_id, provider, role"
            ") VALUES ('T1', 'vnx-dev', 'missing-pool', 'codex', 'test')"
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

        assert schema_manifest.validate_db_at_version(conn, 31) == []
        with pytest.raises(
            schema_manifest.SchemaReconciliationError,
            match="foreign_key_check violations",
        ):
            mfs._assert_manifest_converged(conn)
    finally:
        conn.close()
