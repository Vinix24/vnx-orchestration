"""Migration 0038: intelligence_injections.ab_arm and ab_window, up, down and the manifest (track intelligence-effect-measurable, PR-3). Temp stores only."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import schema_manifest
from migrations import apply_0038
from migrations.auto_apply import auto_apply

from fixtures.live_store_fixture import build_live_store

SQL = _ROOT / "schemas" / "migrations" / "0038_intelligence_injections_ab_arm.sql"
DOWN_SQL = _ROOT / "schemas" / "migrations" / "0038_intelligence_injections_ab_arm_down.sql"
OLD_UP_SQL = _ROOT / "schemas" / "migrations" / "2026_05_intelligence_ab_arm.sql"

_INJECTIONS_V37 = """
CREATE TABLE intelligence_injections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    injection_id        TEXT    NOT NULL,
    dispatch_id         TEXT    NOT NULL,
    injection_point     TEXT    NOT NULL,
    task_class          TEXT    NOT NULL,
    items_injected      INTEGER NOT NULL DEFAULT 0,
    items_json          TEXT    NOT NULL DEFAULT '[]',
    injected_at         TEXT    NOT NULL DEFAULT '2026-11-10T00:00:00Z',
    project_id          TEXT    NOT NULL DEFAULT 'p1',
    UNIQUE (injection_id, project_id)
);
CREATE UNIQUE INDEX ux_intelligence_injections_pid ON intelligence_injections (project_id, id);
CREATE INDEX idx_injection_dispatch ON intelligence_injections (dispatch_id);
"""


def _q(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
    finally:
        conn.close()
    return rows


def _columns(db: Path) -> list[str]:
    return [r[1] for r in _q(db, "PRAGMA table_info(intelligence_injections)")]


def _version(db: Path) -> int:
    return _q(db, "PRAGMA user_version")[0][0]


def _legacy_counter(db: Path) -> int:
    return _q(db, "SELECT MAX(version) FROM runtime_schema_version")[0][0]


def _injection_violations(db: Path) -> list[str]:
    """What the version-38 manifest says about intelligence_injections. The live-shaped fixture
    store does not satisfy the other tables' invariants, so only this table is read."""
    conn = sqlite3.connect(str(db))
    try:
        return [v for v in schema_manifest.validate_db_at_version(conn, 38) if v.startswith(apply_0038.TABLE)]
    finally:
        conn.close()


def _snapshot(db: Path) -> list[tuple]:
    """Every row on the columns that exist at v37."""
    return _q(db, "SELECT id, injection_id, dispatch_id, injection_point, task_class, items_injected, "
                  "items_json, injected_at, project_id FROM intelligence_injections ORDER BY id")


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """A live-shaped store at user_version 37 whose intelligence_injections has no arm columns."""
    path = build_live_store(tmp_path / "state")
    conn = sqlite3.connect(str(path))
    conn.executescript(_INJECTIONS_V37)
    conn.execute("CREATE TABLE IF NOT EXISTS runtime_schema_version (version INTEGER PRIMARY KEY, description TEXT)")
    conn.executemany("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (?, 'x')",
                     [(v,) for v in range(1, 16)])
    for i, project in enumerate(("p1", "p1", "p2")):
        conn.execute("INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, task_class, "
                     "items_injected, project_id) VALUES (?, ?, 'dispatch_create', '01_code_generation', ?, ?)",
                     (f"inj-{i}", f"dlv-{i}", i, project))
    conn.execute("PRAGMA user_version = 37")
    conn.commit()
    conn.close()
    return path


def test_auto_apply_takes_a_store_from_37_to_38_and_adds_both_columns(db):
    assert "ab_arm" not in _columns(db) and "ab_window" not in _columns(db)
    assert auto_apply(db) == [38]
    assert _version(db) == 38
    assert {"ab_arm", "ab_window"} <= set(_columns(db))
    assert _q(db, "SELECT ab_arm, ab_window FROM intelligence_injections") == [(None, None)] * 3
    names = {r[1] for r in _q(db, "PRAGMA index_list(intelligence_injections)")}
    assert apply_0038.WINDOW_INDEX in names
    cols = [r[2] for r in _q(db, f"PRAGMA index_info({apply_0038.WINDOW_INDEX})")]
    assert cols == ["project_id", "ab_window", "ab_arm"]  # ADR-007: project_id first
    assert _injection_violations(db) == []


def test_a_second_run_is_a_skip(db):
    assert apply_0038.apply_migration(db, SQL) is True
    assert apply_0038.apply_migration(db, SQL) is False
    assert auto_apply(db) == []


def test_a_store_that_already_has_ab_arm_reaches_38_without_error(db):
    _q(db, "ALTER TABLE intelligence_injections ADD COLUMN ab_arm TEXT DEFAULT 'treatment'")
    _q(db, "CREATE INDEX idx_injection_ab_arm ON intelligence_injections (ab_arm)")
    assert auto_apply(db) == [38]
    assert _version(db) == 38
    assert {"ab_arm", "ab_window"} <= set(_columns(db))
    # the value that was already there is kept, not reset
    assert _q(db, "SELECT DISTINCT ab_arm FROM intelligence_injections") == [("treatment",)]


def test_a_store_that_has_both_columns_reaches_38(db):
    _q(db, "ALTER TABLE intelligence_injections ADD COLUMN ab_arm TEXT")
    _q(db, "ALTER TABLE intelligence_injections ADD COLUMN ab_window TEXT")
    assert apply_0038.apply_migration(db, SQL) is True
    assert _version(db) == 38


