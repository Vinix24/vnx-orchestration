"""Tests for scripts/lint_migration_rebuild.py — rebuild-preservation CI harness.

Covers:
- Fixture A: pure rename, no columns dropped → pass
- Fixture B: column dropped (status), no allowlist → fail
- Fixture C: column dropped WITH allowlist → pass
- Fixture D: UNIQUE dropped, no allowlist → fail
- Fixture E: FK dropped, no allowlist → fail
- Real-world: main() against actual schemas/migrations/ → 0
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add project root to sys.path so scripts.lint_migration_rebuild is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lint_migration_rebuild import main, _check_migration, _sorted_migrations


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_files(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write name→content mapping into tmp_path and return it."""
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    return tmp_path


# ---------------------------------------------------------------------------
# Fixture A — pure rename, no schema change → pass
# ---------------------------------------------------------------------------

def test_fixture_a_pass(tmp_path: Path) -> None:
    _write_files(tmp_path, {
        "0001_base.sql": (
            "CREATE TABLE orders ("
            "  id INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  status TEXT NOT NULL"
            ");"
        ),
        "0002_rename.sql": (
            "ALTER TABLE orders RENAME TO orders_v2;"
        ),
    })
    result = main(migrations_directory=tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# Fixture B — column 'status' dropped, no allowlist → fail
# ---------------------------------------------------------------------------

def test_fixture_b_fail_column_dropped(tmp_path: Path) -> None:
    _write_files(tmp_path, {
        "0001_base.sql": (
            "CREATE TABLE orders ("
            "  id INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  status TEXT NOT NULL"
            ");"
        ),
        "0002_rename.sql": (
            "ALTER TABLE orders RENAME TO orders_v2;"
            " DROP TABLE orders_v2;"
            " CREATE TABLE orders_v2 (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        ),
    })
    all_migs = _sorted_migrations(tmp_path)
    rename_file = tmp_path / "0002_rename.sql"
    result = _check_migration(rename_file, all_migs)
    assert result["status"] == "fail"
    types = [d["type"] for d in result["drifts"]]
    assert "column_dropped" in types
    items = [d["item"] for d in result["drifts"]]
    assert "status" in items


# ---------------------------------------------------------------------------
# Fixture C — column dropped WITH allowlist → pass
# ---------------------------------------------------------------------------

def test_fixture_c_pass_with_allowlist(tmp_path: Path) -> None:
    _write_files(tmp_path, {
        "0001_base.sql": (
            "CREATE TABLE orders ("
            "  id INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  status TEXT NOT NULL"
            ");"
        ),
        "0002_rename.sql": (
            "-- preservation-allowlist: orders.status\n"
            "-- preservation-rationale: deprecated\n"
            "ALTER TABLE orders RENAME TO orders_v2;"
            " DROP TABLE orders_v2;"
            " CREATE TABLE orders_v2 (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        ),
    })
    result = main(migrations_directory=tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# Fixture D — UNIQUE dropped, no allowlist → fail
# ---------------------------------------------------------------------------

def test_fixture_d_fail_unique_dropped(tmp_path: Path) -> None:
    _write_files(tmp_path, {
        "0001_base.sql": (
            "CREATE TABLE orders ("
            "  id INTEGER PRIMARY KEY,"
            "  project_id TEXT NOT NULL,"
            "  ref TEXT NOT NULL,"
            "  UNIQUE(project_id, ref)"
            ");"
        ),
        "0002_rename.sql": (
            "ALTER TABLE orders RENAME TO orders_v2;"
            " DROP TABLE orders_v2;"
            " CREATE TABLE orders_v2 ("
            "  id INTEGER PRIMARY KEY,"
            "  project_id TEXT NOT NULL,"
            "  ref TEXT NOT NULL"
            ");"
        ),
    })
    all_migs = _sorted_migrations(tmp_path)
    rename_file = tmp_path / "0002_rename.sql"
    result = _check_migration(rename_file, all_migs)
    assert result["status"] == "fail"
    types = [d["type"] for d in result["drifts"]]
    assert "unique_dropped" in types


# ---------------------------------------------------------------------------
# Fixture E — FK dropped, no allowlist → fail
# ---------------------------------------------------------------------------

def test_fixture_e_fail_fk_dropped(tmp_path: Path) -> None:
    _write_files(tmp_path, {
        "0001_base.sql": (
            "CREATE TABLE projects (id INTEGER PRIMARY KEY);"
            " CREATE TABLE orders ("
            "  id INTEGER PRIMARY KEY,"
            "  project_id INTEGER NOT NULL REFERENCES projects(id)"
            ");"
        ),
        "0002_rename.sql": (
            "ALTER TABLE orders RENAME TO orders_v2;"
            " DROP TABLE orders_v2;"
            " CREATE TABLE orders_v2 ("
            "  id INTEGER PRIMARY KEY,"
            "  project_id INTEGER NOT NULL"
            ");"
        ),
    })
    all_migs = _sorted_migrations(tmp_path)
    rename_file = tmp_path / "0002_rename.sql"
    result = _check_migration(rename_file, all_migs)
    assert result["status"] == "fail"
    types = [d["type"] for d in result["drifts"]]
    assert "fk_dropped" in types


# ---------------------------------------------------------------------------
# Real-world: actual schemas/migrations/ must pass
# ---------------------------------------------------------------------------

def test_real_migrations_pass() -> None:
    """Gate returns 0 on real migrations.

    Legacy migrations (0017, 0017_down, 0022) depend on base schemas that are not
    present in schemas/migrations/ — they return 'skip' with a logged warning rather
    than 'error', so they do not fail the gate.  The gate only fails on 'fail' status
    (undeclared drift) or 'error' status (unexpected exception during the check itself).
    """
    rc = main()
    assert rc == 0
