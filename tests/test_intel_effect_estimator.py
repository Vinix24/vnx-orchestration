"""Effect PR-7: the estimator (decision 15), the verdict classes (decision 21), the balance check and ``effect``.

The stores are PR-6's fixture: ``init_schema`` plus the real migrations to 0038, arms stamped by the writer's
hash. The module is imported inside the tests, so on the old code the CLI tests fail on behaviour. Temp stores
only, never ~/.vnx-data.
"""
from __future__ import annotations

import importlib
import itertools
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

import intelligence_selector as sel
from intel_effect_cohort import CohortDispatch
from test_intel_effect_cohort import AS_OF, DAY1, DECISION_7, WINDOW, _ROOT, _balanced, _exec, _seed, rc

W46 = "2026-11-09T00:00:00.000Z"
_ROW1 = "WHERE id = (SELECT min(id) FROM intelligence_injections)"


@pytest.fixture
def es():
    return importlib.import_module("intel_effect_estimator")


def _cell(rc, prefix, task, at, **per_arm):
    """Per arm ``(n, successes)`` door dispatches the writer's hash puts in that arm; failures alternate states."""
    for arm, (n, a) in per_arm.items():
        ids = (f"dlv-{prefix}{arm[0]}-{i:05d}" for i in itertools.count())
        ids = list(itertools.islice((d for d in ids if sel.derive_arm(WINDOW, "p1", d) == arm), n))
        for chunk, state in ((ids[:a], "completed"), (ids[a::2], "expired"), (ids[a + 1::2], "dead_letter")):
            _seed(rc, chunk, task=task, at=at, state=state)


def _effect(rc, *extra):
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "intel_injection_join.py"), "effect", "--rc", str(rc), "--qi",
         str(rc.parent / "quality_intelligence.db"), "--project-id", "p1", "--as-of", AS_OF.isoformat(), *extra],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "VNX_STATE_DIR": str(rc.parent), "VNX_PROJECT_ID": "p1"})


def test_the_cli_gives_n_rates_the_stratified_interval_and_a_verdict(rc):
    _cell(rc, "a", "coding_interactive", DAY1, control=(200, 195), treatment=(200, 196))
    run, data = _effect(rc), json.loads(_effect(rc, "--json").stdout)
    assert run.returncode == data["exit_code"] == 0 and data["refusals"] == [], run.stdout + run.stderr
    assert {arm: (c["n"], c["successes"], c["success_rate"]) for arm, c in data["arms"].items()} == {
        "control": (200, 195, 0.975), "treatment": (200, 196, 0.98)}
    assert data["estimate"]["d_pp"] == 0.5 and abs(data["estimate"]["half_width_pp"] - 3.47) <= 0.3
    assert data["verdict"] == "equivalent_within_5pp" and "VERDICT equivalent_within_5pp" in run.stdout
    assert "# d +0.50 pp, 95% interval [-2.94, +3.94] pp" in run.stdout


def test_199_per_arm_is_exit_3_without_an_estimate_a_rate_or_a_verdict(rc):
    _seed(rc, _balanced(199))
    run, data = _effect(rc), json.loads(_effect(rc, "--json").stdout)
    assert run.returncode == data["exit_code"] == 3, run.stdout + run.stderr
    assert data["estimate"] is data["verdict"] is data["verdict_flags"] is None
    assert [c["success_rate"] for c in data["arms"].values()] == [None, None]
    assert [r["reason"] for r in data["refusals"]] == ["below_floor"] and data["floor_rule"] == DECISION_7
    assert "VERDICT" not in run.stdout and "# d " not in run.stdout and DECISION_7 in run.stdout.splitlines()


def test_min_per_arm_is_no_flag_and_the_estimator_reads_no_environment(rc):
    run = _effect(rc, "--min-per-arm", "10")
    assert run.returncode == 2 and "unrecognized arguments: --min-per-arm 10" in run.stderr, run.stderr
    source = importlib.import_module("intel_effect_estimator").__file__
    text = open(source, encoding="utf-8").read()
    assert "environ" not in text and "getenv" not in text and "MIN_PER_ARM =" not in text


def test_a_known_answer_is_within_0_1_pp_and_the_raw_difference_stands_beside_it(rc):
    """Simpson: pooled the treatment arm looks 0.75 pp worse; per stratum (MH weights 50 and 75) it is +0.8 pp."""
    _cell(rc, "a", "coding_interactive", DAY1, control=(100, 90), treatment=(100, 95))
    _cell(rc, "b", "coding_sql", W46, control=(300, 297), treatment=(100, 97))
    data = json.loads(_effect(rc, "--json").stdout)
    assert data["exit_code"] == 0 and abs(data["estimate"]["d_pp"] - 0.8) <= 0.1, data["refusals"]
    assert data["estimate"]["raw_d_pp"] == -0.75 and data["verdict"] == "equivalent_within_5pp"
    assert (data["strata"]["total"], data["strata"]["dropped"], data["strata"]["dropped_dispatches"]) == (2, 0, 0)


