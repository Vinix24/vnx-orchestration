"""Migration 0039: deliverable_dependencies (ADR-096 §1-2, B1a). Temp stores and a read-only live copy only."""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import schema_manifest
import schema_migration
from coordination_db import db_path_from_state_dir, init_schema
from migrations import apply_0039
from migrations import auto_apply as auto_apply_mod

LIVE_DB = Path.home() / ".vnx-data" / "vnx-dev" / "state" / "runtime_coordination.db"
MIGRATIONS = _ROOT / "schemas" / "migrations"
SQL = MIGRATIONS / "0039_deliverable_dependencies.sql"


def _counts(conn: sqlite3.Connection) -> dict:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _store_at_38(tmp_path: Path) -> tuple[Path, str]:
    """A read-only backup of the live store, or (CI: no live file) a store built from the migrations."""
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    db = db_path_from_state_dir(state_dir)
    if LIVE_DB.is_file():
        src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(str(db))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        return db, "live-copy"
    upto_38 = tmp_path / "upto-38"
    upto_38.mkdir()
    for number, sql_path in auto_apply_mod._discover_migrations(MIGRATIONS):
        if number <= 38:
            shutil.copyfile(sql_path, upto_38 / sql_path.name)
    init_schema(state_dir)
    auto_apply_mod.auto_apply(db, migrations_dir=upto_38)
    return db, "built-from-migrations"


def test_migration_0039_on_a_copy_of_the_live_schema(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(schema_migration, "_PREFLIGHT_HOOKS", {})
    db, source = _store_at_38(tmp_path)
    conn = sqlite3.connect(str(db))
    before, version = _counts(conn), schema_migration.get_user_version(conn)
    conn.close()
    print(f"migration 0039 source store: {source} at user_version {version}")
    assert auto_apply_mod.auto_apply(db) == ([39] if version < 39 else [])
    conn = sqlite3.connect(str(db))
    try:
        assert schema_migration.get_user_version(conn) >= 39
        table = schema_manifest.table_at(39, "deliverable_dependencies")
        assert schema_manifest.validate_table(conn, table) == []
        assert [r[1] for r in sorted(conn.execute("PRAGMA table_info(deliverable_dependencies)"),
                                     key=lambda r: r[5]) if r[5]] == ["project_id", "deliverable_id", "depends_on_id"]
        after = _counts(conn)
    finally:
        conn.close()
    assert {k: v for k, v in after.items() if k in before} == before
    assert set(after) - set(before) <= {"deliverable_dependencies"}


@pytest.fixture()
def db_at_38(tmp_path: Path) -> Path:
    db = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 38")
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def migrated(db_at_38: Path) -> sqlite3.Connection:
    assert apply_0039.apply_migration(db_at_38, SQL) is True
    conn = sqlite3.connect(str(db_at_38))
    yield conn
    conn.close()


def _insert(conn: sqlite3.Connection, pid: str, dlv: str, on: str, kind: str = "hard",
            removed_at: str | None = None, removed_reason: str | None = None) -> None:
    conn.execute("INSERT INTO deliverable_dependencies (project_id, deliverable_id, depends_on_id, kind, created_at, "
                 "created_by, removed_at, removed_reason) VALUES (?, ?, ?, ?, '2026-11-19T00:00:00Z', 't0', ?, ?)",
                 (pid, dlv, on, kind, removed_at, removed_reason))


def test_apply_stamps_39_and_a_second_run_is_skipped(db_at_38, migrated):
    assert schema_migration.get_user_version(migrated) == 39
    assert schema_manifest.validate_table(migrated, schema_manifest.table_at(39, apply_0039.TABLE)) == []
    assert apply_0039.apply_migration(db_at_38, SQL) is False


def test_composite_key_is_per_project(migrated):
    _insert(migrated, "p1", "dlv-b", "dlv-a")
    _insert(migrated, "p2", "dlv-b", "dlv-a")
    _insert(migrated, "p1", "dlv-c", "dlv-a")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(migrated, "p1", "dlv-b", "dlv-a", kind="soft")


def test_kind_check_refuses_an_unknown_kind(migrated):
    _insert(migrated, "p1", "dlv-b", "dlv-a", kind="soft")
    for bad in ("HARD", "", "maybe"):
        with pytest.raises(sqlite3.IntegrityError):
            _insert(migrated, "p1", "dlv-c", "dlv-a", kind=bad)
    migrated.execute("INSERT INTO deliverable_dependencies (project_id, deliverable_id, depends_on_id, created_at, "
                     "created_by) VALUES ('p1', 'dlv-d', 'dlv-a', 'now', 't0')")
    assert migrated.execute("SELECT kind FROM deliverable_dependencies WHERE deliverable_id = 'dlv-d'").fetchone() \
        == ("hard",)


@pytest.mark.parametrize("removed_at, removed_reason", [("2026-11-19T01:00:00Z", None), (None, "no longer needed")])
def test_removed_at_and_reason_come_together(migrated, removed_at, removed_reason):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(migrated, "p1", "dlv-b", "dlv-a", removed_at=removed_at, removed_reason=removed_reason)
    _insert(migrated, "p1", "dlv-b", "dlv-a", removed_at="2026-11-19T01:00:00Z", removed_reason="merged together")


def test_a_table_in_another_shape_rolls_back_and_stays_at_38(db_at_38):
    conn = sqlite3.connect(str(db_at_38))
    # Same columns as 0039 minus the key and the CHECKs: IF NOT EXISTS skips it, the index still builds.
    conn.execute("CREATE TABLE deliverable_dependencies (project_id TEXT, deliverable_id TEXT, depends_on_id TEXT)")
    conn.execute("INSERT INTO deliverable_dependencies VALUES ('p1', 'dlv-b', 'dlv-a')")
    conn.commit()
    conn.close()
    before = db_at_38.read_bytes()
    with pytest.raises(apply_0039.Migration0039Error, match="did not reach the 0039 shape"):
        apply_0039.apply_migration(db_at_38, SQL)
    assert db_at_38.read_bytes() == before
    conn = sqlite3.connect(str(db_at_38))
    try:
        assert schema_migration.get_user_version(conn) == 38
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'idx_deliverable_dependencies_on'") \
            .fetchall() == []
    finally:
        conn.close()


def test_statements_drop_comments_and_split():
    statements = apply_0039._statements(SQL.read_text(encoding="utf-8"))
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS deliverable_dependencies")
    assert statements[1].startswith("CREATE INDEX IF NOT EXISTS idx_deliverable_dependencies_on")
