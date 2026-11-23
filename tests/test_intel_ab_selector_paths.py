"""The A/B arm on select_intelligence and build_intelligence_context (OI-2721).

Both paths stamp ab_window and record the arm decision (ledger, event, then row)
like fetch_intelligence_section. Real temp stores, as in test_intel_ab_injection_path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import coordination_db
import dispatch_register
import intelligence_injection
import intelligence_selector as sel
from migrations.auto_apply import auto_apply

from test_intelligence_selector import _seed_proven_pattern, _setup_quality_db

WINDOW = {"window_id": "w1", "salt": "salt-a", "control_fraction": 0.5,
          "opened_utc": "2026-11-10T08:00:00Z", "hash_version": 1}
PROJECT = "proj-a"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch_register, "_resolve_central_data_dir", lambda pid: tmp_path / "central" / pid)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)


@pytest.fixture
def state(tmp_path):
    coordination_db.init_schema(tmp_path)
    auto_apply(tmp_path / "runtime_coordination.db")
    qi = _setup_quality_db(tmp_path / "quality_intelligence.db")
    _seed_proven_pattern(qi, category="architect", confidence=0.9, usage_count=10)
    qi.close()
    return tmp_path


def _id_for(arm: str, project: str = PROJECT, window: dict = WINDOW) -> str:
    for i in range(500):
        dispatch_id = f"dlv-sp-{i}"
        if sel.arm_for_dispatch(dispatch_id, project, dict(window))[0] == arm:
            return dispatch_id
    raise AssertionError(f"no {arm} id in 500 draws")


def _call_select(state: Path, dispatch_id: str):
    return sel.select_intelligence(
        dispatch_id, "dispatch_create", quality_db_path=state / "quality_intelligence.db",
        coord_state_dir=state, skill_name="architect", dispatch_paths=["scripts/lib/x.py"],
        instruction_text="build x")


def _call_build(state: Path, dispatch_id: str):
    ctx = sel.build_intelligence_context(
        dispatch_id=dispatch_id, role="architect", dispatch_paths=["scripts/lib/x.py"],
        quality_db_path=state / "quality_intelligence.db", coord_state_dir=state)
    return ctx.result


PATHS = pytest.mark.parametrize("call", [_call_select, _call_build], ids=["select", "build"])


def _rows(state: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(str(state / "runtime_coordination.db")) as conn:
        return conn.execute(sql, params).fetchall()


def _injections(state: Path):
    return _rows(state, "SELECT dispatch_id, ab_arm, ab_window, project_id FROM intelligence_injections ORDER BY id")


def _events(state: Path, dispatch_id: str):
    sql = ("SELECT event_type, metadata_json FROM coordination_events "
           "WHERE entity_id = ? AND event_type LIKE 'intelligence_%' ORDER BY rowid")
    return [(r[0], json.loads(r[1])) for r in _rows(state, sql, (dispatch_id,))]


def _ledger(state: Path, dispatch_id: str):
    path = state / "dispatch_register.ndjson"
    raw = path.read_bytes().decode("utf-8", errors="replace").split("\n") if path.exists() else []
    lines = [json.loads(x) for x in raw if x.startswith("{") and x.endswith("}")]
    return [x for x in lines if x.get("dispatch_id") == dispatch_id]


@PATHS
@pytest.mark.parametrize("arm", ["control", "treatment"])
def test_the_row_carries_the_window_the_arm_was_drawn_under(env, state, call, arm):
    dispatch_id = _id_for(arm)
    result = call(state, dispatch_id)
    ab_window = sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    assert ab_window.startswith("w1:")
    assert _injections(state) == [(dispatch_id, arm, ab_window, PROJECT)]
    assert (result.ab_arm, result.ab_window) == (arm, ab_window)
    [(event_type, meta)] = _events(state, dispatch_id)
    assert (meta["ab_arm"], meta["ab_window"], meta["project_id"], meta["dispatch_id"]) == (
        arm, ab_window, PROJECT, dispatch_id)
    assert (result.items_injected > 0) is (arm == "treatment")


@PATHS
@pytest.mark.parametrize("arm", ["control", "treatment"])
def test_ledger_line_and_event_precede_the_row(env, state, monkeypatch, call, arm):
    order = []

    def spy(name, seen):
        real = getattr(sel.IntelligenceSelector, name)

        def wrapper(self, result, coord_state_dir=None):
            order.append((name, seen(result.dispatch_id)))
            return real(self, result, coord_state_dir=coord_state_dir)
        monkeypatch.setattr(sel.IntelligenceSelector, name, wrapper)

    spy("emit_event", lambda d: [x["event"] for x in _ledger(state, d)])
    spy("record_injection", lambda d: [e[0] for e in _events(state, d)])
    dispatch_id = _id_for(arm)
    call(state, dispatch_id)
    assert [o[0] for o in order] == ["emit_event", "record_injection"]
    assert order[0][1] == ["intelligence_arm_assigned"]
    assert order[1][1] != []
    [line] = _ledger(state, dispatch_id)
    ab_window = sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    assert line["project_id"] == PROJECT and line["extra"] == {"ab_arm": arm, "ab_window": ab_window}


@PATHS
def test_a_failing_ledger_write_writes_no_row_and_returns_a_result(env, state, monkeypatch, call):
    monkeypatch.setattr(dispatch_register, "append_event_idempotent", lambda *a, **k: False)
    dispatch_id = _id_for("treatment")
    result = call(state, dispatch_id)
    assert result is not None
    assert _injections(state) == [] and _events(state, dispatch_id) == []


@PATHS
@pytest.mark.parametrize("off", [
    {"VNX_INTEL_AB_TEST": "0"},
    {"VNX_INTEL_AB_TEST": "1", "VNX_INTEL_AB_WINDOW": None},
    {"VNX_INTEL_AB_TEST": "1", "VNX_INTEL_AB_WINDOW": '{"window_id": "w1"}'},
], ids=["flag_off", "no_window", "invalid_window"])
def test_no_active_window_is_unstamped_and_writes_no_ledger_line(env, state, monkeypatch, call, off):
    for key, value in off.items():
        monkeypatch.delenv(key) if value is None else monkeypatch.setenv(key, value)
    dispatch_id = "dlv-sp-plain"
    result = call(state, dispatch_id)
    assert result.items_injected > 0
    assert _injections(state) == [(dispatch_id, None, None, PROJECT)]
    [(_, meta)] = _events(state, dispatch_id)
    assert "ab_arm" not in meta and "ab_window" not in meta
    assert _ledger(state, dispatch_id) == []


@PATHS
def test_placebo_stays_placebo_without_a_window_or_ledger_line(env, state, monkeypatch, call):
    monkeypatch.setenv("VNX_INTEL_PLACEBO_ARM", "1")
    dispatch_id = "dlv-sp-placebo"
    call(state, dispatch_id)
    [row] = _injections(state)
    assert row[:3] == (dispatch_id, sel.PLACEBO_ARM, None)
    assert _ledger(state, dispatch_id) == []


def test_select_intelligence_with_a_raising_record_injection_returns_and_logs(env, state, monkeypatch, caplog):
    def boom(self, result, coord_state_dir=None):
        raise OSError("disk full")
    monkeypatch.setattr(sel.IntelligenceSelector, "record_injection", boom)
    dispatch_id = _id_for("treatment")
    with caplog.at_level(logging.DEBUG):
        result = _call_select(state, dispatch_id)
    assert result.ab_arm == "treatment"
    assert "record_injection failed" in caplog.text and dispatch_id in caplog.text


@PATHS
def test_a_second_decision_with_another_window_writes_no_row_and_keeps_one_ledger_line(env, state, monkeypatch, call):
    dispatch_id = _id_for("treatment")
    call(state, dispatch_id)
    other = dict(WINDOW, window_id="w2")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(other))
    other_id = sel.arm_for_dispatch(dispatch_id, PROJECT, dict(other))
    assert other_id[1] != sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    before = _injections(state)
    call(state, dispatch_id)
    assert _injections(state) == before
    [line] = _ledger(state, dispatch_id)
    assert line["extra"]["ab_window"].startswith("w1:")


@PATHS
def test_a_second_decision_for_another_project_in_the_same_state_dir_writes_nothing(env, state, monkeypatch, call):
    dispatch_id = _id_for("treatment")
    call(state, dispatch_id)
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-b")
    # same arm as proj-a so only the project differs between the two decisions
    if sel.arm_for_dispatch(dispatch_id, "proj-b", dict(WINDOW))[0] != "treatment":
        pytest.skip("proj-b draws control for this id")
    before = _injections(state)
    call(state, dispatch_id)
    assert _injections(state) == before
    [line] = _ledger(state, dispatch_id)
    assert line["project_id"] == PROJECT


def test_the_same_decision_twice_is_recorded_both_times(env, state):
    dispatch_id = _id_for("treatment")
    assert intelligence_injection._record_arm_decision(_decided(dispatch_id), state) is True
    assert intelligence_injection._record_arm_decision(_decided(dispatch_id), state) is True
    assert len(_ledger(state, dispatch_id)) == 1


@pytest.mark.parametrize("bad", ["window", "project", "arm"])
def test_a_deduplicated_decision_that_differs_is_not_recorded(env, state, bad):
    dispatch_id = _id_for("treatment")
    assert intelligence_injection._record_arm_decision(_decided(dispatch_id), state) is True
    changed = _decided(dispatch_id)
    if bad == "window":
        changed.ab_window = "w2:0000000000000000"
    elif bad == "project":
        changed.project_id = "proj-b"
    else:
        changed.ab_arm = "control"
    assert intelligence_injection._record_arm_decision(changed, state) is False
    assert len(_ledger(state, dispatch_id)) == 1


def test_an_unreadable_ledger_line_after_the_write_is_not_a_recorded_decision(env, state, monkeypatch):
    dispatch_id = _id_for("treatment")
    monkeypatch.setattr(dispatch_register, "append_event_idempotent", lambda *a, **k: True)
    assert intelligence_injection._record_arm_decision(_decided(dispatch_id), state) is False


def _fetch(state: Path, dispatch_id: str):
    return intelligence_injection.fetch_intelligence_section(
        dispatch_id, "architect", state, dispatch_paths=["scripts/lib/x.py"], instruction_text="build x")


ENTRY_POINTS = pytest.mark.parametrize("call", [_call_select, _call_build, _fetch], ids=["select", "build", "fetch"])
DAMAGED = b"\xff\xfe{\x80 not utf-8 \xc3\x28\n"


@ENTRY_POINTS
def test_a_damaged_line_elsewhere_in_the_register_spoils_only_that_line(env, state, call):
    dispatch_id = _id_for("treatment")
    (state / "dispatch_register.ndjson").write_bytes(DAMAGED)
    call(state, dispatch_id)
    assert len(_ledger(state, dispatch_id)) == 1
    assert [r[:3] for r in _injections(state)] == [(dispatch_id, "treatment", _decided(dispatch_id).ab_window)]


@ENTRY_POINTS
def test_a_register_of_only_damaged_bytes_is_not_recorded_and_raises_nothing(env, state, monkeypatch, call):
    dispatch_id = _id_for("treatment")
    path = state / "dispatch_register.ndjson"
    path.write_bytes(DAMAGED)
    monkeypatch.setattr(dispatch_register, "append_event_idempotent", lambda *a, **k: True)
    call(state, dispatch_id)
    assert _injections(state) == []


def _decided(dispatch_id: str):
    from intelligence_sources import InjectionResult
    return InjectionResult(
        injection_point="dispatch_create", injected_at="2026-11-10T09:00:00Z", items=[], suppressed=[],
        task_class="x", dispatch_id=dispatch_id, ab_arm="treatment",
        ab_window=sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1], project_id=PROJECT)
