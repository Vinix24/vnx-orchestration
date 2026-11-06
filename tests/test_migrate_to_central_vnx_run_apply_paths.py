"""Characterisation tests for the entry, gate and exit paths of ``_run_apply``.

G2-7 of the ``functies-onder-200`` track splits
``scripts/migrate_to_central_vnx.py::_run_apply`` (281 executable lines on
``origin/main``) into private helpers.  These tests pin the observable
behaviour of every path *before* the split — exit code, files present
afterwards, central rows and the key stdout/log lines — and stay green across
the move.

Paths covered: normal apply, ``--test-apply`` (success and failure),
``--verify-only`` (pass and mismatch), the plan-only default mode, a declined
confirm, ``--dry-run-manifest`` (missing, stale and fresh), a fresh central
with and without ``--fresh-central``, a partial QI DB, and the failures that
return 1, 2, 3 and 4.

Only external processes (``subprocess.call``), the interactive confirm and the
failure injectors are patched; every database is a real SQLite file under
``tmp_path``.

Dispatch-ID: dlv-ef1b2b363973
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402
from scripts.lib import migrate_import as _MI  # noqa: E402


# ---------------------------------------------------------------------------
# Real SQLite fixtures (one QI + one RC per project, plus a live central)
# ---------------------------------------------------------------------------


def _make_qi_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                pattern_data TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _make_rc_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT
            );
            CREATE TABLE dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT INTO runtime_schema_version (version, description) VALUES (10, 'fixture');
            """
        )
        con.commit()
    finally:
        con.close()


def _make_partial_qi_db(path: Path, rows: int = 2) -> None:
    """Only ``dispatch_experiments`` — the OI-011 partial-init state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE dispatch_experiments (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT UNIQUE,
                role TEXT,
                model TEXT,
                terminal TEXT
            );
            """
        )
        for i in range(rows):
            con.execute(
                "INSERT INTO dispatch_experiments (dispatch_id, role, model, terminal)"
                " VALUES (?, 'backend-developer', 'sonnet', 'T1')",
                (f"partial-{i}",),
            )
        con.commit()
    finally:
        con.close()


def _seed_source(path_qi: Path, path_rc: Path, project_id: str) -> None:
    with sqlite3.connect(path_qi) as c:
        c.executemany(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("approach", "test", f"{project_id}-p1", "d", "{}", project_id),
                ("approach", "test", f"{project_id}-p2", "d", "{}", project_id),
            ],
        )
        c.execute(
            "INSERT INTO pattern_usage VALUES (?, ?, ?, ?)",
            ("shared-key", f"{project_id}-title", "hash", project_id),
        )
    with sqlite3.connect(path_rc) as c:
        c.execute(
            "INSERT INTO dispatches VALUES (?, ?, ?)",
            ("shared-dispatch", "completed", project_id),
        )


def _table_names(db: Path) -> set[str]:
    if not db.exists():
        return set()
    with sqlite3.connect(db) as c:
        return {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }


def _row_count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _drop_central_dbs(env: dict) -> None:
    for db in (env["central_qi"], env["central_rc"]):
        for suffix in ("", "-wal", "-shm"):
            path = db.with_name(db.name + suffix)
            if path.exists():
                path.unlink()


@pytest.fixture
def apply_env(tmp_path: Path, monkeypatch) -> dict:
    """Two real source projects + a live (non-fresh) central state dir."""
    abort_flag = tmp_path / ".vnx-aggregator" / "ABORT"
    monkeypatch.setattr(M, "ABORT_FLAG", abort_flag)
    monkeypatch.setattr(_MI, "ABORT_FLAG", abort_flag)

    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    central_state = tmp_path / "central" / "state"
    central_qi = central_state / "quality_intelligence.db"
    central_rc = central_state / "runtime_coordination.db"
    _make_qi_db(central_qi)
    _make_rc_db(central_rc)

    specs: list[dict] = []
    for name in ("proj-a", "proj-b"):
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        qi = state / "quality_intelligence.db"
        rc = state / "runtime_coordination.db"
        _make_qi_db(qi)
        _make_rc_db(rc)
        _seed_source(qi, rc, name)
        specs.append({"name": name, "path": str(proj), "project_id": name})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    return {
        "tmp_path": tmp_path,
        "backup_base": backup_base,
        "central_state": central_state,
        "central_qi": central_qi,
        "central_rc": central_rc,
        "registry": registry,
        "abort_flag": abort_flag,
    }


