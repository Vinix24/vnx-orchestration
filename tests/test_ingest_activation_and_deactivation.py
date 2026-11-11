"""Deactivating a pattern activation is one command, and it stays off (loop D9, split 1: the deactivation)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pattern_activation as pa
import prevention_rule_store as store
import quality_db_init as qdi

SCRIPT = REPO_ROOT / "scripts" / "deactivate_pattern_activation.py"
PID = "tproj"
# The live definition, read with mode=ro from the vnx-dev store on 2026-11-11: project_id NOT NULL, no default.
LIVE_PREVENTION_RULES = (
    "CREATE TABLE prevention_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, tag_combination TEXT NOT NULL, "
    "rule_type TEXT NOT NULL, description TEXT NOT NULL, recommendation TEXT NOT NULL, confidence REAL DEFAULT (0.0), "
    "created_at TEXT NOT NULL, triggered_count INTEGER DEFAULT (0), last_triggered TEXT, source_dispatch_id TEXT "
    "DEFAULT (NULL), valid_from DATETIME DEFAULT (NULL), valid_until DATETIME DEFAULT (NULL), source TEXT, "
    "project_id TEXT NOT NULL)")
PROPOSAL = {"pattern": "Error pattern: deadline exceeded", "terminal_constraint": "T1", "failure_class": "timeout",
            "prevention": "split the task", "confidence": 0.8, "occurrence_count": 3,
            "source_dispatch_ids": ["d-1", "d-2", "d-3"]}
RID = store.rule_id(PROPOSAL)


def _row(conn, description: str, source: str) -> int:
    return conn.execute("INSERT INTO prevention_rules (tag_combination, rule_type, description, recommendation, "
                        "created_at, source, project_id) VALUES ('T1', 'r', ?, 'rec', 'now', ?, ?)",
                        (description, source, PID)).lastrowid


def _approve(state: Path, now: str = "2026-11-11T09:00:00+00:00") -> dict:
    (record,) = store.decide(state, [RID], "approved", reason="recurring", decided_by="vincent", project_id=PID,
                             now=now, effect_criterion="timeouts below 1 a week", stop_condition="a new class")
    return record


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A v35 store with the live prevention_rules definition, one legacy row and one active loop activation."""
    state = tmp_path / "state"
    state.mkdir()
    db = state / "quality_intelligence.db"
    assert qdi.bootstrap_qi_db(db) is True
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE prevention_rules")
    conn.execute(LIVE_PREVENTION_RULES)
    legacy, loop_row = _row(conn, "legacy", "behavior_analysis"), _row(conn, PROPOSAL["pattern"], pa.LOOP_SOURCE)
    store.queue_rules(state, [PROPOSAL], project_id=PID, now="2026-11-11T08:00:00+00:00")
    act = pa.record_activation(conn, _approve(state), state_dir=state, rule_row_id=loop_row,
                               now="2026-11-11T09:30:00+00:00")
    conn.commit()
    yield conn, state, legacy, pa.activation_for(conn, PID, act)
    conn.close()


def _cli(state: Path, *args: str, project: str = PID) -> subprocess.CompletedProcess:
    env = {**os.environ, "VNX_PROJECT_ID": project}
    return subprocess.run([sys.executable, str(SCRIPT), *args, "--state-dir", str(state)],
                          capture_output=True, text=True, env=env, timeout=60)


def _active(conn) -> list:
    return conn.execute("SELECT activation_id, active, deactivation_reason, deactivation_approval_id "
                        "FROM pattern_activation ORDER BY activated_at").fetchall()


def _off(state: Path) -> list:
    return [r for r in store.decisions(state) if r["decision"] == "deactivated"]


def test_the_recovery_path_switches_off_once_and_records_the_decision(env):
    conn, state, _, act = env
    assert store.approved_decision(state, RID)["recovery_path"] == store.RECOVERY_PATH
    first = _cli(state, "--activation-id", act["activation_id"], "--reason", "effect not reached")
    assert first.returncode == 0, first.stderr
    (record,) = _off(state)
    assert record["approval_id"].startswith("apr-") and record["activation_id"] == act["activation_id"]
    assert _active(conn) == [(act["activation_id"], 0, "effect not reached", record["approval_id"])]
    assert [r["status"] for r in store.load_queue(state)] == ["deactivated"]
    again = _cli(state, "--activation-id", act["activation_id"], "--reason", "again")
    assert again.returncode == 0 and "unchanged" in again.stdout and len(_off(state)) == 1


