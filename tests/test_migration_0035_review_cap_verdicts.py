"""Migration 0035: review_cap_verdicts, the tiebreaker's verdicts at the B3 review cap (R2a, ADR-041).

Dispatch 20261020-rs-r2a-tiebreaker. Pinned on a copy of a v34 store, built here
the way a real store reaches 34 (init_schema, then every numbered migration up
to 34), never on a store under ~/.vnx-data:

- 0035 takes it to 35: the table with its ADR-007 key over (project_id,
  pr_number, verdict_id), the store's id as the project_id default, the
  one-verkleinen index, a clean composite_index_audit and the v35 manifest;
- a second run is a no-op, byte for byte;
- an unresolvable store and a table of that name in another shape are refused
  and left untouched at 34;
- the store itself refuses a second verkleinen on one PR.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

# tests/conftest.py puts scripts/lib on sys.path.
import schema_manifest
import schema_migration
from coordination_db import composite_index_audit, db_path_from_state_dir, init_schema
from migrations import auto_apply as auto_apply_mod
from migrations.auto_apply import auto_apply
from project_scope import TenantUnresolved

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
TABLE = "review_cap_verdicts"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    saved = {k: list(v) for k, v in schema_migration._PREFLIGHT_HOOKS.items()}
    schema_migration._PREFLIGHT_HOOKS.clear()
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    yield
    schema_migration._PREFLIGHT_HOOKS.clear()
    schema_migration._PREFLIGHT_HOOKS.update(saved)


@pytest.fixture(scope="module")
def v34_template(tmp_path_factory) -> Path:
    """A store of project ``proj-a`` at user_version 34, built once."""
    root = tmp_path_factory.mktemp("v34")
    upto_34 = root / "migrations-upto-34"
    upto_34.mkdir()
    for number, sql_path in auto_apply_mod._discover_migrations(_MIGRATIONS_DIR):
        if number <= 34:
            shutil.copyfile(sql_path, upto_34 / sql_path.name)
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("VNX_PROJECT_ID", raising=False)
        db = _store_db(root / "template", "proj-a")
        init_schema(db.parent)
        auto_apply(db, migrations_dir=upto_34)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()
    assert _user_version(db) == 34
    return db


def _store_db(root: Path, pid: str) -> Path:
    state_dir = root / ".vnx-data" / pid / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return db_path_from_state_dir(state_dir)


@pytest.fixture
def v34_store(v34_template, tmp_path) -> Path:
    db = _store_db(tmp_path / "copy", "proj-a")
    shutil.copyfile(v34_template, db)
    return db


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _file_hash(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def _schema(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        return sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall(), key=str)
    finally:
        conn.close()


def test_a_v34_store_reaches_35_with_the_table_and_its_key(v34_store):
    assert TABLE not in {row[1] for row in _schema(v34_store)}

    assert auto_apply(v34_store) == [35, 36]

    assert _user_version(v34_store) == 36
    conn = sqlite3.connect(str(v34_store))
    try:
        info = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        pk = tuple(row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5]))
        defaults = {row[1]: row[4] for row in info}
        audit = composite_index_audit(conn)
        manifest = schema_manifest.validate_db_at_version(conn, 35)
    finally:
        conn.close()
    assert pk == ("project_id", "pr_number", "verdict_id")
    assert defaults["project_id"] == "'proj-a'"
    assert (audit["missing"], audit["failures"]) == ([], [])
    assert manifest == []
    assert schema_manifest.TERMINAL_VERSION == 36


def test_a_second_run_is_a_noop(v34_store):
    auto_apply(v34_store)
    before = (_schema(v34_store), _file_hash(v34_store))

    assert auto_apply(v34_store) == []

    assert (_schema(v34_store), _file_hash(v34_store)) == before


def test_an_unresolvable_store_is_refused_and_left_untouched(v34_template, tmp_path):
    loose = tmp_path / "loose" / "runtime_coordination.db"
    loose.parent.mkdir()
    shutil.copyfile(v34_template, loose)
    before = (_user_version(loose), _file_hash(loose))

    with pytest.raises(TenantUnresolved):
        auto_apply(loose)

    assert (_user_version(loose), _file_hash(loose)) == before == (34, before[1])


def test_a_table_of_that_name_in_another_shape_is_refused_and_left_untouched(v34_store):
    conn = sqlite3.connect(str(v34_store))
    conn.execute(f"CREATE TABLE {TABLE} (verdict_id TEXT PRIMARY KEY, outcome TEXT)")
    conn.commit()
    conn.close()
    before = (_user_version(v34_store), _file_hash(v34_store))

    # auto_apply loads the runner from its file, so its Migration0035Error is another class object.
    with pytest.raises(RuntimeError, match="did not reach the 0035 shape"):
        auto_apply(v34_store)

    assert (_user_version(v34_store), _file_hash(v34_store)) == before


def test_the_store_refuses_a_second_verkleinen_on_one_pr(v34_store):
    auto_apply(v34_store)
    row = (
        "INSERT INTO review_cap_verdicts (pr_number, verdict_id, head_sha, cap_rule, outcome, chosen_outcome, "
        "intervention, granted_dispatch_id, created_at) VALUES (7, ?, ?, 'B3.1', 'verkleinen', 'verkleinen', "
        "'split the parser', 'd-x', '2026-10-20T10:00:00Z')"
    )
    conn = sqlite3.connect(str(v34_store))
    try:
        conn.execute(row, ("cap-pr7-a", "a" * 40))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(row, ("cap-pr7-b", "b" * 40))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO review_cap_verdicts (pr_number, verdict_id, head_sha, cap_rule, outcome, "
                "chosen_outcome, created_at) VALUES (8, 'cap-pr8-c', 'c', 'B3.2', 'verkleinen', 'verkleinen', 'x')"
            )
        stored = conn.execute("SELECT project_id, verdict_id FROM review_cap_verdicts").fetchall()
    finally:
        conn.close()
    assert stored == [("proj-a", "cap-pr7-a")]
