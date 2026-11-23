"""The A/B arm on the live injection path (effect PR-5): fetch_intelligence_section.

Real temp stores: the coordination store is built with init_schema plus auto_apply
over the real migration directory, the quality store with the selector test helpers.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import coordination_db
import intelligence_injection
import intelligence_selector as sel
from migrations.auto_apply import auto_apply

from test_intelligence_selector import _seed_proven_pattern, _setup_quality_db

WINDOW = {"window_id": "w1", "salt": "salt-a", "control_fraction": 0.5,
          "opened_utc": "2026-11-10T08:00:00Z", "hash_version": 1}
PROJECT = "proj-a"
QI = "quality_intelligence.db"
STAMPED = "SELECT source_dispatch_ids FROM success_patterns WHERE source_dispatch_ids IS NOT NULL"


@pytest.fixture
def env(monkeypatch, tmp_path):
    # The module the code under test imports now, not the collection-time reference: an
    # earlier test may have re-imported it, and a patch on the stale object misses.
    live_register = importlib.import_module("dispatch_register")
    monkeypatch.setattr(live_register, "_resolve_central_data_dir", lambda pid: tmp_path / "central" / pid)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.delenv("VNX_ADR_INJECTION", raising=False)


@pytest.fixture
def state(tmp_path):
    coordination_db.init_schema(tmp_path)
    auto_apply(tmp_path / "runtime_coordination.db")
    qi = _setup_quality_db(tmp_path / "quality_intelligence.db")
    _seed_proven_pattern(qi, category="architect", confidence=0.9, usage_count=10)
    qi.close()
    return tmp_path


def _id_for(arm: str, project: str = PROJECT) -> str:
    for i in range(500):
        dispatch_id = f"dlv-path-{i}"
        if sel.arm_for_dispatch(dispatch_id, project, dict(WINDOW))[0] == arm:
            return dispatch_id
    raise AssertionError(f"no {arm} id in 500 draws")


def _fetch(state: Path, dispatch_id: str) -> str:
    return intelligence_injection.fetch_intelligence_section(
        dispatch_id, "architect", state, dispatch_paths=["scripts/lib/x.py"], instruction_text="build x")


def _rows(state: Path, sql: str, params: tuple = (), db: str = "runtime_coordination.db"):
    with sqlite3.connect(str(state / db)) as conn:
        return conn.execute(sql, params).fetchall()


def _injections(state: Path):
    return _rows(state, "SELECT dispatch_id, ab_arm, ab_window, items_injected, project_id "
                        "FROM intelligence_injections ORDER BY id")


def _offers(state: Path) -> int:
    has = _rows(state, "SELECT 1 FROM sqlite_master WHERE name='dispatch_pattern_offered'", db=QI)
    return _rows(state, "SELECT COUNT(*) FROM dispatch_pattern_offered", db=QI)[0][0] if has else 0


@pytest.mark.parametrize("arm", ["control", "treatment"])
def test_each_draw_gets_a_stamped_row_and_only_treatment_injects(env, state, arm):
    dispatch_id = _id_for(arm)
    section = _fetch(state, dispatch_id)
    [row] = _injections(state)
    ab_window = sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    assert row[:3] == (dispatch_id, arm, ab_window) and row[4] == PROJECT and row[2].startswith("w1:")
    assert (section != "") is (arm == "treatment")
    assert (row[3] > 0) is (arm == "treatment")
    assert (_offers(state) > 0) is (arm == "treatment")


@pytest.mark.parametrize("off", [
    {"VNX_INTEL_AB_TEST": "0"},
    {"VNX_INTEL_AB_TEST": "1", "VNX_INTEL_AB_WINDOW": None},
    {"VNX_INTEL_AB_TEST": "1", "VNX_INTEL_AB_WINDOW": '{"window_id": "w1"}'},
], ids=["flag_off", "no_window", "invalid_window"])
def test_no_active_window_is_byte_identical_and_unstamped(env, state, monkeypatch, off):
    dispatch_id = "dlv-path-plain"
    # The reference: the same call on the same inputs with no A/B configuration at all.
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    baseline = _fetch(state, dispatch_id)
    assert baseline != ""
    for key, value in off.items():
        monkeypatch.delenv(key) if value is None else monkeypatch.setenv(key, value)
    assert _fetch(state, dispatch_id) == baseline
    assert [r[1:3] for r in _injections(state)] == [(None, None), (None, None)]


def test_a_forced_control_draw_with_the_window_off_still_injects(env, state, monkeypatch):
    dispatch_id = _id_for("control")
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    assert _fetch(state, dispatch_id) != "" and _injections(state)[0][1:3] == (None, None)


def test_a_store_without_the_arm_columns_writes_the_row_without_the_arm(env, state, caplog):
    with sqlite3.connect(str(state / "runtime_coordination.db")) as conn:
        for sql in ("DROP INDEX IF EXISTS idx_injection_window_arm",
                    "ALTER TABLE intelligence_injections DROP COLUMN ab_window",
                    "ALTER TABLE intelligence_injections DROP COLUMN ab_arm"):
            conn.execute(sql)
    assert _fetch(state, dispatch_id := _id_for("control")) == ""
    assert _rows(state, "SELECT dispatch_id, items_injected FROM intelligence_injections") == [(dispatch_id, 0)]
    events = _rows(state, "SELECT entity_id, metadata_json FROM coordination_events "
                          "WHERE event_type = 'ab_stamp_failed'")
    assert len(events) == 1 and events[0][0] == dispatch_id
    assert json.loads(events[0][1])["has_ab_arm_column"] is False


def _events(state: Path, dispatch_id: str):
    sql = ("SELECT event_id, event_type, metadata_json FROM coordination_events "
           "WHERE entity_id = ? AND event_type LIKE 'intelligence_%' ORDER BY rowid")
    return [(r[0], r[1], json.loads(r[2])) for r in _rows(state, sql, (dispatch_id,))]


def _ledger(state: Path, dispatch_id: str):
    path = state / "dispatch_register.ndjson"
    lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [x for x in lines if x.get("dispatch_id") == dispatch_id]


@pytest.mark.parametrize("arm", ["control", "treatment"])
def test_the_arm_decision_is_in_the_event_and_the_ledger_before_the_row(env, state, monkeypatch, arm):
    order = []

    def spy(name, seen):
        real = getattr(sel.IntelligenceSelector, name)
        def wrapper(self, result, coord_state_dir=None):
            order.append((name, seen(result.dispatch_id)))
            return real(self, result, coord_state_dir=coord_state_dir)
        monkeypatch.setattr(sel.IntelligenceSelector, name, wrapper)

    spy("emit_event", lambda d: [x["event"] for x in _ledger(state, d)])
    spy("record_injection", lambda d: _events(state, d))
    dispatch_id = _id_for(arm)
    _fetch(state, dispatch_id)
    ab_window = sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    assert [o[0] for o in order] == ["emit_event", "record_injection"]
    [(_, _, meta)] = order[1][1]  # the event exists when the row is written
    assert (meta["ab_arm"], meta["ab_window"], meta["dispatch_id"], meta["project_id"]) == (
        arm, ab_window, dispatch_id, PROJECT)
    assert order[0][1] == ["intelligence_arm_assigned"]  # ledger line precedes the SQLite event
    [line] = _ledger(state, dispatch_id)
    assert line["project_id"] == PROJECT and line["extra"] == {"ab_arm": arm, "ab_window": ab_window}


def test_no_window_event_carries_no_arm_and_writes_no_ledger_line(env, state, monkeypatch):
    monkeypatch.delenv("VNX_INTEL_AB_TEST")
    dispatch_id = "dlv-path-nowindow"
    assert _fetch(state, dispatch_id) != ""
    [(_, _, meta)] = _events(state, dispatch_id)
    assert "ab_arm" not in meta and "ab_window" not in meta
    assert _ledger(state, dispatch_id) == []


@pytest.mark.parametrize("arm,expect_section", [("control", False), ("treatment", True)])
def test_emit_event_raising_writes_no_row_but_the_dispatch_proceeds(env, state, monkeypatch, arm, expect_section):
    def boom(self, result, coord_state_dir=None):
        raise OSError("disk full")
    monkeypatch.setattr(sel.IntelligenceSelector, "emit_event", boom)
    assert (_fetch(state, _id_for(arm)) != "") is expect_section
    assert _rows(state, "SELECT dispatch_id FROM intelligence_injections") == []
    assert _rows(state, STAMPED, db=QI) == []


def test_the_ledger_write_failing_writes_nothing_derived_and_keeps_the_section(env, state, monkeypatch):
    def derived():
        return {"ledger": _ledger(state, d), "offers": _offers(state), "stamps": _rows(state, STAMPED, db=QI),
                "events": _rows(state, "SELECT event_type FROM coordination_events WHERE entity_id = ?", (d,)),
                "rows": _rows(state, "SELECT ab_arm FROM intelligence_injections")}
    with monkeypatch.context() as m:
        m.setattr(importlib.import_module("dispatch_register"), "append_event_idempotent", lambda *a, **k: False)
        section = _fetch(state, d := _id_for("treatment"))
    assert section != "" and derived() == {"ledger": [], "offers": 0, "events": [], "rows": [], "stamps": []}
    assert _fetch(state, d) == section and all(derived().values())  # recorded: same section, every write seen


def test_two_projects_with_the_same_dispatch_id_get_their_own_rows(env, state, monkeypatch):
    dispatch_id = _id_for("control", "proj-a")
    other = next(p for p in ("proj-b", "proj-c", "proj-d", "proj-e", "proj-f")
                 if sel.arm_for_dispatch(dispatch_id, p, dict(WINDOW))[0] == "treatment")
    second = state / "second-project"  # one state dir per project: a shared one refuses the second decision
    second.mkdir()
    coordination_db.init_schema(second)
    auto_apply(second / "runtime_coordination.db")
    qi = _setup_quality_db(second / "quality_intelligence.db")
    _seed_proven_pattern(qi, category="architect", confidence=0.9, usage_count=10)
    qi.close()
    _fetch(state, dispatch_id)
    monkeypatch.setenv("VNX_PROJECT_ID", other)
    _fetch(second, dispatch_id)
    assert _injections(state)[0][1] == "control" and _injections(state)[0][4] == "proj-a"
    assert _injections(second)[0][1] == "treatment" and _injections(second)[0][4] == other


def test_the_default_of_the_result_is_not_assigned():
    from intelligence_sources import InjectionResult
    result = InjectionResult("dispatch_create", "2026-11-10T00:00:00Z", [], [], "x", "d")
    assert (result.ab_arm, result.ab_window) == (None, None)
