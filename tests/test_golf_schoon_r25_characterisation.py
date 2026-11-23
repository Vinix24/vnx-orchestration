#!/usr/bin/env python3
"""Characterisation of ``WorkflowSupervisor.handle_incident`` (golf schoon R25).

Pins, on real temporary SQLite stores, what one call observably does: the
returned decision, the rows it writes in incident_log, retry_state and
escalation_log, the dispatch state it leaves behind, and the order of the
collaborator calls (reads and writes). Written green on the code before the
extract-method refactor and kept green after it.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB))

from incident_taxonomy import IncidentClass
from runtime_coordination import (
    get_connection,
    get_dispatch,
    init_schema,
    register_dispatch,
    transition_dispatch,
)

_spec = importlib.util.spec_from_file_location(
    "_golf_r25_workflow_supervisor", _LIB / "workflow_supervisor.py"
)
ws = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ws
_spec.loader.exec_module(ws)

_TRACED_FUNCS = (
    "get_contract", "should_escalate", "get_dispatch", "select_recovery_action",
    "record_incident", "apply_dead_letter", "record_escalation", "get_cooldown_seconds",
)
_TRACED_METHODS = (
    "_get_or_create_retry_state", "_detect_repeated_failure_loop",
    "_cooldown_elapsed", "_increment_retry",
)


@pytest.fixture
def state_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        init_schema(tmpdir)
        yield tmpdir


@pytest.fixture
def trace(monkeypatch):
    calls = []

    def _wrap(name, original):
        def _traced(*args, **kwargs):
            calls.append(name)
            return original(*args, **kwargs)
        return _traced

    for name in _TRACED_FUNCS:
        monkeypatch.setattr(ws, name, _wrap(name, getattr(ws, name)))
    for name in _TRACED_METHODS:
        monkeypatch.setattr(
            ws.WorkflowSupervisor, name, _wrap(name, getattr(ws.WorkflowSupervisor, name))
        )
    return calls


def _register(state_dir, dispatch_id, states=()):
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id="T1", track="B",
                          project_id="vnx-dev")
        for to_state in states:
            transition_dispatch(conn, dispatch_id=dispatch_id, to_state=to_state,
                                actor="test", reason="setup")
        conn.commit()


def _rows(state_dir, sql):
    with get_connection(state_dir) as conn:
        return [tuple(r) for r in conn.execute(sql).fetchall()]


def _call(sup, trace, **kwargs):
    trace.clear()
    decision = sup.handle_incident(**kwargs)
    return decision, list(trace)


_D = ("action_taken", "should_retry", "should_escalate", "should_dead_letter",
      "auto_recovery_halted", "budget_remaining", "cooldown_seconds", "incident_class")


def _short(decision):
    d = decision.to_dict()
    return tuple(d[k] for k in _D)


_BASE = ["get_contract", "_get_or_create_retry_state"]


def test_delivery_failure_series_on_failed_delivery_dispatch(state_dir, trace):
    _register(state_dir, "d-c1", ("claimed", "delivering", "failed_delivery"))
    sup = ws.WorkflowSupervisor(state_dir, auto_init=False)
    seen = []
    for i in range(5):
        decision, calls = _call(sup, trace, incident_class=IncidentClass.DELIVERY_FAILURE,
                                dispatch_id="d-c1", terminal_id="T1", component="tap",
                                reason=f"fail {i}", metadata={"n": i})
        assert decision.dispatch_id == "d-c1"
        assert decision.reason == f"fail {i}"
        assert decision.incident_record is not None
        assert (decision.escalation_record is None) is (not decision.should_escalate)
        seen.append((_short(decision), calls))

    read = _BASE + ["should_escalate", "get_dispatch", "_cooldown_elapsed",
                    "select_recovery_action", "record_incident", "_increment_retry",
                    "get_cooldown_seconds"]
    loop = _BASE + ["_detect_repeated_failure_loop", "get_contract", "should_escalate",
                    "get_dispatch", "select_recovery_action", "record_incident",
                    "_increment_retry", "get_cooldown_seconds"]
    assert seen == [
        (("redeliver_dispatch", True, False, False, False, 2, 5, "delivery_failure"),
         read + ["get_cooldown_seconds"]),
        (("escalate_to_operator", False, False, False, False, 1, 0, "delivery_failure"),
         read),
        (("escalate_to_operator", False, True, False, False, 0, 0, "delivery_failure"),
         read + ["record_escalation"]),
        (("dead_letter_dispatch", False, True, True, True, 0, 0, "repeated_failure_loop"),
         loop + ["apply_dead_letter", "record_escalation"]),
        (("halt_terminal", False, True, False, True, 0, 0, "repeated_failure_loop"),
         loop + ["record_escalation"]),
    ]

    def _inc(cls, sev, state, n, exhausted):
        return (cls, sev, "d-c1", "T1", "tap", state, n, exhausted, exhausted, 0,
                f"fail {n - 1}", "workflow_supervisor")

    assert _rows(state_dir, "SELECT incident_class, severity, dispatch_id, terminal_id,"
                 " component_name, state, attempt_count, budget_exhausted, escalated,"
                 " auto_recovery_halted, failure_detail, actor FROM incident_log"
                 " ORDER BY id") == [
        _inc("delivery_failure", "warning", "open", 1, 0),
        _inc("delivery_failure", "warning", "open", 2, 0),
        _inc("delivery_failure", "warning", "escalated", 3, 1),
        _inc("repeated_failure_loop", "critical", "dead_lettered", 4, 1),
        _inc("repeated_failure_loop", "critical", "escalated", 5, 1),
    ]
    assert _rows(state_dir, "SELECT dispatch_id, incident_class, attempts_used,"
                 " budget_exhausted, escalated, halted FROM retry_state") == [
        ("d-c1", "delivery_failure", 5, 1, 1, 1)]
    assert _rows(state_dir, "SELECT e.incident_class, e.severity, e.reason, e.retry_count,"
                 " e.budget_exhausted, e.auto_recovery_halted, i.failure_detail"
                 " FROM escalation_log e JOIN incident_log i ON i.incident_id = e.incident_id"
                 " ORDER BY e.id") == [
        ("delivery_failure", "warning", "fail 2", 2, 0, 0, "fail 2"),
        ("repeated_failure_loop", "critical", "fail 3", 3, 1, 1, "fail 3"),
        ("repeated_failure_loop", "critical", "fail 4", 4, 1, 1, "fail 4"),
    ]
    with get_connection(state_dir) as conn:
        assert get_dispatch(conn, "d-c1")["state"] == "dead_letter"


def test_incident_without_dispatch_skips_dispatch_reads_and_retry_writes(state_dir, trace):
    sup = ws.WorkflowSupervisor(state_dir, auto_init=False)
    decision, calls = _call(sup, trace, incident_class=IncidentClass.PROCESS_CRASH,
                            terminal_id="T2", reason="crash")
    assert decision.dispatch_id == ""
    assert _short(decision) == ("restart_process", True, False, False, False, 2, 10,
                                "process_crash")
    assert calls == _BASE + ["should_escalate", "_cooldown_elapsed",
                             "select_recovery_action", "record_incident",
                             "get_cooldown_seconds"]
    assert _rows(state_dir, "SELECT COUNT(*) FROM retry_state") == [(0,)]
    assert _rows(state_dir, "SELECT incident_class, dispatch_id, terminal_id, attempt_count"
                 " FROM incident_log") == [("process_crash", None, "T2", 1)]


def test_lease_conflict_halts_and_escalates_on_first_call(state_dir, trace):
    _register(state_dir, "d-c3")
    sup = ws.WorkflowSupervisor(state_dir, auto_init=False)
    decision, calls = _call(sup, trace, incident_class=IncidentClass.LEASE_CONFLICT,
                            dispatch_id="d-c3", terminal_id="T1", reason="conflict",
                            metadata={"k": "v"})
    assert _short(decision) == ("escalate_to_operator", False, True, False, True, 0, 0,
                                "lease_conflict")
    assert calls == _BASE + ["should_escalate", "get_dispatch", "select_recovery_action",
                             "record_incident", "_increment_retry", "get_cooldown_seconds",
                             "record_escalation"]
    assert decision.escalation_record.incident_id == decision.incident_record.incident_id
    assert _rows(state_dir, "SELECT incident_class, retry_count, budget_exhausted,"
                 " auto_recovery_halted, metadata_json FROM escalation_log") == [
        ("lease_conflict", 0, 0, 1, '{"k": "v"}')]
    with get_connection(state_dir) as conn:
        assert get_dispatch(conn, "d-c3")["state"] == "queued"


def test_unregistered_dispatch_never_dead_letters(state_dir, trace):
    sup = ws.WorkflowSupervisor(state_dir, auto_init=False)
    for _ in range(3):
        decision, calls = _call(sup, trace, incident_class=IncidentClass.DELIVERY_FAILURE,
                                dispatch_id="d-ghost", reason="x")
    assert decision.should_dead_letter is False
    assert "apply_dead_letter" not in calls
    assert _rows(state_dir, "SELECT attempts_used FROM retry_state") == [(3,)]


def test_record_incident_failure_propagates_before_any_retry_write(state_dir, monkeypatch):
    _register(state_dir, "d-c5")
    sup = ws.WorkflowSupervisor(state_dir, auto_init=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("incident write failed")

    monkeypatch.setattr(ws, "record_incident", _boom)
    with pytest.raises(RuntimeError, match="incident write failed"):
        sup.handle_incident(incident_class=IncidentClass.DELIVERY_FAILURE,
                            dispatch_id="d-c5", reason="x")
    assert _rows(state_dir, "SELECT attempts_used FROM retry_state") in ([], [(0,)])
    assert _rows(state_dir, "SELECT COUNT(*) FROM incident_log") == [(0,)]
