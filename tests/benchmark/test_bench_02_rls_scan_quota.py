"""tests/benchmark/test_bench_02_rls_scan_quota.py

Bench-02: tenant-scoped schema migration for scan_quota (ADR-007).

Verifies all 5 acceptance points from the dispatch:
  1. 3 seed rows remain accessible with project_id = 'default'
  2. Cross-tenant insert (scan_id='scan_a', project_id='tenant_x') succeeds
  3. Duplicate (scan_id='scan_a', project_id='default') raises IntegrityError
  4. UNIQUE constraint is composite (project_id, scan_id) — verifiable in sqlite_master
  5. Index on (project_id) exists

Plus:
  6. migrate.sql is idempotent — second sqlite3 run does not error
  7. sqlite3 CLI applies migrate.sql cleanly on a fresh seed DB
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_DIR = (
    _REPO_ROOT
    / "scripts"
    / "benchmark"
    / "field-tests"
    / "tasks"
    / "t1_trivial"
    / "02_rls_policy"
    / "seed"
)
_MIGRATE_SQL = _SEED_DIR / "migrate.sql"
_INIT_SQL = _SEED_DIR / "init_seed_db.sql"


def _apply_via_sqlite3_cli(db_path: Path, sql_path: Path) -> None:
    result = subprocess.run(
        ["sqlite3", str(db_path)],
        input=sql_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"sqlite3 failed (rc={result.returncode}):\n{result.stderr[-500:]}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "scan_quota.db"
    _apply_via_sqlite3_cli(db, _INIT_SQL)
    _apply_via_sqlite3_cli(db, _MIGRATE_SQL)
    return db


# ---------------------------------------------------------------------------
# Core verification (dispatch points 1-5)
# ---------------------------------------------------------------------------


def test_seed_rows_preserved_with_default_project_id(migrated_db: Path) -> None:
    conn = sqlite3.connect(str(migrated_db))
    try:
        rows = conn.execute(
            "SELECT id, scan_id, project_id FROM scan_quota ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 3, f"Expected 3 seed rows, got {len(rows)}"
    for row_id, scan_id, project_id in rows:
        assert project_id == "default", (
            f"Row {row_id} (scan_id={scan_id!r}) has project_id={project_id!r}, expected 'default'"
        )


def test_cross_tenant_insert_succeeds(migrated_db: Path) -> None:
    conn = sqlite3.connect(str(migrated_db))
    try:
        conn.execute(
            "INSERT INTO scan_quota(scan_id, project_id) VALUES ('scan_a', 'tenant_x')"
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM scan_quota WHERE scan_id='scan_a'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 2, "Expected 2 rows with scan_id='scan_a' (default + tenant_x)"


def test_duplicate_within_same_tenant_raises(migrated_db: Path) -> None:
    conn = sqlite3.connect(str(migrated_db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scan_quota(scan_id, project_id) VALUES ('scan_a', 'default')"
            )
    finally:
        conn.close()


def test_unique_constraint_is_composite_project_scan(migrated_db: Path) -> None:
    conn = sqlite3.connect(str(migrated_db))
    try:
        index_defs = [
            row[0] or ""
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='scan_quota'"
            )
        ]
    finally:
        conn.close()

    composite_unique = any(
        "project_id" in sql.lower() and "scan_id" in sql.lower()
        for sql in index_defs
    )
    assert composite_unique, (
        f"No composite UNIQUE index over (project_id, scan_id) found. "
        f"Indexes: {[s[:80] for s in index_defs]}"
    )


def test_project_id_index_exists(migrated_db: Path) -> None:
    conn = sqlite3.connect(str(migrated_db))
    try:
        index_defs = [
            row[0] or ""
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='scan_quota'"
            )
        ]
    finally:
        conn.close()

    project_id_only = any(
        "project_id" in sql.lower()
        and ("scan_id" not in sql.lower() or "create index" in sql.lower())
        for sql in index_defs
    )
    assert project_id_only, (
        f"No standalone index on (project_id) found. "
        f"Indexes: {[s[:80] for s in index_defs]}"
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_migrate_sql_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "scan_quota.db"
    _apply_via_sqlite3_cli(db, _INIT_SQL)
    _apply_via_sqlite3_cli(db, _MIGRATE_SQL)
    # Second application must not raise and must not corrupt data
    _apply_via_sqlite3_cli(db, _MIGRATE_SQL)

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM scan_quota").fetchone()[0]
        project_ids = {r[0] for r in conn.execute("SELECT DISTINCT project_id FROM scan_quota")}
    finally:
        conn.close()

    assert rows == 3, f"Row count changed after idempotent re-run: {rows}"
    assert project_ids == {"default"}, f"Unexpected project_id values after re-run: {project_ids}"


# ---------------------------------------------------------------------------
# SQLite CLI smoke test
# ---------------------------------------------------------------------------


def test_sqlite3_cli_applies_migrate_sql(tmp_path: Path) -> None:
    db = tmp_path / "scan_quota_cli.db"
    _apply_via_sqlite3_cli(db, _INIT_SQL)
    _apply_via_sqlite3_cli(db, _MIGRATE_SQL)

    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_quota)")}
    finally:
        conn.close()

    assert "project_id" in cols, "project_id column missing after CLI apply"