def test_the_round_trip_37_38_37_38_keeps_other_columns_and_rows(db):
    before = _snapshot(db)
    columns_before = [c for c in _columns(db)]
    legacy = _legacy_counter(db)
    assert legacy == 15

    assert apply_0038.apply_migration(db, SQL) is True
    assert _snapshot(db) == before
    assert _legacy_counter(db) == 15

    assert apply_0038.revert_migration(db) is True
    assert _version(db) == 37
    assert _columns(db) == columns_before
    assert _snapshot(db) == before
    assert _legacy_counter(db) == 15
    names = {r[1] for r in _q(db, "PRAGMA index_list(intelligence_injections)")}
    assert apply_0038.WINDOW_INDEX not in names
    assert {"ux_intelligence_injections_pid", "idx_injection_dispatch"} <= names

    assert apply_0038.apply_migration(db, SQL) is True
    assert _version(db) == 38
    assert _snapshot(db) == before
    assert _legacy_counter(db) == 15
    assert {"ab_arm", "ab_window"} <= set(_columns(db))


def test_down_drops_stamped_values_with_the_columns_and_the_legacy_index(db):
    _q(db, "ALTER TABLE intelligence_injections ADD COLUMN ab_arm TEXT")
    _q(db, "CREATE INDEX idx_injection_ab_arm ON intelligence_injections (ab_arm)")
    apply_0038.apply_migration(db, SQL)
    _q(db, "UPDATE intelligence_injections SET ab_arm = 'control', ab_window = 'w1:abc' WHERE id = 1")
    before = _snapshot(db)
    assert apply_0038.revert_migration(db) is True
    assert "ab_arm" not in _columns(db) and "ab_window" not in _columns(db)
    assert _snapshot(db) == before
    assert _version(db) == 37


def test_down_is_a_no_op_at_37(db):
    assert apply_0038.revert_migration(db) is False
    assert _version(db) == 37


@pytest.mark.parametrize("version", [36, 39, 0])
def test_down_refuses_a_store_that_is_neither_37_nor_38(db, version):
    _q(db, f"PRAGMA user_version = {version}")
    with pytest.raises(apply_0038.Migration0038Error, match=f"found {version}"):
        apply_0038.revert_migration(db)
    assert _version(db) == version
    assert "ab_arm" not in _columns(db)


def test_down_refuses_up_front_on_a_sqlite_without_drop_column(db, monkeypatch):
    apply_0038.apply_migration(db, SQL)
    monkeypatch.setattr(apply_0038.sqlite3, "sqlite_version_info", (3, 34, 0))
    with pytest.raises(apply_0038.Migration0038Error, match="DROP COLUMN"):
        apply_0038.revert_migration(db)
    assert _version(db) == 38 and "ab_window" in _columns(db)


def test_a_failing_up_rolls_back_and_leaves_the_store_at_37(db):
    # the index needs project_id, which this table lacks
    _q(db, "DROP TABLE intelligence_injections")
    _q(db, "CREATE TABLE intelligence_injections (id INTEGER PRIMARY KEY, injection_id TEXT)")
    with pytest.raises(sqlite3.Error):
        apply_0038.apply_migration(db, SQL)
    assert _version(db) == 37
    assert "ab_arm" not in _columns(db) and "ab_window" not in _columns(db)


def test_a_store_without_the_table_is_stamped_and_nothing_is_created(db):
    _q(db, "DROP TABLE intelligence_injections")
    assert apply_0038.apply_migration(db, SQL) is True
    assert _version(db) == 38
    assert _q(db, "SELECT name FROM sqlite_master WHERE name = 'intelligence_injections'") == []
    assert apply_0038.revert_migration(db) is True
    assert _version(db) == 37


@pytest.mark.parametrize("dropped", [["ab_arm"], ["ab_window"], ["ab_arm", "ab_window"]])
def test_validate_db_at_version_38_reports_a_store_that_claims_38_without_the_columns(db, dropped):
    apply_0038.apply_migration(db, SQL)
    assert _injection_violations(db) == []
    _q(db, f"DROP INDEX {apply_0038.WINDOW_INDEX}")
    for column in dropped:
        _q(db, f"ALTER TABLE intelligence_injections DROP COLUMN {column}")
    assert _version(db) == 38
    violations = _injection_violations(db)
    assert violations
    for column in dropped:
        assert any(column in v for v in violations), violations
    assert any(apply_0038.WINDOW_INDEX in v for v in violations), violations


def test_validate_db_at_version_38_reports_a_store_that_has_a_non_text_column(db):
    _q(db, "ALTER TABLE intelligence_injections ADD COLUMN ab_arm INTEGER")
    _q(db, "ALTER TABLE intelligence_injections ADD COLUMN ab_window TEXT")
    _q(db, f"CREATE INDEX {apply_0038.WINDOW_INDEX} ON intelligence_injections (project_id, ab_window, ab_arm)")
    violations = _injection_violations(db)
    assert any("ab_arm" in v for v in violations), violations


def test_the_two_sql_files_exist_and_the_old_pair_is_marked_superseded():
    assert "ab_window" in SQL.read_text(encoding="utf-8")
    assert "revert_migration" in DOWN_SQL.read_text(encoding="utf-8")
    head = OLD_UP_SQL.read_text(encoding="utf-8").splitlines()[:6]
    assert head[0].startswith("-- SUPERSEDED by 0038_intelligence_injections_ab_arm.sql")
    assert all(line.startswith("--") for line in head)
