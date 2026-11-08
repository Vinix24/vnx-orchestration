"""The direct creation paths write the delivery identity (ADR-056, zelfst PR-8a): real CLI, real functions, tmp stores."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dispatch_register
import planning_cli
import state_writer
import tracks as tracks_lib
from delivery_identity import delivery_identity_fields

from fixtures.live_store_fixture import build_live_store

PID, TRACK = "p-test", "feat-source"


# --- the helper --------------------------------------------------------------

def test_helper_plain_id():
    assert delivery_identity_fields("dlv-abc", "p1") == {"project_id": "p1", "delivery_id": "dlv-abc"}


def test_helper_round_id_gives_the_base():
    assert delivery_identity_fields("dlv-abc-r3", "p1")["delivery_id"] == "dlv-abc"


@pytest.mark.parametrize("project", [None, "", "   ", 7, ["x"], {"a": 1}])
def test_helper_bad_project_is_unknown(project):
    assert delivery_identity_fields("dlv-abc", project)["project_id"] == "unknown"


@pytest.mark.parametrize("dispatch_id", [None, "", "  ", 5])
def test_helper_bad_dispatch_id_is_unknown(dispatch_id):
    assert delivery_identity_fields(dispatch_id, "p1")["delivery_id"] == "unknown"


# --- deliverable add ---------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    build_live_store(state_dir)
    tracks_lib.create_track(state_dir, TRACK, PID, TRACK, "ship it", phase="queued",
                            metadata_json=json.dumps({"plan_scope": "light"}))
    return state_dir


def _add(state_dir: Path) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return planning_cli.main(["deliverable", "add", "--objective", TRACK, "--output-kind", "pr",
                                  "--title", "t", "--project-id", PID, "--state-dir", str(state_dir)])


def _rows(state_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_add_writes_project(store):
    assert _add(store) == 0
    dispatch_id = _rows(store, "SELECT dispatch_id FROM dispatches")[0]["dispatch_id"]
    event = _rows(store, "SELECT * FROM coordination_events WHERE event_type='deliverable_created'")[0]
    meta = json.loads(event["metadata_json"])
    assert meta.get("delivery_id") == dispatch_id
    assert meta.get("project_id") == PID
    assert meta.get("actor_class") in {"operator", "t0", "worker", "automation", "unknown"}
    assert event["actor"] == "operator"
    assert _rows(store, "SELECT project_id FROM dispatches")[0]["project_id"] == PID


# --- proposed track dispatch -------------------------------------------------

def _central_state(tmp_path: Path, monkeypatch, project: str) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # HOME is a tmp dir here, so the guard would call this fake "~/.vnx-data" the real central store.
    monkeypatch.setattr(state_writer, "refuse_real_central_store_write_under_test_runner", lambda _path: None)
    state_dir = tmp_path / ".vnx-data" / project / "state"
    build_live_store(state_dir)
    return state_dir


def _ndjson(state_dir: Path) -> list[dict]:
    path = state_dir.parent / "events" / "dispatch_register.ndjson"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_proposed_track_writes_project(tmp_path, monkeypatch):
    state_dir = _central_state(tmp_path, monkeypatch, "seocrawler")
    dispatch_register.register_proposed_track_dispatch(state_dir, "dlv-x-r2", "T1", "trk", "PR-1")
    row = _rows(state_dir, "SELECT * FROM dispatches WHERE dispatch_id='dlv-x-r2'")[0]
    assert row["project_id"] == "seocrawler"
    record = _ndjson(state_dir)[-1]
    assert record["project_id"] == "seocrawler"
    assert record["delivery_id"] == "dlv-x"


def test_proposed_track_explicit_project_is_kept(tmp_path, monkeypatch):
    state_dir = _central_state(tmp_path, monkeypatch, "seocrawler")
    dispatch_register.register_proposed_track_dispatch(state_dir, "dlv-y", "T1", "trk", "PR-1", project_id="other")
    assert _rows(state_dir, "SELECT project_id FROM dispatches")[0]["project_id"] == "other"
    assert _ndjson(state_dir)[-1]["project_id"] == "other"


@pytest.mark.parametrize("project", [None, "", "  "])
def test_proposed_track_unresolvable_store_is_unknown(tmp_path, monkeypatch, project):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state_dir = tmp_path / "loose" / "state"
    build_live_store(state_dir)
    dispatch_register.register_proposed_track_dispatch(state_dir, "dlv-z", "T1", "trk", "PR-1", project_id=project)
    assert _rows(state_dir, "SELECT project_id FROM dispatches")[0]["project_id"] == "unknown"
    assert _ndjson(state_dir)[-1]["project_id"] == "unknown"


def test_proposed_track_ndjson_precedes_a_failed_db_insert(tmp_path, monkeypatch):
    state_dir = _central_state(tmp_path, monkeypatch, "seocrawler")
    dispatch_register.register_proposed_track_dispatch(state_dir, "dlv-dup", "T1", "trk", "PR-1")
    with pytest.raises(sqlite3.Error):
        dispatch_register.register_proposed_track_dispatch(state_dir, "dlv-dup", "T1", "trk", "PR-1")
    assert [r["dispatch_id"] for r in _ndjson(state_dir)] == ["dlv-dup", "dlv-dup"]
    assert len(_rows(state_dir, "SELECT 1 FROM dispatches WHERE dispatch_id='dlv-dup'")) == 1
