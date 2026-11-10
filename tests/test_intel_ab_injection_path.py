"""The A/B arm on the live injection path (effect PR-5): fetch_intelligence_section.

Real temp stores: the coordination store is built with init_schema plus auto_apply
over the real migration directory, the quality store with the selector test helpers.
"""

from __future__ import annotations

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

WINDOW = {
    "window_id": "w1", "salt": "salt-a", "control_fraction": 0.5,
    "opened_utc": "2026-11-10T08:00:00Z", "hash_version": 1,
}
PROJECT = "proj-a"


@pytest.fixture
def env(monkeypatch):
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
        dispatch_id, "architect", state, dispatch_paths=["scripts/lib/x.py"], instruction_text="build x",
    )


def _rows(state: Path, sql: str, params: tuple = ()):
    conn = sqlite3.connect(str(state / "runtime_coordination.db"))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _injections(state: Path):
    return _rows(state, "SELECT dispatch_id, ab_arm, ab_window, items_injected, project_id "
                        "FROM intelligence_injections ORDER BY id")


def _offers(state: Path) -> int:
    conn = sqlite3.connect(str(state / "quality_intelligence.db"))
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='dispatch_pattern_offered'").fetchone()
        return conn.execute("SELECT COUNT(*) FROM dispatch_pattern_offered").fetchone()[0] if has else 0
    finally:
        conn.close()


def test_control_draw_gets_no_section_and_a_stamped_row_without_offers(env, state):
    dispatch_id = _id_for("control")
    assert _fetch(state, dispatch_id) == ""
    [row] = _injections(state)
    ab_window = sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    assert row == (dispatch_id, "control", ab_window, 0, PROJECT)
    assert row[2].startswith("w1:")
    assert _offers(state) == 0


def test_treatment_draw_injects_and_stamps_the_window(env, state):
    dispatch_id = _id_for("treatment")
    section = _fetch(state, dispatch_id)
    assert section != ""
    [row] = _injections(state)
    assert row[1] == "treatment" and row[2] == sel.arm_for_dispatch(dispatch_id, PROJECT, dict(WINDOW))[1]
    assert row[3] > 0
    assert _offers(state) > 0


@pytest.mark.parametrize("off", ["flag_off", "no_window", "invalid_window"])
def test_no_active_window_is_byte_identical_and_unstamped(env, state, monkeypatch, off):
    dispatch_id = "dlv-path-plain"
    # The reference: the same call on the same inputs with no A/B configuration at all.
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    baseline = _fetch(state, dispatch_id)
    assert baseline != ""
    if off == "flag_off":
        pass
    elif off == "no_window":
        monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
        monkeypatch.delenv("VNX_INTEL_AB_WINDOW")
    else:
        monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
        monkeypatch.setenv("VNX_INTEL_AB_WINDOW", '{"window_id": "w1"}')
    assert _fetch(state, dispatch_id) == baseline
    assert [r[1:3] for r in _injections(state)] == [(None, None), (None, None)]


def test_a_forced_control_draw_with_the_window_off_still_injects(env, state, monkeypatch):
    dispatch_id = _id_for("control")
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    assert _fetch(state, dispatch_id) != ""
    assert _injections(state)[0][1:3] == (None, None)


def test_a_store_without_the_arm_columns_writes_the_row_without_the_arm(env, state, caplog):
    conn = sqlite3.connect(str(state / "runtime_coordination.db"))
    conn.execute("DROP INDEX IF EXISTS idx_injection_window_arm")
    conn.execute("ALTER TABLE intelligence_injections DROP COLUMN ab_window")
    conn.execute("ALTER TABLE intelligence_injections DROP COLUMN ab_arm")
    conn.commit()
    conn.close()
    dispatch_id = _id_for("control")
    assert _fetch(state, dispatch_id) == ""
    [(got_id, items)] = _rows(state, "SELECT dispatch_id, items_injected FROM intelligence_injections")
    assert (got_id, items) == (dispatch_id, 0)
    events = _rows(state, "SELECT entity_id, actor, metadata_json FROM coordination_events "
                          "WHERE event_type = 'ab_stamp_failed'")
    assert len(events) == 1 and events[0][0] == dispatch_id
    meta = json.loads(events[0][2])
    assert meta["ab_arm"] == "control" and meta["has_ab_arm_column"] is False


def test_the_event_write_failing_does_not_fail_the_row(env, state):
    conn = sqlite3.connect(str(state / "runtime_coordination.db"))
    conn.execute("DROP INDEX IF EXISTS idx_injection_window_arm")
    conn.execute("ALTER TABLE intelligence_injections DROP COLUMN ab_window")
    conn.execute("ALTER TABLE intelligence_injections DROP COLUMN ab_arm")
    conn.execute("DROP TABLE coordination_events")
    conn.commit()
    conn.close()
    assert _fetch(state, _id_for("treatment")) != ""
    assert len(_injections_columnless(state)) == 1


def _injections_columnless(state: Path):
    return _rows(state, "SELECT dispatch_id FROM intelligence_injections")


def test_two_projects_with_the_same_dispatch_id_get_their_own_rows(env, state, monkeypatch):
    dispatch_id = _id_for("control", "proj-a")
    other = next(p for p in ("proj-b", "proj-c", "proj-d", "proj-e", "proj-f")
                 if sel.arm_for_dispatch(dispatch_id, p, dict(WINDOW))[0] == "treatment")
    _fetch(state, dispatch_id)
    monkeypatch.setenv("VNX_PROJECT_ID", other)
    _fetch(state, dispatch_id)
    rows = {r[4]: r for r in _injections(state)}
    assert rows["proj-a"][1] == "control" and rows[other][1] == "treatment"
    assert rows["proj-a"][2] != rows[other][2] or rows["proj-a"][1] != rows[other][1]


def test_the_default_of_the_result_is_not_assigned():
    from intelligence_sources import InjectionResult
    result = InjectionResult("dispatch_create", "2026-11-10T00:00:00Z", [], [], "x", "d")
    assert result.ab_arm is None and result.ab_window is None