def _apply_cmd(env: dict, *extra: str) -> list[str]:
    return [
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(env["registry"]),
        "--backup-base", str(env["backup_base"]),
        "--central-state", str(env["central_state"]),
        *extra,
    ]


def _apply(env: dict, *extra: str) -> int:
    return M.main(_apply_cmd(env, *extra))


def _verify_only(env: dict) -> int:
    return M.main([
        "--verify-only",
        "--registry", str(env["registry"]),
        "--central-state", str(env["central_state"]),
    ])


def _test_apply_tmp_dir_from_log(caplog: pytest.LogCaptureFixture) -> Path:
    """The temp dir the redirect log line names (the run under test creates it)."""
    for record in caplog.records:
        if record.getMessage().startswith("test-apply: redirecting central_state"):
            return Path(record.args[1])
    raise AssertionError("test-apply never logged its temp dir redirect")


# ---------------------------------------------------------------------------
# Normal apply
# ---------------------------------------------------------------------------


def test_normal_apply_exits_zero_and_imports_rows(apply_env, capsys):
    rc = _apply(apply_env)
    out = capsys.readouterr().out

    assert rc == 0
    assert "P4 import complete. Backup:" in out
    assert f"Central QI: {apply_env['central_qi']}" in out
    assert f"Central RC: {apply_env['central_rc']}" in out
    assert "[proj-a] quality_intelligence.db success_patterns: +2 (0 idempotent skips)" in out

    assert _row_count(apply_env["central_qi"], "success_patterns") == 4
    with sqlite3.connect(apply_env["central_qi"]) as c:
        pattern_ids = {row[0] for row in c.execute("SELECT pattern_id FROM pattern_usage")}
    assert pattern_ids == {"proj-a:shared-key", "proj-b:shared-key"}

    backup_dirs = [d for d in apply_env["backup_base"].iterdir() if d.is_dir()]
    assert len(backup_dirs) == 1
    manifest = backup_dirs[0] / "manifest.sha256"
    assert manifest.is_file()
    assert len(manifest.read_text().strip().splitlines()) == 2


def test_normal_apply_json_reports_payload(apply_env, capsys):
    rc = _apply(apply_env, "--json")
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["central_qi"] == str(apply_env["central_qi"])
    assert payload["central_rc"] == str(apply_env["central_rc"])
    assert payload["failed_projects"] == []
    assert "test_apply" not in payload
    assert {entry["project_id"] for entry in payload["imported_summary"]} == {"proj-a", "proj-b"}


# ---------------------------------------------------------------------------
# --dry-run-manifest gate
# ---------------------------------------------------------------------------


def test_dry_run_manifest_missing_exits_two(apply_env, caplog, capsys):
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")
    missing = apply_env["tmp_path"] / "does-not-exist.json"
    pre_rows = _row_count(apply_env["central_qi"], "success_patterns")

    rc = _apply(apply_env, "--dry-run-manifest", str(missing))

    assert rc == 2
    assert f"dry-run manifest missing: {missing}" in " ".join(
        record.getMessage() for record in caplog.records
    )
    assert _row_count(apply_env["central_qi"], "success_patterns") == pre_rows
    assert list(apply_env["backup_base"].iterdir()) == []


def test_dry_run_manifest_stale_exits_two(apply_env, caplog):
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")
    stale = apply_env["tmp_path"] / "stale-dry-run.json"
    stale.write_text("{}")
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))

    rc = _apply(apply_env, "--dry-run-manifest", str(stale))

    assert rc == 2
    assert "hours old (> 24); regenerate before applying" in " ".join(
        record.getMessage() for record in caplog.records
    )
    assert list(apply_env["backup_base"].iterdir()) == []


def test_dry_run_manifest_fresh_allows_apply(apply_env):
    fresh = apply_env["tmp_path"] / "fresh-dry-run.json"
    fresh.write_text("{}")

    rc = _apply(apply_env, "--dry-run-manifest", str(fresh))

    assert rc == 0
    assert _row_count(apply_env["central_qi"], "success_patterns") == 4


# ---------------------------------------------------------------------------
# Confirmation / abort (exit 1)
# ---------------------------------------------------------------------------