@pytest.mark.parametrize("make, code, text", [
    (lambda act, legacy: (act, "  "), 2, "must not be empty"),
    (lambda act, legacy: ("act-unknown", "r"), 1, "is no activation"),
    (lambda act, legacy: (str(legacy), "r"), 1, "legacy rule"),
])
def test_an_empty_reason_an_unknown_id_and_a_legacy_rule_are_refused(env, make, code, text):
    conn, state, legacy, act = env
    activation_id, reason = make(act["activation_id"], legacy)
    result = _cli(state, "--activation-id", activation_id, "--reason", reason)
    assert (result.returncode, text in result.stderr) == (code, True), result.stderr
    assert [a[1] for a in _active(conn)] == [1] and _off(state) == []


def test_an_activation_of_another_project_or_a_missing_store_is_exit_1(env, tmp_path):
    conn, state, _, act = env
    assert _cli(state, "--activation-id", act["activation_id"], "--reason", "r", project="other").returncode == 1
    assert _cli(tmp_path / "nowhere", "--activation-id", act["activation_id"], "--reason", "r").returncode == 1
    assert [a[1] for a in _active(conn)] == [1] and _off(state) == []


def test_a_record_written_before_a_crash_is_finished_by_a_rerun_without_a_second_record(env):
    conn, state, _, act = env
    store.record_deactivation(state, act, reason="first", decided_by="v", now="2026-11-11T10:00:00+00:00")
    assert [a[1] for a in _active(conn)] == [1]  # the crash: the record is there, the row is still on
    assert _cli(state, "--activation-id", act["activation_id"], "--reason", "rerun").returncode == 0
    assert [(r["reason"], a[2]) for r, a in zip(_off(state), _active(conn))] == [("first", "first")]


def test_it_stays_off_until_a_new_approval_gives_a_new_activation(env):
    conn, state, _, act = env
    old = store.approved_decision(state, RID)
    assert _cli(state, "--activation-id", act["activation_id"], "--reason", "r").returncode == 0
    assert store.approved_decision(state, RID) is None
    with pytest.raises(ValueError):  # the old approval is no longer the latest record
        pa.record_activation(conn, old, state_dir=state, rule_row_id=act["rule_row_id"], now="2026-11-12T08:00:00Z")
    fresh = pa.record_activation(conn, _approve(state, now="2026-11-12T09:00:00+00:00"), state_dir=state,
                                 rule_row_id=act["rule_row_id"], now="2026-11-12T09:30:00+00:00")
    conn.commit()
    assert fresh != act["activation_id"] and [a[1] for a in _active(conn)] == [0, 1]
    with pytest.raises(store.DecisionRefused):
        store.record_deactivation(state, act, reason="r", decided_by="v", now="2026-11-12T10:00:00+00:00")


def test_deactivate_switches_off_once_and_only_from_a_store_deactivation(env):
    conn, state, _, act = env
    with pytest.raises(ValueError):  # no deactivation in the record: a dict in hand switches nothing off
        pa.deactivate(conn, state, act["activation_id"], project_id=PID)
    with pytest.raises(LookupError):
        pa.deactivate(conn, state, act["activation_id"], project_id="other")
    store.record_deactivation(state, act, reason="r", decided_by="v", now="2026-11-11T10:00:00+00:00")
    assert pa.deactivate(conn, state, act["activation_id"], project_id=PID) is True
    assert pa.deactivate(conn, state, act["activation_id"], project_id=PID) is False
    assert [a[1:3] for a in _active(conn)] == [(0, "r")]


@pytest.mark.parametrize("change", [{"approval_id": "apr-handmade"}, {"activation_id": "act-other"},
                                    {"deactivates_approval_id": "apr-other"}, {"reason": " "}])
def test_a_deactivation_line_the_store_did_not_write_for_this_activation_switches_nothing_off(env, change):
    conn, state, _, act = env
    line = {"decision": "deactivated", "project_id": PID, "rule_id": RID, "decided_at": "t", "decided_by": "v",
            "reason": "r", "activation_id": act["activation_id"], "deactivates_approval_id": act["approval_id"],
            "approval_id": store.mint_approval_id(PID, RID, "deactivated", "t", "v"), **change}
    with open(state / store.DECISIONS_FILENAME, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    with pytest.raises(ValueError):
        pa.deactivate(conn, state, act["activation_id"], project_id=PID)
    assert [a[1] for a in _active(conn)] == [1]


@pytest.mark.parametrize("kwargs", [{"reason": ""}, {"decided_by": " "}, {"now": None}])
def test_record_deactivation_refuses_an_empty_field_and_writes_nothing(env, kwargs):
    conn, state, _, act = env
    with pytest.raises(ValueError):
        store.record_deactivation(state, act, **{"reason": "r", "decided_by": "v", "now": "t", **kwargs})
    assert _off(state) == [] and [r["status"] for r in store.load_queue(state)] == ["approved"]
