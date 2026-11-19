"""Ochtendronde sweep: one learning beacon and one dream run per registered project."""
from __future__ import annotations

import json
import sqlite3
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


def test_two_healthy_projects_each_get_own_beacon(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "someone-else")
    home = tmp_path / "home"
    _store(home, "a")
    _store(home, "b")
    lseen, dseen = [], []
    results = _run(tmp_path, ["a", "b"], _learning_ok(lseen), _dream_ok(dseen))
    assert [read_beacon(home / pid / "state")["project_id"] for pid in ("a", "b")] == ["a", "b"]
    assert {p for p, _ in lseen} == {"a", "b"}
    assert all(db.parent.parent == store and store.name == pid for pid, db, store in dseen)
    assert [(r.failed, r.dream["status"]) for r in results] == [(False, "completed")] * 2


def test_failures_are_isolated_and_named(tmp_path):
    home = tmp_path / "home"
    _store(home, "good")
    _store(home, "boom")

    def learning(pid, entry, store):
        if pid == "boom":
            raise RuntimeError("db locked")
        _learning_ok([])(pid, entry, store)

    results = _run(tmp_path, ["gone", "boom", "good"], learning, _dream_ok([]))
    by = {r.project_id: r for r in results}
    assert by["good"].learning["status"] == "ok" and not by["good"].failed
    assert by["gone"].learning == {"status": "skipped", "reason": "store_absent"}
    assert read_beacon(home / "gone" / "state")["status"] == "skipped"
    boom = read_beacon(home / "boom" / "state")
    assert boom["status"] == "failed" and "db locked" in boom["reason"] and by["boom"].failed


def test_main_exit_code_and_last_line_name_failed(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    _store(home, "ok-one")
    _store(home, "nodb", db=False)
    reg = _register(tmp_path, ["ok-one", "nodb", "missing"])
    monkeypatch.setattr(sw, "run_learning_child", _learning_ok([]))
    monkeypatch.setattr(sw, "run_dream_in_process", _dream_ok([]))
    code = sw.main(["--registry", str(reg), "--data-home", str(home)])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 1
    assert out[-1] == "FAILED: nodb"  # a missing store is skipped, a missing db is failed
    assert any(line.startswith("project=missing learning=skipped") for line in out)
    assert read_beacon(home / "nodb" / "state")["reason"] == "db_missing"


def test_dream_exception_is_failed_and_sweep_continues(tmp_path):
    _store(tmp_path / "home", "a")
    _store(tmp_path / "home", "b")
    dream = lambda pid, db, store: (_raise(sqlite_error()) if pid == "a"
                                    else {"status": "skipped", "reason": "unchanged_input"})
    results = _run(tmp_path, ["a", "b"], _learning_ok([]), dream)
    assert results[0].dream["status"] == "failed" and "locked" in results[0].dream["reason"]
    assert results[1].dream == {"status": "skipped", "reason": "unchanged_input"}


def _raise(exc):
    raise exc


def sqlite_error():
    return sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"projects": []}), "[]"])
def test_empty_or_unreadable_register_exits_nonzero_and_writes_nothing(tmp_path, content, capsys):
    reg = tmp_path / "projects.json"
    if content is not None:
        reg.write_text(content)
    assert sw.main(["--registry", str(reg), "--data-home", str(tmp_path / "home")]) == 2
    assert "register empty or unreadable" in capsys.readouterr().out
    assert not (tmp_path / "home").exists()


def test_foreign_or_stale_beacon_is_failed(tmp_path):
    home = tmp_path / "home"
    _store(home, "a")

    def foreign(pid, entry, store):
        write_beacon(store / "state", {"project_id": "b", "started_at": sw.utc_now_iso(), "status": "ok"})

    assert _run(tmp_path, ["a"], foreign, _dream_ok([]))[0].learning["reason"].startswith("beacon_project_mismatch")
    beacon_path(home / "a" / "state").unlink()
    write_beacon(home / "a" / "state", {"project_id": "a", "started_at": "2000-01-01T00:00:00+00:00", "status": "ok"})
    assert _run(tmp_path, ["a"], lambda *a: None, _dream_ok([]))[0].learning["reason"] == "beacon_stale"
    beacon_path(home / "a" / "state").unlink()
    assert _run(tmp_path, ["a"], lambda *a: None, _dream_ok([]))[0].learning["reason"] == "beacon_missing_or_unreadable"


@pytest.mark.parametrize("result", [None, {}, {"status": "weird"}, {"status": "timeout", "reason": "kimi_timeout"}])
def test_unknown_dream_result_is_failed(result):
    assert sw._dream_outcome(result)["status"] == "failed"


@pytest.mark.parametrize("entry", [{"name": "x"}, {"project_id": "../etc"}, {"project_id": 7}, {"project_id": ""}])
def test_unusable_register_entry_is_listed_failed(tmp_path, entry):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [entry]}))
    results = sw.sweep(reg, store_resolver=lambda p: tmp_path / "never" / p)
    assert results[0].failed and not (tmp_path / "never").exists()


def test_learning_child_runs_with_project_env_and_cwd(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setenv("VNX_PROJECT_ID", "other")
    checkout = tmp_path / "co"; checkout.mkdir()
    sw.run_learning_child("p1", {"path": str(checkout)}, tmp_path / "p1")
    cmd, kw = calls[0]
    assert kw["env"]["VNX_PROJECT_ID"] == "p1" and kw["cwd"] == str(checkout) and "PROJECT_ROOT" not in kw["env"]
    assert kw["timeout"] == sw.LEARNING_TIMEOUT_SECONDS


def test_failed_dream_event_write_is_reported(tmp_path, monkeypatch):
    _store(tmp_path / "home", "a")
    monkeypatch.setattr("dream.consolidator._emit_dream_event", lambda event, store: _raise(OSError("disk full")))
    res = _run(tmp_path, ["a"], _learning_ok([]), lambda pid, db, store: _raise(sqlite_error()))[0]
    assert res.dream["status"] == "failed" and "locked" in res.dream["reason"]
    assert all("dream_event_error: OSError: disk full" in t for t in (res.line(), json.dumps(res.as_dict())))


def _child(monkeypatch, tmp_path, code, stderr, beacon=True):
    def fake(cmd, **kw):
        if beacon:
            write_beacon(tmp_path / "state", {"project_id": "p1", "started_at": sw.utc_now_iso(),
                                              "status": "ok"})
        return subprocess.CompletedProcess(cmd, code, "", stderr)
    monkeypatch.setattr(subprocess, "run", fake)
    _store(tmp_path.parent, tmp_path.name)
    return sw._sweep_one("p1", {}, tmp_path, sw.run_learning_child, _dream_ok([])).learning


def test_child_nonzero_exit_with_fresh_beacon_is_failed(tmp_path, monkeypatch):
    out = _child(monkeypatch, tmp_path, 1, "x" * 900 + "boom")
    assert out["status"] == "failed" and "exit_code=1" in out["reason"]
    assert out["reason"].endswith("boom") and len(out["reason"]) < 400


@pytest.mark.parametrize("beacon,status", [(True, "ok"), (False, "failed")])
def test_child_exit_zero_is_ok_only_with_a_fresh_beacon(tmp_path, monkeypatch, beacon, status):
    assert _child(monkeypatch, tmp_path, 0, "", beacon)["status"] == status
