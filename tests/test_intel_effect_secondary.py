"""Effect PR-8: the falsifications F1-F3 with their coverage and the per-arm CQS and open items (plan decision 19).

Stores are PR-6's RC fixture (real migrations to 0038) plus a QI built here with the columns the measures read.
The module is imported inside the tests, so on the old code the CLI tests fail on behaviour. Temp stores only.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

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


_META = ("CREATE TABLE dispatch_metadata (id INTEGER PRIMARY KEY, dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL, "
         "cqs REAL, open_items_created INTEGER, open_items_resolved INTEGER, UNIQUE (project_id, dispatch_id))")


def _meta(rc, cover=(100, 100), cqs=(80.0, 60.0), oi=((2, 1), (4, 3)), pid="p1"):
    """dispatch_metadata rows for the first ``cover`` % (control, treatment) of the dispatches of each arm."""
    path = rc.parent / "quality_intelligence.db"
    _exec(path, _META, [()])
    with closing(sqlite3.connect(str(rc))) as conn:
        arms = [[r[0] for r in conn.execute("SELECT dispatch_id FROM intelligence_injections WHERE project_id = 'p1' "
                                            "AND ab_arm = ? ORDER BY id", (arm,))] for arm in ("control", "treatment")]
    for ids, pct, score, (made, shut) in zip(arms, cover, cqs, oi):
        _exec(path, "INSERT INTO dispatch_metadata (dispatch_id, project_id, cqs, open_items_created, open_items_resolved) "
                    "VALUES (?, ?, ?, ?, ?)", [(d, pid, score, made, shut) for d in ids[:len(ids) * pct // 100]])


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


def _world(rc, adoption=(20, 40), cover=100, chars=(100, 80), treatment_ok=98, size=100, control_ok=98):
    """Two weeks, ``size`` per arm each (200 per arm, the floor, at 100), treatment adoption and chars per item per week."""
    _qi(rc)
    for prefix, at, per, cpi in (("a", DAY1, adoption[0], chars[0]), ("b", W46, adoption[1], chars[1])):
        _cell(rc, prefix, "coding_interactive", at, control=(size, min(control_ok, size - 2)), treatment=(size, min(treatment_ok, size - 2)))
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


def _f(status):
    return {k: status[k] for k in ("F1", "F2", "F3")}


def test_rising_adoption_without_a_favourable_verdict_fires_f1_and_f2_and_exits_0(rc):
    """Red run: on the old code `secondary` is `invalid choice` (exit 2) and no measure has a status."""
    _world(rc)
    status, m = _status(rc)
    assert _f(status) == {"F1": "fired", "F2": "fired", "F3": "clear"}
    assert m["F1"]["rise_pp"] == 20.0 and m["F1"]["verdict"] == "equivalent_within_5pp"
    assert (m["F2"]["chars_per_item_first"], m["F2"]["chars_per_item_last"], m["F2"]["drop_pct"]) == (100.0, 80.0, 20.0)
    run, _ = _run(rc, "secondary")
    assert run.returncode == 0 and "F1          fired" in run.stdout


def test_f2_stands_on_the_adoption_rise_not_on_the_verdict(rc):
    _world(rc, control_ok=60)
    status, m = _status(rc)
    assert m["F1"]["verdict"] == "treatment_better" and _f(status) == {"F1": "clear", "F2": "fired", "F3": "clear"}


def test_f2_fires_without_any_verdict_when_the_weeks_are_evaluable(rc):
    _world(rc, adoption=(8, 16), size=40)
    status, m = _status(rc)
    assert "verdict" not in m["F1"] and _f(status) == {"F1": "not_evaluable", "F2": "fired", "F3": "not_evaluable"}
    assert "below_floor" in m["F1"]["reason"] and m["F1"]["rise_pp"] == 20.0


def test_a_dilution_of_5_percent_is_clear_on_the_same_adoption(rc):
    _world(rc, chars=(100, 95))
    assert _status(rc)[0]["F2"] == "clear"

def test_a_zero_first_week_payload_is_not_evaluable_and_does_not_end_the_command(rc):
    _world(rc)
    _exec(rc, "UPDATE intelligence_injections SET payload_chars = 0, items_injected = 3 WHERE ab_arm = 'treatment' "
              "AND injected_at = ?", [(DAY1,)])
    status, m = _status(rc)
    assert (status["F1"], status["F2"]) == ("fired", "not_evaluable")
    assert m["F2"]["reason"] == "zero characters per item in the first evaluable week"


@pytest.mark.parametrize("cover, items", [(0, 2), (100, 0)])
def test_a_zero_denominator_is_not_evaluable_never_an_exit(rc, cover, items):
    _world(rc, cover=cover)
    _exec(rc, "UPDATE intelligence_injections SET items_injected = ? WHERE ab_arm = 'treatment' AND injected_at = ?",
          [(items, DAY1)])
    status, _ = _status(rc)
    assert status["F2"] == "not_evaluable" and (status["F1"] == "not_evaluable") == (cover == 0)


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


def test_cqs_and_open_items_report_the_mean_per_arm_with_coverage(rc):
    """Red run: on the old code `secondary --json` has no `cqs` key at all, so the status lookup raises KeyError."""
    _world(rc)
    _meta(rc)
    status, m = _status(rc)
    assert (status["cqs"], status["open_items"]) == ("evaluated", "evaluated")
    control = m["cqs"]["arms"]["control"]
    assert (control["with_value"], control["coverage_pct"], control["means"]) == (control["resolved"], 100.0, 80.0)
    assert control["resolved"] > 0 and m["cqs"]["arms"]["treatment"]["means"] == 60.0
    assert m["open_items"]["arms"]["treatment"]["means"] == {"created": 4.0, "resolved": 3.0}
    assert "means 80.0" in _run(rc, "secondary")[0].stdout


def test_zero_metadata_rows_is_not_evaluable_with_zero_coverage(rc):
    """Red run: on the old code there is no `cqs` measure to be not_evaluable."""
    _world(rc)
    _meta(rc, cover=(0, 0))
    status, m = _status(rc)
    assert status["cqs"] == status["open_items"] == "not_evaluable" and "dekking 0 %" in m["cqs"]["reason"]
    assert m["cqs"]["arms"]["control"]["coverage_pct"] == 0.0


def test_coverage_below_80_percent_in_either_arm_is_not_evaluable_and_names_both_percentages(rc):
    _world(rc)
    _meta(rc, cover=(60, 100))
    status, m = _status(rc)
    arms = m["cqs"]["arms"]
    assert status["cqs"] == status["open_items"] == "not_evaluable" and arms["control"]["means"] is None
    assert arms["treatment"]["coverage_pct"] == 100.0 and arms["control"]["coverage_pct"] < 80
    assert "control" in m["cqs"]["reason"] and "treatment 100 %" in m["cqs"]["reason"]


def test_the_coverage_edge_at_80_percent_is_evaluable_and_just_under_is_not(rc, sec):
    def measure(have):
        ids = {"control": [f"c{i}" for i in range(10)], "treatment": [f"t{i}" for i in range(10)]}
        with closing(sqlite3.connect(":memory:")) as qi:
            qi.execute(_META)
            qi.executemany("INSERT INTO dispatch_metadata (dispatch_id, project_id, cqs) VALUES (?, 'p1', 50)",
                           [(d,) for d in ids["control"][:have] + ids["treatment"]])
            return sec._per_arm("cqs", qi, sec._CQS_SQL, "p1", ids, sec._mean_cqs)

    assert measure(8).status == "evaluated" and measure(7).status == "not_evaluable"


@pytest.mark.parametrize("sql", ["DROP TABLE dispatch_metadata", "ALTER TABLE dispatch_metadata DROP COLUMN open_items_created"])
def test_a_missing_metadata_table_or_column_is_not_evaluable_with_the_reason(rc, sql):
    _world(rc)
    _meta(rc)
    _exec(rc.parent / "quality_intelligence.db", sql, [()])
    status, m = _status(rc)
    assert status["open_items"] == "not_evaluable" and "source unreadable" in m["open_items"]["reason"]
    assert status["cqs"] == ("not_evaluable" if "DROP TABLE" in sql else "evaluated")
    assert status["F1"] == "fired"  # the other measures do not follow


@pytest.mark.parametrize("bad, column", [("'high'", "cqs"), ("NULL", "cqs"), ("-1", "cqs"), ("-3", "open_items_created"),
                                         ("'x'", "open_items_resolved"), ("NULL", "open_items_resolved")])
def test_an_unreadable_value_counts_in_the_denominator_not_in_the_mean(rc, bad, column):
    _world(rc)
    _meta(rc)
    _exec(rc.parent / "quality_intelligence.db", f"UPDATE dispatch_metadata SET {column} = {bad} WHERE id % 2 = 0", [()])
    status, m = _status(rc)
    name = "cqs" if column == "cqs" else "open_items"
    control = m[name]["arms"]["control"]
    assert status[name] == "not_evaluable" and "dekking" in m[name]["reason"]
    assert control["with_value"] < control["resolved"] and control["means"] is None


def test_one_unreadable_value_in_a_covered_arm_leaves_the_mean_over_the_readable_rows(rc):
    _world(rc)
    _meta(rc)
    _exec(rc.parent / "quality_intelligence.db", "UPDATE dispatch_metadata SET cqs = 'high' WHERE id = 1", [()])
    arm = _status(rc)[1]["cqs"]["arms"]["control"]
    assert (arm["resolved"] - arm["with_value"], arm["means"]) == (1, 80.0)


def test_metadata_of_another_project_is_not_counted(rc):
    _world(rc)
    _meta(rc, pid="p2")
    status, m = _status(rc)
    assert status["cqs"] == "not_evaluable" and "dekking 0 %" in m["cqs"]["reason"]


def test_a_cohort_refusal_makes_cqs_and_open_items_not_evaluable(rc):
    _world(rc)
    _meta(rc)
    _exec(rc, "DELETE FROM project_config WHERE project_id = 'p1'", [()])
    status, m = _status(rc)
    assert status["cqs"] == status["open_items"] == "not_evaluable" and "window_invalid" in m["cqs"]["reason"]


def test_cqs_rows_never_change_the_verdict_or_the_exit_code_of_effect(rc):
    _world(rc)
    before = _run(rc, "effect", "--json")[0]
    _meta(rc)
    after = _run(rc, "effect", "--json")[0]
    assert (before.returncode, before.stdout) == (after.returncode, after.stdout)
    assert _run(rc, "secondary")[0].returncode == 0


def test_the_secondary_help_names_cqs_and_open_items_and_what_it_reads():
    run = subprocess.run([sys.executable, os.path.join(_ROOT, "scripts", "intel_injection_join.py"), "secondary", "--help"],
                         capture_output=True, text=True, timeout=60)
    text = " ".join(run.stdout.split())
    assert run.returncode == 0 and "CQS" in text and "open items" in text and "dispatch_metadata" in text
    assert "not read" not in text
