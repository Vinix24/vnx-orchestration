"""Tests for the terminal_leases.worker_pid self-heal.

Background: rc6's runtime_coordination code WRITES and READS
``terminal_leases.worker_pid`` (pool_state_repo.store_worker_pid / list_members)
but no schema file or migration ever defined the column — the 2nd schema-code
drift after worker_states.project_id (OI-095). Every dispatch logged
"PID persistence failed: no such column: worker_pid".

Coverage:
  A. Fresh canonical init → terminal_leases already has worker_pid
  B. Legacy DB without worker_pid → init self-heals it, independent of
     ``user_version`` (proves version-independence)
  C. Re-run is a clean no-op (idempotent)
  D. terminal_leases absent → ``skipped_missing`` (no crash)
  E. Real ``PoolStateRepository.store_worker_pid`` UPDATE fails before the heal
     and succeeds after (regression: runs the actual production code path)
  F. ``run_runtime_coordination_migration`` reports the worker_pid status
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from project_id_migration import (  # noqa: E402
    ensure_worker_pid_column,
    run_runtime_coordination_migration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _init_runtime_db(state_dir: Path) -> Path:
    """Initialise a fresh runtime_coordination.db via the canonical schema."""
    from runtime_coordination import init_schema  # local import (sys.path set above)

    init_schema(state_dir, _SCHEMAS_DIR / "runtime_coordination.sql")
    return state_dir / "runtime_coordination.db"


def _make_legacy_leases_db(db_path: Path, *, user_version: int = 0) -> None:
    """Build a runtime_coordination.db whose terminal_leases predates worker_pid."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE terminal_leases (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id       TEXT NOT NULL,
                project_id        TEXT NOT NULL DEFAULT 'vnx-dev',
                state             TEXT NOT NULL DEFAULT 'idle',
                dispatch_id       TEXT,
                generation        INTEGER NOT NULL DEFAULT 1,
                leased_at         TEXT,
                expires_at        TEXT,
                last_heartbeat_at TEXT,
                released_at       TEXT,
                metadata_json     TEXT DEFAULT '{}',
                UNIQUE(terminal_id, project_id)
            )
            """
        )
        conn.execute(
            "CREATE TABLE runtime_schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO terminal_leases (terminal_id, state, generation) VALUES (?, 'idle', 1)",
            [("T1",), ("T2",), ("T3",)],
        )
        conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A. Fresh canonical init already carries worker_pid
# ---------------------------------------------------------------------------

def test_fresh_canonical_db_has_worker_pid(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)

    conn = sqlite3.connect(str(db_path))
    try:
        assert "worker_pid" in _columns(conn, "terminal_leases")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B. Legacy DB self-heals, independent of user_version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("user_version", [0, 12, 99])
def test_legacy_db_heals_worker_pid_any_version(tmp_path: Path, user_version: int) -> None:
    db_path = tmp_path / "runtime_coordination.db"
    _make_legacy_leases_db(db_path, user_version=user_version)

    conn = sqlite3.connect(str(db_path))
    try:
        assert "worker_pid" not in _columns(conn, "terminal_leases")
        assert ensure_worker_pid_column(conn) == "added"
        conn.commit()
        assert "worker_pid" in _columns(conn, "terminal_leases")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C. Idempotent — re-run is a clean no-op
# ---------------------------------------------------------------------------

def test_worker_pid_heal_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_coordination.db"
    _make_legacy_leases_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert ensure_worker_pid_column(conn) == "added"
        conn.commit()
        assert ensure_worker_pid_column(conn) == "already_present"
        assert ensure_worker_pid_column(conn) == "already_present"
        # Exactly one worker_pid column — no duplication.
        assert _columns(conn, "terminal_leases").count("worker_pid") == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# D. terminal_leases absent → skipped_missing
# ---------------------------------------------------------------------------

def test_worker_pid_skipped_when_table_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    try:
        assert ensure_worker_pid_column(conn) == "skipped_missing"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# E. Real production path: PoolStateRepository.store_worker_pid
# ---------------------------------------------------------------------------

def test_store_worker_pid_fails_before_heal_succeeds_after(tmp_path: Path) -> None:
    """Runs the actual pool_state_repo code, not a reimplementation."""
    from pool_state_repo import PoolStateRepository

    db_path = tmp_path / "runtime_coordination.db"
    _make_legacy_leases_db(db_path)
    repo = PoolStateRepository(db_path, project_id="vnx-dev")

    # Before the heal: the UPDATE references a column that does not exist.
    with pytest.raises(sqlite3.OperationalError, match="worker_pid"):
        repo.store_worker_pid("T1", 4242)

    # Heal, then the same real code path must succeed.
    conn = sqlite3.connect(str(db_path))
    try:
        assert ensure_worker_pid_column(conn) == "added"
        conn.commit()
    finally:
        conn.close()

    repo.store_worker_pid("T1", 4242)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT worker_pid FROM terminal_leases WHERE terminal_id = ?", ("T1",)
        ).fetchone()
        assert row[0] == 4242
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# F. run_runtime_coordination_migration reports worker_pid status
# ---------------------------------------------------------------------------

def test_runtime_migration_reports_worker_pid_added(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_coordination.db"
    _make_legacy_leases_db(db_path)

    result = run_runtime_coordination_migration(db_path)
    assert result["status"] == "ok"
    assert result["worker_pid_status"] == "added"

    # Re-run is idempotent and now reports the column already present.
    result2 = run_runtime_coordination_migration(db_path)
    assert result2["worker_pid_status"] == "already_present"


def test_runtime_migration_fresh_db_worker_pid_present(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = _init_runtime_db(state_dir)

    result = run_runtime_coordination_migration(db_path)
    # Fresh canonical schema already declares worker_pid → no ALTER needed.
    assert result["worker_pid_status"] == "already_present"
