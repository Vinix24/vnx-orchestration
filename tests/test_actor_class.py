"""tests/test_actor_class.py - ADR-056 actor class: derivation and the promote/close events."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import actor_class  # noqa: E402

_REAL_STDIN_PROBE = actor_class._stdin_isatty
import planning_cli  # noqa: E402

PID = "vnx-dev"
_MARKER_VARS = (
    "CLAUDE_ROLE", "VNX_TERMINAL", "VNX_TERMINAL_ID", "VNX_DISPATCH_ID",
    "VNX_CURRENT_DISPATCH_ID", "VNX_PROCESS_OWNER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _MARKER_VARS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("env,tty,expected", [
    ({}, True, "operator"),
    ({"CLAUDE_ROLE": "orchestrator"}, True, "t0"),
    ({"CLAUDE_ROLE": "orchestrator"}, False, "t0"),
    ({"VNX_TERMINAL": "T0"}, True, "t0"),
    ({"VNX_TERMINAL_ID": "T0", "VNX_DISPATCH_ID": "d-1"}, False, "t0"),
    ({"CLAUDE_ROLE": "worker", "VNX_DISPATCH_ID": "d-1"}, False, "worker"),
    ({"VNX_CURRENT_DISPATCH_ID": "d-1"}, True, "worker"),
    ({"VNX_TERMINAL": "T1", "VNX_DISPATCH_ID": "d-1"}, False, "worker"),
    ({"VNX_PROCESS_OWNER": "daemon"}, False, "automation"),
    ({"VNX_PROCESS_OWNER": "reconciler"}, False, "automation"),
    ({"VNX_PROCESS_OWNER": "scheduled"}, False, "automation"),
])
def test_derive_positive_classes(env, tty, expected):
    assert actor_class.derive(env=env, isatty=tty) == expected


@pytest.mark.parametrize("env,tty", [
    ({}, False),                                            # no signal at all
    ({"CLAUDE_ROLE": "worker"}, False),                     # agent without dispatch marker
    ({"CLAUDE_ROLE": "worker"}, True),                      # never operator
    ({"VNX_TERMINAL": "T2"}, True),
    ({"VNX_PROCESS_OWNER": ""}, False),
    ({"VNX_PROCESS_OWNER": "cron-of-someone"}, False),      # unknown owner value
    ({"VNX_PROCESS_OWNER": "daemon", "CLAUDE_ROLE": "worker"}, False),
    ({"CLAUDE_ROLE": "  ", "VNX_DISPATCH_ID": " "}, False), # blank markers are not markers
])
def test_derive_unknown_stays_unknown(env, tty):
    assert actor_class.derive(env=env, isatty=tty) == "unknown"


def test_derive_blank_marker_with_tty_is_operator():
    assert actor_class.derive(env={"CLAUDE_ROLE": " "}, isatty=True) == "operator"


def test_derive_only_returns_the_five_classes():
    for env in ({}, {"CLAUDE_ROLE": "x"}, {"VNX_DISPATCH_ID": "d"}):
        for tty in (True, False):
            assert actor_class.derive(env=env, isatty=tty) in actor_class.ACTOR_CLASSES
    assert len(actor_class.ACTOR_CLASSES) == 5


def test_derive_reads_process_env_and_probe_by_default(monkeypatch):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: True)
    assert actor_class.derive() == "operator"
    monkeypatch.setenv("CLAUDE_ROLE", "orchestrator")
    assert actor_class.derive() == "t0"


def test_stdin_probe_without_stdin_is_not_a_tty(monkeypatch):
    # The suite-wide operator default patches the probe; this test needs the real one.
    monkeypatch.setattr(actor_class, "_stdin_isatty", _REAL_STDIN_PROBE)
    monkeypatch.setattr(sys, "stdin", None)
    assert actor_class._stdin_isatty() is False


# --- promote and close write the class ---------------------------------------

def _store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL, "
        "project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL, pr_ref TEXT, "
        "updated_at TEXT, metadata_json TEXT DEFAULT '{}', UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, "
        "event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, to_state TEXT, "
        "actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    conn.close()
    return state_dir


def _seed(state_dir: Path, dispatch_id: str, state: str) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?,?,?)",
        (dispatch_id, PID, state),
    )
    conn.commit()
    conn.close()


def _event(state_dir: Path, event_type: str) -> dict:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM coordination_events WHERE event_type = ?", (event_type,)
    ).fetchone()
    conn.close()
    assert row is not None, f"no {event_type} event written"
    ev = dict(row)
    ev["metadata"] = json.loads(ev["metadata_json"])
    return ev


def _promote(state_dir: Path, did: str) -> int:
    return planning_cli.cmd_deliverable_promote(
        argparse.Namespace(dispatch_id=did, project_id=PID, state_dir=str(state_dir))
    )


def _close(state_dir: Path, did: str) -> int:
    return planning_cli.cmd_deliverable_close(argparse.Namespace(
        dispatch_id=did, project_id=PID, state_dir=str(state_dir), evidence="123", json=True,
    ))


def test_promote_writes_class_operator_on_a_tty(tmp_path, monkeypatch):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: True)
    state_dir = _store(tmp_path)
    _seed(state_dir, "dlv-1", "proposed")
    assert _promote(state_dir, "dlv-1") == 0
    ev = _event(state_dir, "deliverable_promoted")
    assert ev["metadata"].get("actor_class") == "operator"
    assert ev["actor"] == "operator"


def test_promote_writes_class_t0_from_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: False)
    monkeypatch.setenv("CLAUDE_ROLE", "orchestrator")
    monkeypatch.setattr(
        planning_cli, "_promotion_grant",
        lambda project_id, track_id: planning_cli.plan_gate_enforcement.PromotionGrant.VERIFIED,
    )
    state_dir = _store(tmp_path)
    _seed(state_dir, "dlv-1", "proposed")
    monkeypatch.setattr(planning_cli.tracks_lib, "track_autonomous_promotion_eligible", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_track_blocks_promotion", lambda *a, **k: False)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("ALTER TABLE dispatches ADD COLUMN track TEXT")
    conn.execute("UPDATE dispatches SET track = 'trk'")
    conn.commit()
    conn.close()
    assert _promote(state_dir, "dlv-1") == 0
    assert _event(state_dir, "deliverable_promoted")["metadata"]["actor_class"] == "t0"


def test_promote_without_any_signal_is_refused_as_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: False)
    state_dir = _store(tmp_path)
    _seed(state_dir, "dlv-1", "proposed")
    assert _promote(state_dir, "dlv-1") == 1
    assert "'unknown'" in capsys.readouterr().err


def test_close_writes_class_operator_and_keeps_existing_metadata(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: True)
    state_dir = _store(tmp_path)
    _seed(state_dir, "dlv-2", "ready")
    assert _close(state_dir, "dlv-2") == 0
    ev = _event(state_dir, "deliverable_completed")
    assert ev["metadata"]["actor_class"] == "operator"
    assert ev["metadata"]["evidence_pr"] == 123
    assert ev["metadata"]["attestation"] == "operator"
    assert ev["actor"] == "operator"


def test_close_superseded_path_writes_class(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: False)
    monkeypatch.setenv("CLAUDE_ROLE", "orchestrator")
    state_dir = _store(tmp_path)
    _seed(state_dir, "dlv-3", "failed_delivery")
    _seed(state_dir, "dlv-3-r1", "completed")
    assert _close(state_dir, "dlv-3") == 0
    ev = _event(state_dir, planning_cli.SUPERSEDED_EVENT)
    assert ev["metadata"]["actor_class"] == "t0"
    assert ev["metadata"]["superseded_by"] == "dlv-3-r1"
