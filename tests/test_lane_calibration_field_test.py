"""Tests for the lane-calibration field-test (scripts/benchmark/field-tests).

This is the pytest wrapper around
scripts/benchmark/field-tests/runners/lane_calibration.py — the realistic-bench
field-test that feeds real field-tests task instructions through the
production smart-router (classify_dispatch -> resolve_tier_route) and checks
the result against the documented expected outcome in lane_calibration.yaml.

Covers: every case in the calibration table matches (regression guard against
routing drift), the runner's CLI exit code, and that a genuine mismatch is
actually detected (the fixture-comparison logic isn't a no-op).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "runners"
RUNNER_SCRIPT = RUNNER_DIR / "lane_calibration.py"

sys.path.insert(0, str(RUNNER_DIR))
import lane_calibration as lc  # noqa: E402


def test_calibration_yaml_and_tasks_yaml_are_consistent():
    tasks_by_id = lc.load_tasks_by_id()
    cases = lc.load_calibration_cases()
    assert cases, "lane_calibration.yaml has no cases"
    for case in cases:
        assert case["task_id"] in tasks_by_id, (
            f"{case['task_id']} in lane_calibration.yaml has no matching "
            "entry in tasks.yaml"
        )


@pytest.mark.parametrize(
    "case",
    lc.load_calibration_cases(),
    ids=lambda c: c["task_id"],
)
def test_case_matches_documented_expected_outcome(case):
    tasks_by_id = lc.load_tasks_by_id()
    result = lc.run_case(case, tasks_by_id)
    assert result.passed, (
        f"{result.task_id}: expected tier={result.expected_tier} "
        f"provider={result.expected_provider} lane={result.expected_lane}, "
        f"got tier={result.actual_tier} provider={result.actual_provider} "
        f"lane={result.actual_lane}"
    )


def test_run_all_returns_one_result_per_case():
    results = lc.run_all()
    cases = lc.load_calibration_cases()
    assert len(results) == len(cases)
    assert all(r.passed for r in results)


def test_mismatch_is_actually_detected():
    """Guard against the comparison silently always passing."""
    tasks_by_id = lc.load_tasks_by_id()
    case = dict(lc.load_calibration_cases()[0])
    case["expected_tier"] = "tier-does-not-exist"
    result = lc.run_case(case, tasks_by_id)
    assert result.passed is False


def test_cli_runs_clean_and_exits_zero():
    proc = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT)],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cases passed" in proc.stdout


def test_cli_json_output_is_valid():
    import json

    proc = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT), "--json"],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload, list) and payload
    assert all(row["passed"] for row in payload)
