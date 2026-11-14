"""Characterisation tests for golf-schoon R01c: rls_policy ``verify``.

They pin the observable behaviour of the field-test verifier (verdict dict,
evidence string, check order, early-failure shapes) against the real sqlite3
CLI and real temporary seed directories, so the extract-method refactor can be
shown green on both the old and the new code.
"""
from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TASK_DIR = REPO / "scripts/benchmark/field-tests/tasks/t1_trivial/02_rls_policy"

pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed"
)


def _load():
    spec = importlib.util.spec_from_file_location("rls_policy_verify", TASK_DIR / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load()

GOOD_MIGRATE = """
ALTER TABLE scan_quota RENAME TO scan_quota_old;
CREATE TABLE IF NOT EXISTS scan_quota (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100,
    project_id TEXT NOT NULL DEFAULT 'default'
);
INSERT INTO scan_quota (id, scan_id, used_count, quota_limit)
    SELECT id, scan_id, used_count, quota_limit FROM scan_quota_old;
DROP TABLE scan_quota_old;
CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_project ON scan_quota(project_id, scan_id);
CREATE INDEX IF NOT EXISTS ix_scan_project ON scan_quota(project_id);
"""

# Guarded by a table-info lookup so a second run is a no-op (idempotent).
IDEMPOTENT_MIGRATE = """
DROP TABLE IF EXISTS scan_quota;
CREATE TABLE scan_quota (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100,
    project_id TEXT NOT NULL DEFAULT 'default'
);
INSERT INTO scan_quota (id, scan_id, used_count, quota_limit) VALUES
    (1, 'scan_a', 0, 100), (2, 'scan_b', 5, 100), (3, 'scan_c', 10, 100);
CREATE UNIQUE INDEX uq_scan_project ON scan_quota(project_id, scan_id);
CREATE INDEX ix_scan_project ON scan_quota(project_id);
"""

NAMES = [
    "3 original rows scoped to 'default' tenant",
    "cross-tenant insert allowed",
    "same-tenant duplicate rejected",
    "composite UNIQUE on (project_id, scan_id) exists",
    "index on (project_id) exists",
    "migration is idempotent (re-runs cleanly)",
]


def _workdir(tmp_path: Path, migrate: str | None, init: bool = True) -> Path:
    seed = tmp_path / V.SEED_REL
    seed.mkdir(parents=True)
    if init:
        shutil.copy(TASK_DIR / "seed/init_seed_db.sql", seed / "init_seed_db.sql")
    if migrate is not None:
        (seed / "migrate.sql").write_text(migrate, encoding="utf-8")
    return tmp_path


def test_missing_migrate_sql(tmp_path):
    out = V.verify(_workdir(tmp_path, None), {})
    assert out == {
        "pass": False,
        "evidence": "migrate.sql not written by worker",
        "details": {"pass_count": 0, "expected": 5, "files_written": []},
    }


def test_missing_init_script(tmp_path):
    out = V.verify(_workdir(tmp_path, GOOD_MIGRATE, init=False), {})
    assert out == {
        "pass": False,
        "evidence": "seed init script missing (test bug)",
        "details": {"pass_count": 0, "expected": 5, "files_written": ["migrate.sql"]},
    }


def test_seed_init_failure(tmp_path):
    wd = _workdir(tmp_path, GOOD_MIGRATE)
    (wd / V.SEED_REL / "init_seed_db.sql").write_text("NOT VALID SQL;\n", encoding="utf-8")
    out = V.verify(wd, {})
    assert out["pass"] is False
    assert out["evidence"].startswith("seed init failed: sqlite3 rc=")
    assert out["details"] == {"pass_count": 0, "expected": 5, "files_written": ["migrate.sql"]}


def test_migration_failure(tmp_path):
    out = V.verify(_workdir(tmp_path, "THIS IS NOT SQL;\n"), {})
    assert out["pass"] is False
    assert out["evidence"].startswith("migration failed: sqlite3 rc=")
    assert out["details"] == {"pass_count": 0, "expected": 5, "files_written": ["migrate.sql"]}


def test_full_pass_with_idempotent_migration(tmp_path):
    out = V.verify(_workdir(tmp_path, IDEMPOTENT_MIGRATE), {})
    assert out["pass"] is True
    assert out["evidence"] == "; ".join(f"PASS {n}" for n in NAMES)
    d = out["details"]
    assert d["pass_count"] == 5 and d["expected"] == 5
    assert d["files_written"] == ["migrate.sql"]
    assert d["idempotent"] is True
    assert [c["name"] for c in d["checks"]] == NAMES
    assert all(c["ok"] for c in d["checks"])
    notes = {c["name"]: c["note"] for c in d["checks"]}
    assert notes[NAMES[0]] == "found 3, expected 3"
    assert notes[NAMES[1]] == "inserted scan_a/tenant_x"
    assert notes[NAMES[2]] == "UNIQUE constraint fired as expected"
    assert notes[NAMES[5]] == "re-applied OK"
    assert notes[NAMES[3]].startswith("indexes: [")


def test_non_idempotent_migration_keeps_five_of_six_and_passes_count(tmp_path):
    out = V.verify(_workdir(tmp_path, GOOD_MIGRATE), {})
    d = out["details"]
    assert d["idempotent"] is False
    assert d["checks"][-1]["name"] == NAMES[5]
    assert d["checks"][-1]["ok"] is False
    assert d["checks"][-1]["note"].startswith("sqlite3 rc=")
    assert d["pass_count"] == 5  # capped at expected
    assert out["pass"] is True  # 5 passing checks reach the threshold
    assert f"FAIL {NAMES[5]}" in out["evidence"]


def test_missing_composite_unique_fails_duplicate_and_composite_checks(tmp_path):
    sql = IDEMPOTENT_MIGRATE.replace(
        "CREATE UNIQUE INDEX uq_scan_project ON scan_quota(project_id, scan_id);\n", ""
    )
    out = V.verify(_workdir(tmp_path, sql), {})
    checks = {c["name"]: c for c in out["details"]["checks"]}
    assert checks[NAMES[2]]["ok"] is False
    assert checks[NAMES[2]]["note"] == "duplicate insert was allowed"
    assert checks[NAMES[3]]["ok"] is False
    assert checks[NAMES[4]]["ok"] is True
    assert out["details"]["pass_count"] == 4
    assert out["pass"] is False
    assert out["evidence"].startswith(f"PASS {NAMES[0]}; PASS {NAMES[1]}; FAIL {NAMES[2]}")


def test_missing_project_id_column_raises_operational_error(tmp_path):
    # The first schema query runs outside the try block: a migration that adds
    # no project_id column surfaces as sqlite3.OperationalError, not a verdict.
    with pytest.raises(sqlite3.OperationalError):
        V.verify(_workdir(tmp_path, "SELECT 1;\n"), {})


def test_apply_sql_script_ok_returns_stdout(tmp_path):
    sql = tmp_path / "a.sql"
    sql.write_text("SELECT 41 + 1;\n", encoding="utf-8")
    assert V._apply_sql_script(tmp_path / "x.db", sql) == (True, "42\n")


def test_apply_sql_script_error_reports_returncode(tmp_path):
    sql = tmp_path / "a.sql"
    sql.write_text("NOPE;\n", encoding="utf-8")
    ok, msg = V._apply_sql_script(tmp_path / "x.db", sql)
    assert ok is False and msg.startswith("sqlite3 rc=1: ")


def test_apply_sql_script_unreadable_sql_is_oserror_branch(tmp_path):
    ok, msg = V._apply_sql_script(tmp_path / "x.db", tmp_path / "missing.sql")
    assert ok is False and msg.startswith("sqlite3 invocation failed: ")


def test_verify_does_not_touch_seed_db(tmp_path):
    wd = _workdir(tmp_path, IDEMPOTENT_MIGRATE)
    before = sorted(p.name for p in (wd / V.SEED_REL).iterdir())
    V.verify(wd, {})
    assert sorted(p.name for p in (wd / V.SEED_REL).iterdir()) == before