def test_the_half_width_at_200_per_arm_and_0_977_and_the_iso_week_strata(es):
    est = es.estimate({("coding_sql", "2026-W45"): {"control": [200, 195.4], "treatment": [200, 195.4]}})
    assert est.d == 0 and abs(est.half_width * 100 - 3.47) <= 0.3 and es.ac_variance(10, 10) > 0
    at = datetime(2027, 1, 1, tzinfo=timezone.utc)  # %W says 2027-W00; the ISO week is 2026-W53
    units = [CohortDispatch(did, arm, task, at, "success", True)
             for did, arm, task in (("a", "control", ""), ("b", "treatment", None), ("c", "control", "  "))]
    assert {key: cell["control"][0] for key, cell in es.stratify(units).items()} == {(None, "2026-W53"): 2}


@pytest.mark.parametrize("dropped, refused", [(50, False), (51, True)])
def test_a_stratum_with_an_empty_arm_drops_out_and_coverage_refuses_above_10_percent(rc, es, dropped, refused):
    _cell(rc, "a", "coding_interactive", DAY1, control=(225, 225), treatment=(225, 225))
    _cell(rc, "x", "research_structured", DAY1, treatment=(dropped, dropped))  # 50 of 500 is exactly 10 %
    effect = es.build_effect(rc, "p1", AS_OF)
    assert (len(effect.strata), len(effect.kept), effect.dropped) == (2, 1, dropped)
    assert [r for r, _ in effect.refusals] == (["strata_coverage"] if refused else [])
    assert (effect.estimate is None) == refused and es.as_dict(effect)["strata"]["dropped_pct"] == round(
        dropped / (450 + dropped) * 100, 2)


@pytest.mark.parametrize("lower, upper, verdict", [
    (0.001, 0.03, "treatment_better"), (0.01, 0.07, "treatment_better"), (-0.03, -0.001, "control_better"),
    (-0.049, 0.049, "equivalent_within_5pp"), (0.0, 0.04, "equivalent_within_5pp"),
    (-0.06, 0.02, "inconclusive"), (-0.02, 0.05, "inconclusive"), (-0.05, 0.0, "inconclusive"),
])
def test_the_verdict_classes_follow_decision_21(es, lower, upper, verdict):
    flags = es.verdict_flags(lower, upper)
    assert es.verdict_class(flags) == verdict and [k for k, v in flags.items() if v] == [verdict]


def test_the_balance_counts_per_arm_unknown_is_never_a_value_and_never_moves_the_verdict(rc, es):
    _seed(rc, ids := _balanced(200))
    _seed(rc, ["dlv-open"], state="proposed")
    before = es.as_dict(es.build_effect(rc, "p1", AS_OF))
    ctl, trt = ([d for d in ids if sel.derive_arm(WINDOW, "p1", d) == arm] for arm in ("control", "treatment"))
    _exec(rc, "UPDATE dispatches SET terminal_id = ?, track = ? WHERE dispatch_id = ? AND project_id = 'p1'",
          [("T1", "A", d) for d in ctl] + [("T2", None, d) for d in trt] + [("T9", "Z", "dlv-open")])
    _exec(rc, "UPDATE dispatches SET terminal_id = ? WHERE dispatch_id = ? AND project_id = 'p1'",
          [(None, ctl[0]), ("", ctl[1]), ("  ", trt[0]), (b"\x00", trt[1])])
    after = es.as_dict(es.build_effect(rc, "p1", AS_OF))
    assert {k: v for k, v in after.items() if k != "balance"} == {k: v for k, v in before.items() if k != "balance"}
    assert after["balance"] == {
        "control": {"terminal_id": {"counts": {"T1": 198}, "unknown": 2}, "track": {"counts": {"A": 200}, "unknown": 0}},
        "treatment": {"terminal_id": {"counts": {"T2": 198}, "unknown": 2}, "track": {"counts": {}, "unknown": 200}}}
    _seed(rc, ids, pid="p2")  # the same ids in a second project, on other terminals
    _exec(rc, "UPDATE dispatches SET terminal_id = 'T7', track = 'B' WHERE project_id = 'p2'", [()])
    assert es.as_dict(es.build_effect(rc, "p1", AS_OF)) == after


@pytest.mark.parametrize("reason, sql", [
    ("unknown_states", "UPDATE dispatches SET state = 'paused' WHERE dispatch_id = 'dlv-00000'"),
    ("arm_invalid", f"UPDATE intelligence_injections SET ab_arm = 'placebo' {_ROW1}"),
    ("window_invalid", "DELETE FROM project_config WHERE project_id = 'p1'"),
])
def test_every_cohort_refusal_is_a_refusal_here_without_an_estimate(rc, es, reason, sql):
    _seed(rc, _balanced(200))
    _exec(rc, sql, [()])
    effect = es.build_effect(rc, "p1", AS_OF)
    assert effect.exit_code == 3 and effect.refusals[0][0] == reason and effect.estimate is None
    assert es.as_dict(effect)["verdict"] is None