def test_declined_confirm_exits_one_without_writes(apply_env):
    rc = M.main([
        "--apply",
        "--no-prompt",
        "--registry", str(apply_env["registry"]),
        "--backup-base", str(apply_env["backup_base"]),
        "--central-state", str(apply_env["central_state"]),
    ])

    assert rc == 1
    assert _row_count(apply_env["central_qi"], "success_patterns") == 0
    assert list(apply_env["backup_base"].iterdir()) == []
    assert not apply_env["abort_flag"].exists()


def test_abort_flag_exits_one_before_backup(apply_env):
    apply_env["abort_flag"].parent.mkdir(parents=True, exist_ok=True)
    apply_env["abort_flag"].write_text("stop")

    rc = _apply(apply_env)

    assert rc == 1
    assert list(apply_env["backup_base"].iterdir()) == []
    assert _row_count(apply_env["central_qi"], "success_patterns") == 0


# ---------------------------------------------------------------------------
# Plan-only default mode (no --apply)
# ---------------------------------------------------------------------------


def test_plan_only_delegates_to_dry_run_subprocess(apply_env, monkeypatch):
    calls: list[list[str]] = []

    class _FakeSubprocess:
        @staticmethod
        def call(cmd):
            calls.append(list(cmd))
            return 7

    monkeypatch.setattr(M, "subprocess", _FakeSubprocess)

    rc = M.main([
        "--registry", str(apply_env["registry"]),
        "--central-state", str(apply_env["central_state"]),
    ])

    assert rc == 7
    assert len(calls) == 1
    assert calls[0][0] == sys.executable
    assert calls[0][1] == str(ROOT / "scripts" / "migrate_dry_run.py")
    assert calls[0][2:] == ["--registry", str(apply_env["registry"])]
    assert _row_count(apply_env["central_qi"], "success_patterns") == 0
    assert list(apply_env["backup_base"].iterdir()) == []


def test_plan_only_passes_json_flag_to_dry_run(apply_env, monkeypatch):
    calls: list[list[str]] = []

    class _FakeSubprocess:
        @staticmethod
        def call(cmd):
            calls.append(list(cmd))
            return 0

    monkeypatch.setattr(M, "subprocess", _FakeSubprocess)

    rc = M.main([
        "--json",
        "--registry", str(apply_env["registry"]),
        "--central-state", str(apply_env["central_state"]),
    ])

    assert rc == 0
    assert calls[0][-1] == "--json"


# ---------------------------------------------------------------------------
# --verify-only (exit 0 and 4)
# ---------------------------------------------------------------------------


def test_verify_only_after_apply_exits_zero(apply_env, capsys):
    assert _apply(apply_env) == 0
    capsys.readouterr()

    rc = _verify_only(apply_env)
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["discrepancies"] == []


def test_verify_only_missing_table_exits_four(apply_env, capsys):
    assert _apply(apply_env) == 0
    with sqlite3.connect(apply_env["central_qi"]) as c:
        c.execute("DROP TABLE pattern_usage")
        c.commit()
    capsys.readouterr()

    rc = _verify_only(apply_env)

    assert rc == 4
    report = json.loads(capsys.readouterr().out)
    assert any(
        err.get("phase") == "central_table_missing" and err.get("table") == "pattern_usage"
        for err in report.get("read_errors", [])
    )


# ---------------------------------------------------------------------------
# --test-apply (success and failure cleanup)
# ---------------------------------------------------------------------------


def test_test_apply_runs_in_temp_dir_and_cleans_up(apply_env, caplog, capsys):
    caplog.set_level(logging.INFO, logger="vnx.migrate.apply")
    live_qi_size = apply_env["central_qi"].stat().st_size

    rc = M.main([
        "--test-apply",
        "--registry", str(apply_env["registry"]),
        "--backup-base", str(apply_env["backup_base"]),
        "--central-state", str(apply_env["central_state"]),
    ])
    out = capsys.readouterr().out
    tmp_dir = _test_apply_tmp_dir_from_log(caplog)

    assert rc == 0
    assert "TEST MODE -- no writes to live central DB at" in out
    assert "TEST MODE complete. Temp central dir will be cleaned up." in out
    assert f"TEST MODE -- temp central dir cleaned up: {tmp_dir}" in out
    assert not tmp_dir.exists()
    assert apply_env["central_qi"].stat().st_size == live_qi_size
    assert list(apply_env["backup_base"].iterdir()) == []


