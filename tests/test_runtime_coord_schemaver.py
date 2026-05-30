"""tests/test_runtime_coord_schemaver.py

Regression tests for the runtime_schema_version table bootstrap gap:
  - run_runtime_coordination_migration must CREATE the table when absent,
    then stamp it — not assume init_schema already created it.
  - After migrate, _check_schema_versions (vnx doctor) must return PASS
    with no OperationalError on the SELECT.
  - Idempotency: second call is a no-op.

Dispatch-ID: 20260530-095126-schemaver-table
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

from project_id_migration import (
    RUNTIME_SCHEMA_VERSION,
    run_runtime_coordination_migration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bare_coordination_db(path: Path) -> None:
    """Create a runtime_coordination.db WITHOUT runtime_schema_version table.

    Simulates the fresh-pip-install scenario where init_schema resolves the
    wrong schema directory and never applies the base SQL that creates the
    table, but PRAGMA user_version is set (e.g. by auto_apply runners).
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "CREATE TABLE dispatches ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "dispatch_id TEXT NOT NULL UNIQUE, "
            "state TEXT NOT NULL DEFAULT 'queued')"
        )
        conn.execute(
            "CREATE TABLE terminal_leases ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "terminal_id TEXT NOT NULL UNIQUE, "
            "state TEXT NOT NULL DEFAULT 'idle', "
            "generation INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute("PRAGMA user_version = 26")
        conn.commit()
    finally:
        conn.close()


def _table_exists(db_path: Path, table: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _get_schema_versions(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT version FROM runtime_schema_version ORDER BY version"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _get_pragma_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test A: creates table when absent, stamps version
# ---------------------------------------------------------------------------

def test_creates_table_when_absent(tmp_path):
    """run_runtime_coordination_migration must create runtime_schema_version
    and stamp RUNTIME_SCHEMA_VERSION when the table did not exist."""
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir()
    _make_bare_coordination_db(db_path)

    assert not _table_exists(db_path, "runtime_schema_version"), (
        "pre-condition: table must be absent before migration"
    )

    result = run_runtime_coordination_migration(db_path)

    assert result["status"] == "ok"
    assert _table_exists(db_path, "runtime_schema_version"), (
        "runtime_schema_version table must exist after migration"
    )
    versions = _get_schema_versions(db_path)
    assert RUNTIME_SCHEMA_VERSION in versions, (
        f"expected version {RUNTIME_SCHEMA_VERSION} in runtime_schema_version, got {versions}"
    )


# ---------------------------------------------------------------------------
# Test B: PRAGMA user_version preserved (migration does not reset it)
# ---------------------------------------------------------------------------

def test_pragma_user_version_preserved(tmp_path):
    """Migration must not clobber a pre-existing PRAGMA user_version."""
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir()
    _make_bare_coordination_db(db_path)

    pre_version = _get_pragma_version(db_path)
    run_runtime_coordination_migration(db_path)
    post_version = _get_pragma_version(db_path)

    assert post_version == pre_version, (
        f"PRAGMA user_version must not change: was {pre_version}, now {post_version}"
    )


# ---------------------------------------------------------------------------
# Test C: idempotent — second call is a no-op
# ---------------------------------------------------------------------------

def test_idempotent(tmp_path):
    """Two successive calls must leave the DB in the same state as one call."""
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir()
    _make_bare_coordination_db(db_path)

    first = run_runtime_coordination_migration(db_path)
    second = run_runtime_coordination_migration(db_path)

    assert first["status"] == "ok"
    assert second["status"] == "ok"

    versions = _get_schema_versions(db_path)
    count = versions.count(RUNTIME_SCHEMA_VERSION)
    assert count == 1, (
        f"version {RUNTIME_SCHEMA_VERSION} must appear exactly once, found {count} times"
    )


# ---------------------------------------------------------------------------
# Test D: doctor _check_schema_versions returns PASS — no OperationalError
# ---------------------------------------------------------------------------

def test_doctor_schema_check_passes_after_migrate(tmp_path):
    """After run_runtime_coordination_migration, _check_schema_versions must
    return PASS without logging 'runtime_schema_version query failed'."""
    import sys
    from pathlib import Path as P

    _VNX_CLI = P(__file__).resolve().parent.parent
    if str(_VNX_CLI) not in sys.path:
        sys.path.insert(0, str(_VNX_CLI))

    from vnx_cli.commands.doctor import PASS, _check_schema_versions

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "runtime_coordination.db"
    _make_bare_coordination_db(db_path)
    run_runtime_coordination_migration(db_path)

    checks = _check_schema_versions(tmp_path)
    coordination_check = next(
        (c for c in checks if "runtime_coordination.db" in c.name), None
    )
    assert coordination_check is not None, "Expected check for runtime_coordination.db"
    assert coordination_check.status == PASS, (
        f"Expected PASS, got {coordination_check.status}: {coordination_check.detail}"
    )


# ---------------------------------------------------------------------------
# Test E: table columns match what doctor queries
# ---------------------------------------------------------------------------

def test_table_columns_match_doctor_query(tmp_path):
    """Columns created by run_runtime_coordination_migration must satisfy
    the ORDER BY applied_at used by _check_schema_versions."""
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir()
    _make_bare_coordination_db(db_path)
    run_runtime_coordination_migration(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT version FROM runtime_schema_version "
            "ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "SELECT ... ORDER BY applied_at must return a row"
        assert row[0] == RUNTIME_SCHEMA_VERSION
    finally:
        conn.close()
