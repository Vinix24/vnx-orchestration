"""Effect PR-8: the falsifications F1-F3 with their coverage (plan decision 19; CQS and open items follow in PR-8b).

Stores are PR-6's RC fixture (real migrations to 0038) plus a QI built here with the columns the measures read.
The module is imported inside the tests, so on the old code the CLI tests fail on behaviour. Temp stores only.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from fractions import Fraction

import pytest

from test_intel_effect_cohort import AS_OF, DAY1, _balanced, _exec, _seed, rc
from test_intel_effect_estimator import W46, _cell

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_QI_SCHEMA = (
    "CREATE TABLE dispatch_pattern_offered (dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL, "
    "project_id TEXT NOT NULL, PRIMARY KEY (dispatch_id, pattern_id, project_id))",
    "CREATE TABLE pattern_injection_outcome (id INTEGER PRIMARY KEY, dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL, "
    "used INTEGER, project_id TEXT NOT NULL, UNIQUE (project_id, dispatch_id, pattern_id))",
)


@pytest.fixture
def sec():
    return importlib.import_module("intel_effect_secondary")


def _qi(rc):
    path = rc.parent / "quality_intelligence.db"
    with closing(sqlite3.connect(str(path))) as conn, conn:
        for ddl in _QI_SCHEMA:
            conn.execute(ddl)
    return path


def _treatment(rc, at):
    with closing(sqlite3.connect(str(rc))) as conn:
        return [r[0] for r in conn.execute("SELECT dispatch_id FROM intelligence_injections WHERE project_id = 'p1' "
                                           "AND ab_arm = 'treatment' AND injected_at = ? ORDER BY id", (at,))]


def _offers(rc, at, adoption, cover=100, pid="p1"):
    """One offer per treatment dispatch of that week; the first ``cover`` % have an outcome, the first ``adoption`` % of
    the 100 are used."""
    ids = _treatment(rc, at)
    path = rc.parent / "quality_intelligence.db"
    _exec(path, "INSERT OR IGNORE INTO dispatch_pattern_offered VALUES (?, 'pat', ?)", [(d, pid) for d in ids])
    _exec(path, "INSERT OR IGNORE INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id) "
                "VALUES (?, 'pat', ?, ?)", [(d, int(i < adoption), pid) for i, d in enumerate(ids[:cover])])


def _world(rc, adoption=(20, 40), cover=100, chars=(100, 80), treatment_ok=98):
    """Two weeks, 100 per arm each (n = 200 per arm), treatment adoption and chars per item per week."""
    _qi(rc)
    for prefix, at, per, cpi in (("a", DAY1, adoption[0], chars[0]), ("b", W46, adoption[1], chars[1])):
        _cell(rc, prefix, "coding_interactive", at, control=(100, 98), treatment=(100, treatment_ok))
        _offers(rc, at, per, cover)
        _exec(rc, "UPDATE intelligence_injections SET items_injected = 2, payload_chars = ? "
                  "WHERE ab_arm = 'treatment' AND injected_at = ?", [(2 * cpi, at)])


def _run(rc, cmd, *extra):
    run = subprocess.run(
        [sys.executable, os.path.join(_ROOT, "scripts", "intel_injection_join.py"), cmd, "--rc", str(rc), "--qi",
         str(rc.parent / "quality_intelligence.db"), "--project-id", "p1", "--as-of", AS_OF.isoformat(), *extra],
        capture_output=True, text=True, timeout=120, env={**os.environ, "VNX_STATE_DIR": str(rc.parent), "VNX_PROJECT_ID": "p1"})
    return run, (json.loads(run.stdout) if "--json" in extra and run.returncode == 0 else None)


def _status(rc, **kw):
    run, data = _run(rc, "secondary", "--json")
    assert run.returncode == 0, run.stdout + run.stderr
    return {name: m["status"] for name, m in data["measures"].items()}, data["measures"]


def test_rising_adoption_without_a_favourable_verdict_fires_f1_and_f2_and_exits_0(rc):
    """Red run: on the old code `secondary` is `invalid choice` (exit 2) and no measure has a status."""
    _world(rc)
    status, m = _status(rc)
    assert status == {"F1": "fired", "F2": "fired", "F3": "clear"}
    assert m["F1"]["rise_pp"] == 20.0 and m["F1"]["verdict"] == "equivalent_within_5pp"
    assert (m["F2"]["chars_per_item_first"], m["F2"]["chars_per_item_last"], m["F2"]["drop_pct"]) == (100.0, 80.0, 20.0)
    run, _ = _run(rc, "secondary")
    assert run.returncode == 0 and "F1          fired" in run.stdout


def test_it_never_changes_the_verdict_the_exit_code_or_a_store_of_effect(rc):
    _world(rc)
    stores = [rc, rc.parent / "quality_intelligence.db"]

    def digest():
        return [hashlib.sha256(p.read_bytes()).hexdigest() for p in stores]

    before, hashes = _run(rc, "effect", "--json"), digest()
    _run(rc, "secondary")
    after = _run(rc, "effect", "--json")
    assert (before[0].returncode, before[0].stdout) == (after[0].returncode, after[0].stdout) and before[0].returncode == 0
    assert digest() == hashes


def test_a_dilution_that_does_not_follow_the_rise_or_a_flat_adoption_is_clear(rc):
    _world(rc, chars=(100, 100))
    assert _status(rc)[0]["F1"] == "fired" and _status(rc)[0]["F2"] == "clear"
    sec = importlib.import_module("intel_effect_secondary")
    flat = [{"week": "2026-W45", "offers": 100, "with_outcome": 100, "used": 30, "coverage_pct": 100.0, "evaluable": True},
            {"week": "2026-W46", "offers": 100, "with_outcome": 100, "used": 34, "coverage_pct": 100.0, "evaluable": True}]
    assert sec.f1(flat, "equivalent_within_5pp", "x").status == "clear"  # +4 pp is under the 5 pp rise
    assert sec.f1([{**flat[0], "used": 20}, flat[1]], "treatment_better", "x").status == "clear"  # a real effect explains it
    assert sec.f1([{**flat[0], "used": 20}, flat[1]], "inconclusive", "x").status == "fired"  # exactly +14 pp


def test_f3_fires_on_control_better_and_f1_follows_the_verdict(rc):
    _world(rc, treatment_ok=60)
    status, m = _status(rc)
    assert status["F3"] == "fired" and m["F3"]["verdict"] == "control_better" and status["F1"] == "fired"


def test_a_week_at_40_percent_coverage_is_not_evaluable_and_names_the_percentage(rc):
    _world(rc, cover=40)
    status, m = _status(rc)
    assert status["F1"] == status["F2"] == "not_evaluable" and "fewer than 2 evaluable weeks" in m["F1"]["reason"]
    assert [(w["coverage_pct"], w["evaluable"]) for w in m["F1"]["weeks"]] == [(40.0, False), (40.0, False)]
    assert "40% coverage" in _run(rc, "secondary")[0].stdout


def test_a_week_under_30_offers_with_an_outcome_is_not_evaluable_at_the_edge(rc, sec):
    def edge(n):
        return [{"week": f"2026-W4{i}", "offers": n, "with_outcome": n, "used": 10 * i, "coverage_pct": 100.0,
                 "evaluable": n >= 30} for i in (5, 6)]

    assert sec.f1(edge(30), "inconclusive", "x").status == "fired"
    assert sec.f1(edge(29), "inconclusive", "x").status == "not_evaluable"


@pytest.mark.parametrize("sql, measure", [
    ("UPDATE pattern_injection_outcome SET used = 2", "F1"),  # not a 0/1 outcome: no outcome row at all
    ("DROP TABLE pattern_injection_outcome", "F1"),
])
def test_unreadable_adoption_evidence_is_never_a_pass(rc, sql, measure):
    _world(rc)
    _exec(rc.parent / "quality_intelligence.db", sql, [()])
    status, _ = _status(rc)
    assert status[measure] == status["F2"] == "not_evaluable"


@pytest.mark.parametrize("bad", ["'many'", "-5"])
def test_an_unreadable_payload_value_makes_f2_not_evaluable_never_clear(rc, bad):
    _world(rc)
    _exec(rc, f"UPDATE intelligence_injections SET payload_chars = {bad} WHERE ab_arm = 'treatment' AND injected_at = ?",
          [(DAY1,)])
    status, m = _status(rc)
    assert status["F1"] == "fired" and status["F2"] == "not_evaluable" and "unreadable" in m["F2"]["reason"]


def test_below_the_floor_there_is_no_verdict_so_f1_f2_f3_are_not_evaluable_and_coverage_still_reports(rc):
    _qi(rc)
    _seed(rc, _balanced(199))
    status, m = _status(rc)
    assert [status[k] for k in ("F1", "F2", "F3")] == ["not_evaluable"] * 3 and "below_floor" in m["F3"]["reason"]
    assert _run(rc, "effect")[0].returncode == 3 and _run(rc, "secondary")[0].returncode == 0


def test_a_refused_cohort_makes_every_measure_not_evaluable(rc):
    _world(rc)
    _exec(rc, "DELETE FROM project_config WHERE project_id = 'p1'", [()])
    status, m = _status(rc)
    assert set(status.values()) == {"not_evaluable"} and "window_invalid" in m["F1"]["reason"]


def test_another_project_with_the_same_dispatch_ids_changes_nothing(rc):
    _world(rc)
    _, before = _status(rc)
    meta = rc.parent / "quality_intelligence.db"
    ids = _treatment(rc, DAY1) + _treatment(rc, W46)
    _exec(meta, "INSERT INTO dispatch_pattern_offered VALUES (?, 'pat', 'p2')", [(d,) for d in ids])
    _exec(meta, "INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id) VALUES (?, 'pat', 1, 'p2')",
          [(d,) for d in ids])
    _, after = _status(rc)
    assert after == before


def test_id_lists_go_to_the_stores_in_blocks_and_the_result_is_the_same(rc, sec, monkeypatch):
    _world(rc)
    week_of = {d: "2026-W45" for d in _treatment(rc, DAY1)} | {d: "2026-W46" for d in _treatment(rc, W46)}
    with closing(sqlite3.connect(str(rc.parent / "quality_intelligence.db"))) as qi:
        whole = sec.week_stats(qi, "p1", week_of)
        monkeypatch.setattr(sec, "BLOCK", 7)
        assert sec.week_stats(qi, "p1", week_of) == whole and whole[0]["offers"] == 100
    assert Fraction(whole[1]["used"], whole[1]["with_outcome"]) == Fraction(2, 5)


def test_the_same_file_for_both_stores_is_exit_1(rc):
    run = subprocess.run([sys.executable, os.path.join(_ROOT, "scripts", "intel_injection_join.py"), "secondary", "--rc", str(rc),
                          "--qi", str(rc), "--project-id", "p1"], capture_output=True, text=True, timeout=60)
    assert run.returncode == 1 and "same file" in run.stderr