def test_test_apply_failure_still_cleans_up_temp_dir(apply_env, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="vnx.migrate.apply")

    def _boom(qi_db, rc_db):
        raise sqlite3.Error("simulated bootstrap failure")

    monkeypatch.setattr(M, "_init_central_if_missing", _boom)

    rc = M.main([
        "--test-apply",
        "--registry", str(apply_env["registry"]),
        "--backup-base", str(apply_env["backup_base"]),
        "--central-state", str(apply_env["central_state"]),
    ])
    tmp_dir = _test_apply_tmp_dir_from_log(caplog)

    assert rc == 3
    assert not tmp_dir.exists(), "test-apply temp dir must be removed on the failure path"


# ---------------------------------------------------------------------------
# Fresh central gate (with and without --fresh-central)
# ---------------------------------------------------------------------------


def test_fresh_central_without_flag_exits_one(apply_env, caplog):
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")
    _drop_central_dbs(apply_env)

    rc = _apply(apply_env)

    assert rc == 1
    assert "--fresh-central" in " ".join(record.getMessage() for record in caplog.records)
    assert not apply_env["central_qi"].exists() or "success_patterns" not in _table_names(
        apply_env["central_qi"]
    )


def test_fresh_central_with_flag_exits_zero(apply_env):
    _drop_central_dbs(apply_env)

    rc = _apply(apply_env, "--fresh-central")

    assert rc == 0
    assert "success_patterns" in _table_names(apply_env["central_qi"])
    assert "dispatches" in _table_names(apply_env["central_rc"])
    assert _row_count(apply_env["central_qi"], "success_patterns") == 4
    assert apply_env["central_qi"].stat().st_size > 0


# ---------------------------------------------------------------------------
# Partial QI DB (OI-011) - no --fresh-central needed
# ---------------------------------------------------------------------------


def test_partial_qi_db_applies_without_fresh_flag(apply_env):
    _drop_central_dbs(apply_env)
    _make_partial_qi_db(apply_env["central_qi"], rows=3)

    rc = _apply(apply_env)

    assert rc == 0
    assert _row_count(apply_env["central_qi"], "dispatch_experiments") >= 3
    assert "success_patterns" in _table_names(apply_env["central_qi"])
    assert _row_count(apply_env["central_qi"], "success_patterns") == 4


# ---------------------------------------------------------------------------
# Exit 2: mutually exclusive modes, exit 3: bootstrap and backup failures
# ---------------------------------------------------------------------------


def test_test_apply_and_verify_only_are_mutually_exclusive(apply_env):
    rc = M.main([
        "--test-apply",
        "--verify-only",
        "--registry", str(apply_env["registry"]),
        "--central-state", str(apply_env["central_state"]),
    ])

    assert rc == 2


def test_fresh_central_bootstrap_failure_exits_three(apply_env, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")
    _drop_central_dbs(apply_env)

    def _boom(qi_db, rc_db):
        raise sqlite3.Error("simulated bootstrap failure")

    monkeypatch.setattr(M, "_init_central_if_missing", _boom)

    rc = _apply(apply_env, "--fresh-central")

    assert rc == 3
    assert "schema migration failed: simulated bootstrap failure" in " ".join(
        record.getMessage() for record in caplog.records
    )
    # This run created the stubs; a failed bootstrap must not leave 0-table decoys.
    assert not apply_env["central_qi"].exists()
    assert not apply_env["central_rc"].exists()


def test_backup_failure_exits_three(apply_env, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")

    def _boom(projects, backup_base):
        raise M.BackupFailure("simulated backup failure")

    monkeypatch.setattr(M, "backup_projects", _boom)

    rc = _apply(apply_env)

    assert rc == 3
    assert "backup phase failed: simulated backup failure" in " ".join(
        record.getMessage() for record in caplog.records
    )
    assert _row_count(apply_env["central_qi"], "success_patterns") == 0


# ---------------------------------------------------------------------------
# Exit 4: verification mismatch after a partial import
# ---------------------------------------------------------------------------


def test_partial_import_failure_exits_four(apply_env, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")
    real_import = M._import_table

    def _flaky_import(con, alias, project, table):
        if project.project_id == "proj-b" and table == "pattern_usage":
            raise sqlite3.IntegrityError("synthetic project failure")
        return real_import(con, alias, project, table)

    monkeypatch.setattr(M, "_import_table", _flaky_import)

    rc = _apply(apply_env)

    assert rc == 4
    assert "verification failed:" in " ".join(record.getMessage() for record in caplog.records)
    # Snapshot restore wiped the failed run: the live central has no imported rows.
    assert _row_count(apply_env["central_qi"], "success_patterns") == 0
