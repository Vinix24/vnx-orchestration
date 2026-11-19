"""Ochtendronde sweep: one learning beacon and one dream run per registered project."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import ochtendronde_sweep as sw
from learning_loop_beacon import read_beacon, write_beacon, beacon_path


def _register(tmp: Path, ids) -> Path:
    path = tmp / "projects.json"
    path.write_text(json.dumps({"schema_version": 1, "projects": [
        {"name": f"n-{i}", "path": str(tmp / "co" / i), "project_id": i} for i in ids]}))
    return path


def _store(home: Path, pid: str, db: bool = True) -> Path:
    store = home / pid
    (store / "state").mkdir(parents=True)
    if db:
        (store / "state" / sw.DB_NAME).write_bytes(b"")
    return store


def _learning_ok(seen):
    def run(pid, entry, store):
        seen.append((pid, store))
        write_beacon(store / "state", {"project_id": pid, "started_at": sw.utc_now_iso(),
                                       "status": "ok", "reason": None})
    return run


def _dream_ok(seen):
    def run(pid, db_path, store):
        seen.append((pid, db_path, store))
        return {"cycle_id": "c-" + pid, "review_path": str(store / "r.json")}
    return run


def _run(tmp, ids, learning, dream):
    home = tmp / "home"
    return sw.sweep(_register(tmp, ids), store_resolver=lambda p: home / p,
                    learning_runner=learning, dream_runner=dream)


def test_two_healthy_projects_each_get_own_beacon(tmp_path):
    home = tmp_path / "home"
    _store(home, "a")
    _store(home, "b")
    lseen, dseen = [], []
    results = _run(tmp_path, ["a", "b"], _learning_ok(lseen), _dream_ok(dseen))
    assert [r.failed for r in results] == [False, False]
    for pid in ("a", "b"):
        beacon = read_beacon(home / pid / "state")
        assert beacon["project_id"] == pid
        assert not (home / pid / "state" / "x").exists()
    assert {p for p, _ in lseen} == {"a", "b"}
    assert all(db.parent.parent == store and store.name == pid for pid, db, store in dseen)
    assert [r.dream["status"] for r in results] == ["completed", "completed"]


def test_failures_are_isolated_and_named(tmp_path):
    home = tmp_path / "home"
    _store(home, "good")
    _store(home, "boom")
    lseen = []

    def learning(pid, entry, store):
        if pid == "boom":
            raise RuntimeError("db locked")
        _learning_ok(lseen)(pid, entry, store)

    results = _run(tmp_path, ["gone", "boom", "good"], learning, _dream_ok([]))
    by = {r.project_id: r for r in results}
    assert by["good"].learning["status"] == "ok" and not by["good"].failed
    assert by["gone"].learning == {"status": "skipped", "reason": "store_absent"}
    assert read_beacon(home / "gone" / "state")["status"] == "skipped"
    boom = read_beacon(home / "boom" / "state")
    assert boom["status"] == "failed" and "db locked" in boom["reason"]
    assert by["boom"].failed


def test_main_exit_code_and_last_line_name_failed(tmp_path, capsys):
    home = tmp_path / "home"
    _store(home, "ok-one")
    _store(home, "nodb", db=False)
    reg = _register(tmp_path, ["ok-one", "nodb", "missing"])
    orig = (sw.run_learning_child, sw.run_dream_in_process)
    sw.run_learning_child, sw.run_dream_in_process = _learning_ok([]), _dream_ok([])
    try:
        code = sw.main(["--registry", str(reg), "--data-home", str(home)])
    finally:
        sw.run_learning_child, sw.run_dream_in_process = orig
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 1
    assert out[-1] == "FAILED: nodb"  # a missing store is skipped, a missing db is failed
    assert any(line.startswith("project=missing learning=skipped") for line in out)
    assert read_beacon(home / "nodb" / "state")["reason"] == "db_missing"


def test_dream_exception_is_failed_and_sweep_continues(tmp_path):
    home = tmp_path / "home"
    _store(home, "a")
    _store(home, "b")

    def dream(pid, db, store):
        if pid == "a":
            raise sqlite_error()
        return {"status": "skipped", "reason": "unchanged_input"}

    results = _run(tmp_path, ["a", "b"], _learning_ok([]), dream)
    assert results[0].dream["status"] == "failed" and "locked" in results[0].dream["reason"]
    assert results[1].dream == {"status": "skipped", "reason": "unchanged_input"}


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"projects": []}), "[]"])
def test_empty_or_unreadable_register_exits_nonzero_and_writes_nothing(tmp_path, content, capsys):
    reg = tmp_path / "projects.json"
    if content is not None:
        reg.write_text(content)
    home = tmp_path / "home"
    code = sw.main(["--registry", str(reg), "--data-home", str(home)])
    assert code == 2
    assert "register empty or unreadable" in capsys.readouterr().out
    assert not home.exists()


def test_project_id_comes_from_register_not_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "someone-else")
    home = tmp_path / "home"
    _store(home, "real")
    lseen, dseen = [], []
    _run(tmp_path, ["real"], _learning_ok(lseen), _dream_ok(dseen))
    assert lseen[0][0] == "real" and dseen[0][0] == "real"
    assert read_beacon(home / "real" / "state")["project_id"] == "real"


def test_foreign_or_stale_beacon_is_failed(tmp_path):
    home = tmp_path / "home"
    _store(home, "a")

    def foreign(pid, entry, store):
        write_beacon(store / "state", {"project_id": "b", "started_at": sw.utc_now_iso(), "status": "ok"})

    def silent(pid, entry, store):
        return None

    assert _run(tmp_path, ["a"], foreign, _dream_ok([]))[0].learning["reason"].startswith("beacon_project_mismatch")
    beacon_path(home / "a" / "state").unlink()
    write_beacon(home / "a" / "state", {"project_id": "a", "started_at": "2000-01-01T00:00:00+00:00", "status": "ok"})
    assert _run(tmp_path, ["a"], silent, _dream_ok([]))[0].learning["reason"] == "beacon_stale"
    beacon_path(home / "a" / "state").unlink()
    assert _run(tmp_path, ["a"], silent, _dream_ok([]))[0].learning["reason"] == "beacon_missing_or_unreadable"


@pytest.mark.parametrize("result", [None, {}, {"status": "weird"}, {"status": "timeout", "reason": "kimi_timeout"}])
def test_unknown_dream_result_is_failed(result):
    assert sw._dream_outcome(result)["status"] == "failed"


@pytest.mark.parametrize("entry", [{"name": "x"}, {"project_id": "../etc"}, {"project_id": 7}, {"project_id": ""}])
def test_unusable_register_entry_is_listed_failed(tmp_path, entry):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [entry]}))
    results = sw.sweep(reg, store_resolver=lambda p: tmp_path / "never" / p)
    assert results[0].failed and not (tmp_path / "never").exists()


def test_project_env_drops_sweep_project_and_sets_explicit(tmp_path):
    base = {"VNX_PROJECT_ID": "other", "VNX_STATE_DIR": "/x", "PROJECT_ROOT": "/y", "PATH": "/bin"}
    env = sw.project_env(base, "p1", tmp_path / "p1")
    assert env["VNX_PROJECT_ID"] == "p1" and env["VNX_DATA_DIR_EXPLICIT"] == "1"
    assert env["VNX_STATE_DIR"] == str(tmp_path / "p1" / "state")
    assert "PROJECT_ROOT" not in env and env["PATH"] == "/bin"


def test_learning_child_runs_with_project_env_and_cwd(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setenv("VNX_PROJECT_ID", "other")
    checkout = tmp_path / "co"
    checkout.mkdir()
    sw.run_learning_child("p1", {"path": str(checkout)}, tmp_path / "p1")
    cmd, kw = calls[0]
    assert cmd[1].endswith("learning_loop_nightly.py")
    assert kw["env"]["VNX_PROJECT_ID"] == "p1" and kw["cwd"] == str(checkout)
    assert kw["timeout"] == sw.LEARNING_TIMEOUT_SECONDS
