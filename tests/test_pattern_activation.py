"""pattern_activation (v35): an activation carries the decision record and a recovery path (loop D5)."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import prevention_rule_store as store  # noqa: E402
import quality_db_init as qdi  # noqa: E402

NOW = "2026-11-10T12:00:00+00:00"
LATER = "2026-11-11T09:00:00+00:00"
PID = "tproj"
ROW = 7


def _bootstrap(tmp_path: Path) -> Path:
    db = tmp_path / "qi.db"
    assert qdi.bootstrap_qi_db(db) is True
    return db


def _sql(db: Path, *statements: str):
    with closing(sqlite3.connect(db)) as conn:
        result = [conn.execute(s).fetchall() for s in statements]
        conn.commit()
    return result[-1]


def _back_to_v34(db: Path) -> None:
    _sql(db, "DROP TABLE IF EXISTS pattern_activation", "PRAGMA user_version = 34")


def test_bootstrap_stamps_v35_with_the_adr007_key(tmp_path):
    db = _bootstrap(tmp_path)
    assert _sql(db, "PRAGMA user_version") == [(qdi.HIGHEST_QI_VERSION,)]
    info = _sql(db, "PRAGMA table_info(pattern_activation)")
    assert [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])] == ["project_id", "activation_id"]
    assert _sql(db, "PRAGMA index_info(idx_pattern_activation_rule_active)") != []


def test_a_v34_copy_migrates_to_v35_and_keeps_its_rows(tmp_path):
    db = _bootstrap(tmp_path)
    _back_to_v34(db)
    _sql(db, "INSERT INTO prevention_rules (tag_combination, rule_type, description, recommendation, created_at) "
             "VALUES ('t', 'r', 'd', 'rec', 'now')")
    assert qdi.bootstrap_qi_db(db) is True
    assert _sql(db, "PRAGMA user_version") == [(qdi.HIGHEST_QI_VERSION,)]
    assert _sql(db, "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_activation'") != []
    assert _sql(db, "SELECT count(*) FROM prevention_rules") == [(1,)]


def test_verify_database_structure_requires_pattern_activation(tmp_path, monkeypatch):
    db = _bootstrap(tmp_path)
    monkeypatch.setattr(qdi, "DB_PATH", db)
    assert qdi.verify_database_structure() is True
    _sql(db, "DROP TABLE IF EXISTS pattern_activation")
    assert qdi.verify_database_structure() is False


def test_a_pattern_activation_of_another_shape_is_refused_and_stays_v34(tmp_path):
    db = _bootstrap(tmp_path)
    _back_to_v34(db)
    _sql(db, "CREATE TABLE pattern_activation (project_id TEXT, activation_id TEXT)")
    assert qdi.bootstrap_qi_db(db) is False
    assert _sql(db, "PRAGMA user_version") == [(34,)]
    assert [r[1] for r in _sql(db, "PRAGMA table_info(pattern_activation)")] == ["project_id", "activation_id"]


@pytest.fixture
def pa():
    return importlib.import_module("pattern_activation")


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(_bootstrap(tmp_path))) as connection:
        yield connection


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path / "state"


def _decide(state: Path, decision: str = "approved", pid: str = PID, pattern: str = "p1") -> dict:
    proposal = {"pattern": pattern, "terminal_constraint": "T1", "failure_class": "timeout",
                "prevention": "prevent", "confidence": 0.8, "occurrence_count": 3,
                "source_dispatch_ids": ["d-1", "d-2"]}
    store.queue_rules(state, [proposal], project_id=pid, now=NOW)
    extra = {"effect_criterion": "timeouts drop below 2 a week", "stop_condition": "a worker is blocked twice"}
    (record,) = store.decide(state, [store.rule_id(proposal)], decision, reason="r", decided_by="v",
                             project_id=pid, now=NOW, **(extra if decision == "approved" else {}))
    return record


def _remint(record: dict, **changes) -> dict:
    out = {**record, **changes}
    keys = (out["project_id"], out["rule_id"], "approved", out["decided_at"], out["decided_by"])
    return {**out, "approval_id": store.mint_approval_id(*keys)}


def _append_line(state: Path, line: str) -> None:
    with open(state / store.DECISIONS_FILENAME, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _rows(conn) -> list:
    return conn.execute("SELECT * FROM pattern_activation").fetchall()


def test_a_store_decision_writes_one_row_and_a_repeat_hits_the_same_key(pa, conn, state):
    record = _decide(state)
    activation_id = pa.record_activation(conn, record, state_dir=state, rule_row_id=ROW, now=LATER)
    assert activation_id == pa.mint_activation_id(PID, record["approval_id"], record["rule_id"])
    row = pa.activation_for(conn, PID, activation_id)
    for name in ("rule_id", "decision_id", "approval_id", "bundle_ref", "rollout_scope", "baseline_metric",
                 "baseline_value", "effect_criterion", "stop_condition", "recovery_path"):
        assert row[name] == record[name], name
    assert (row["rule_row_id"], row["activated_at"], row["active"], row["deactivated_at"]) == (ROW, LATER, 1, None)
    assert pa.record_activation(conn, record, state_dir=state, rule_row_id=ROW, now=NOW) == activation_id
    assert len(_rows(conn)) == 1 and pa.active_row_ids(conn, PID) == frozenset({ROW})


def test_a_hand_built_approval_the_store_never_wrote_is_refused(pa, conn, state):
    _decide(state, pattern="p2")  # the store holds an approval, only not of this rule
    rid = "rule-handmade"
    record = _remint({"decision": "approved", "project_id": PID, "rule_id": rid, "decision_id": "dec-handmade",
                      "bundle_ref": store.mint_bundle_ref([rid], NOW), "rollout_scope": store.ROLLOUT_SCOPE,
                      "baseline_metric": "dispatches_with_class:timeout", "baseline_value": 3, "effect_criterion": "e",
                      "stop_condition": "s", "recovery_path": store.RECOVERY_PATH, "decided_at": NOW, "decided_by": "v"})
    with pytest.raises(ValueError, match="not an approval"):
        pa.record_activation(conn, record, state_dir=state, rule_row_id=ROW, now=LATER)
    assert _rows(conn) == []


def test_an_approval_a_later_rejection_overrides_is_refused(pa, conn, state):
    record = _decide(state)
    _append_line(state, json.dumps({**record, "decision": "rejected", "decided_at": LATER, "bundle_ref": None}))
    with pytest.raises(ValueError, match="not an approval"):
        pa.record_activation(conn, record, state_dir=state, rule_row_id=ROW, now=LATER)
    assert _rows(conn) == []


DIFFERS = [("passed", c) for c in ({"decision_id": "dec-x"}, {"bundle_ref": "bundle-x"}, {"effect_criterion": "x"},
                                    {"stop_condition": "x"}, {"baseline_metric": "x"}, {"baseline_value": 4},
                                    {"decided_at": LATER}, {"project_id": "other"}, {"rule_id": "rule-x"})]
DIFFERS += [("record", c) for c in ({"rollout_scope": "fleet"}, {"recovery_path": "rm -rf state"},
                                    {"project_id": "other"}, {"bundle_ref": None})]


@pytest.mark.parametrize("side, changes", DIFFERS)
def test_an_approval_that_differs_from_the_record_on_a_stored_field_is_refused(pa, conn, state, side, changes):
    record = _decide(state)
    passed = _remint(record, **changes) if side == "passed" else record
    if side == "record":
        _append_line(state, json.dumps({**record, **changes}))
    with pytest.raises(ValueError, match="differ|not an approval"):
        pa.record_activation(conn, passed, state_dir=state, rule_row_id=ROW, now=LATER)
    assert _rows(conn) == []


@pytest.mark.parametrize("line", ["not json", "[]", '{"rule_id": "", "decision": "approved"}',
                                  '{"rule_id": "r", "decision": "unknown"}'])
def test_an_unreadable_decision_record_is_refused(pa, conn, state, line):
    record = _decide(state)
    _append_line(state, line)
    with pytest.raises(store.DecisionsUnreadable):
        pa.record_activation(conn, record, state_dir=state, rule_row_id=ROW, now=LATER)
    assert _rows(conn) == []


def _drop(name):
    return lambda r: {k: v for k, v in r.items() if k != name}


def _set(name, value):
    return lambda r: {**r, name: value}


REFUSED = [_drop(n) for n in ("approval_id", "bundle_ref", "effect_criterion", "stop_condition", "recovery_path",
                             "rollout_scope", "baseline_value", "baseline_metric", "decision", "project_id")]
REFUSED += [_set(n, v) for n in ("approval_id", "bundle_ref", "effect_criterion", "stop_condition", "recovery_path",
                                "rollout_scope", "baseline_metric", "decision_id") for v in (None, "", "   ")]
REFUSED += [_set("rollout_scope", "fleet"), _set("rollout_scope", "Project"), _set("decision", "rejected"),
            _set("decision", None), _set("recovery_path", "rm -rf state"), _set("baseline_value", 0),
            _set("baseline_value", True), _set("baseline_value", "3"), _set("baseline_value", None),
            _set("decided_by", "someone else"), lambda r: {**r, "approval_id": r["approval_id"][:-1] + "x"},
            lambda r: list(r.items()), lambda r: None]


@pytest.mark.parametrize("mutate", REFUSED)
def test_a_record_that_cannot_activate_is_refused_and_writes_no_row(pa, conn, state, mutate):
    with pytest.raises(ValueError):
        pa.record_activation(conn, mutate(_decide(state)), state_dir=state, rule_row_id=ROW, now=LATER)
    assert _rows(conn) == []


def test_a_rejected_store_record_is_refused(pa, conn, state):
    with pytest.raises(ValueError, match="approved"):
        pa.record_activation(conn, _decide(state, "rejected"), state_dir=state, rule_row_id=ROW, now=LATER)
    assert _rows(conn) == []


@pytest.mark.parametrize("row_id, now", [(0, LATER), (None, LATER), (True, LATER), ("7", LATER), (ROW, None), (ROW, "")])
def test_a_bad_rule_row_or_time_is_refused(pa, conn, state, row_id, now):
    with pytest.raises(ValueError):
        pa.record_activation(conn, _decide(state), state_dir=state, rule_row_id=row_id, now=now)
    assert _rows(conn) == []


def test_a_row_of_another_project_is_invisible(pa, conn, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    mine = pa.record_activation(conn, _decide(a), state_dir=a, rule_row_id=ROW, now=LATER)
    theirs = pa.record_activation(conn, _decide(b, pid="other"), state_dir=b, rule_row_id=9, now=LATER)
    assert pa.activation_for(conn, "other", mine) is None and pa.activation_for(conn, PID, theirs) is None
    assert pa.active_row_ids(conn, PID) == frozenset({ROW}) and pa.active_row_ids(conn, "other") == frozenset({9})
    assert pa.active_row_ids(conn, "nobody") == frozenset()
    for bad in (None, "", " "):
        with pytest.raises(ValueError):
            pa.active_row_ids(conn, bad)


def test_the_same_key_on_another_rule_row_and_a_second_active_activation_are_refused(pa, conn, state):
    first = _decide(state)
    pa.record_activation(conn, first, state_dir=state, rule_row_id=ROW, now=LATER)
    with pytest.raises(ValueError, match="rule row"):
        pa.record_activation(conn, first, state_dir=state, rule_row_id=ROW + 1, now=LATER)
    with pytest.raises(ValueError, match="active"):
        pa.record_activation(conn, _decide(state, pattern="p2"), state_dir=state, rule_row_id=ROW, now=LATER)
    assert len(_rows(conn)) == 1


def test_a_database_without_the_table_has_no_activation_and_refuses_a_write(pa, tmp_path, state):
    db = _bootstrap(tmp_path)
    _back_to_v34(db)
    with closing(sqlite3.connect(db)) as old:
        assert pa.active_row_ids(old, PID) == frozenset() and pa.activation_for(old, PID, "act-x") is None
        with pytest.raises(sqlite3.OperationalError):
            pa.record_activation(old, _decide(state), state_dir=state, rule_row_id=ROW, now=LATER)
