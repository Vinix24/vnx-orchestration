"""Effect PR-9: the A/B report delegates to the one implementation (cohort and estimator), no second query.

Two stores in the real shape: RC is ``init_schema`` plus the migrations to 0038, QI an empty store. Temp
stores only, never ~/.vnx-data.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from test_intel_effect_cohort import AS_OF, _ROOT, _balanced, _exec, _seed, rc

sys.path.insert(0, str(_ROOT / "scripts"))
import intelligence_ab_report as rep


@pytest.fixture
def qi(rc):
    return rc.parent / "quality_intelligence.db"


def test_the_two_store_fixture_gives_a_treatment_control_pair(rc, qi):
    _seed(rc, _balanced(200))
    report = rep.weekly_ab_lift(qi, rc, "p1", AS_OF)
    assert report["refusals"] == [] and report["verdict"] is not None
    [pair] = report["pairs"]
    assert (pair["week"], pair["task_class"]) == ("2026-W45", "coding_interactive")
    assert (pair["treatment_n"], pair["control_n"]) == (200, 200)
    assert pair["treatment_rate"] == pair["control_rate"] == 1.0 and pair["lift"] == 0.0


def test_a_row_with_a_null_arm_is_never_a_treatment_row(rc, qi):
    _seed(rc, _balanced(200))
    _exec(rc, "UPDATE intelligence_injections SET ab_arm = NULL WHERE id = (SELECT min(id) FROM intelligence_injections)", [()])
    report = rep.weekly_ab_lift(qi, rc, "p1", AS_OF)
    assert report["pairs"] == [] and report["verdict"] is None
    assert "arm_invalid" in [r["reason"] for r in report["refusals"]]
    assert "arm_invalid" in rep._render_markdown(report)


def test_below_the_floor_there_are_no_pairs_and_no_verdict(rc, qi):
    _seed(rc, _balanced(199))
    report = rep.weekly_ab_lift(qi, rc, "p1", AS_OF)
    assert report["pairs"] == [] and report["verdict"] is None
    assert [r["reason"] for r in report["refusals"]] == ["below_floor"]
    assert rep._render_markdown(report).startswith("No treatment/control pairs: no verdict.")


def test_another_project_never_enters_the_pairs(rc, qi):
    _seed(rc, _balanced(200))
    _seed(rc, _balanced(200, "p2"), pid="p2", state="expired", task="other_class")
    report = rep.weekly_ab_lift(qi, rc, "p1", AS_OF)
    assert [p["task_class"] for p in report["pairs"]] == ["coding_interactive"]
    assert report["pairs"][0]["control_rate"] == 1.0


def test_a_failure_lowers_the_rate_and_the_markdown_carries_the_pair(rc, qi):
    ids = _balanced(200)
    _seed(rc, ids)
    _exec(rc, "UPDATE dispatches SET state = 'expired' WHERE dispatch_id IN (SELECT dispatch_id FROM "
              "intelligence_injections WHERE ab_arm = 'control' ORDER BY id LIMIT 20)", [()])
    [pair] = rep.weekly_ab_lift(qi, rc, "p1", AS_OF)["pairs"]
    assert pair["control_rate"] == 0.9 and pair["treatment_rate"] == 1.0 and pair["lift"] == 0.1
    text = rep._render_markdown(rep.weekly_ab_lift(qi, rc, "p1", AS_OF))
    assert "| 2026-W45 | coding_interactive | 100.00% | 200 | 90.00% | 200 | +10.0% |" in text


def test_the_same_file_twice_and_an_unreadable_store_raise(rc, qi, tmp_path):
    with pytest.raises(ValueError):
        rep.weekly_ab_lift(rc, rc, "p1", AS_OF)
    with pytest.raises(RuntimeError):
        rep.weekly_ab_lift(tmp_path / "missing.db", rc, "p1", AS_OF)
    with pytest.raises(RuntimeError):
        rep.weekly_ab_lift(qi, tmp_path / "missing.db", "p1", AS_OF)
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    with pytest.raises(RuntimeError):
        rep.weekly_ab_lift(qi, bare, "p1", AS_OF)


def _cli(*args):
    return subprocess.run([sys.executable, str(_ROOT / "scripts" / "intelligence_ab_report.py"), *args],
                          capture_output=True, text=True, timeout=120, env={**os.environ, "VNX_PROJECT_ID": "p1"})


def test_the_cli_exits_0_with_pairs_3_when_refused_and_1_on_a_bad_store(rc, qi, tmp_path):
    _seed(rc, _balanced(200))
    ok = _cli("--qi", str(qi), "--rc", str(rc), "--as-of", AS_OF.isoformat())
    assert ok.returncode == 0 and "| 2026-W45 | coding_interactive |" in ok.stdout, ok.stdout + ok.stderr
    _exec(rc, "DELETE FROM intelligence_injections WHERE id IN (SELECT id FROM intelligence_injections LIMIT 5)", [()])
    refused = _cli("--qi", str(qi), "--rc", str(rc), "--as-of", AS_OF.isoformat())
    assert refused.returncode == 3 and "REFUSED" in refused.stdout
    assert _cli("--qi", str(qi), "--rc", str(tmp_path / "x.db")).returncode == 1
    assert _cli("--qi", str(qi), "--rc", str(rc), "--as-of", "yesterday").returncode == 2
