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


# ── tier-zero env-branch coverage ───────────────────────────────────────────
# resolve_tier_route('tier-zero') returns different routes depending on whether
# DEEPSEEK_API_KEY is in the environment and whether the kimi CLI is on PATH
# (OI-1185: missing key and cooldown walk the SAME chain). The calibration
# corpus pins env={} + kimi-absent (see the runner), which exercises the codex
# terminal vangnet. These tests cover every branch explicitly with kimi mocked
# so the route is verified regardless of where the suite runs.


def test_tier_zero_without_deepseek_key_routes_to_codex(monkeypatch):
    """Without DEEPSEEK_API_KEY and without the kimi CLI, tier-zero -> codex."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    route = lc.resolve_tier_route("tier-zero", env={})
    assert route.tier == "tier-zero"
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"
    assert route.lane == "provider"


def test_tier_zero_without_key_but_kimi_present_routes_to_kimi(monkeypatch):
    """OI-1185: a missing key walks the SAME chain as cooldown — kimi is a
    regular step before the codex vangnet."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/kimi")
    route = lc.resolve_tier_route("tier-zero", env={})
    assert route.tier == "tier-zero"
    assert route.provider == "kimi"
    assert route.model == "kimi-k3"
    assert route.lane == "kimi_cli"


def test_tier_zero_with_deepseek_key_routes_to_deepseek():
    """With DEEPSEEK_API_KEY, tier-zero -> deepseek-harness / deepseek-v4-flash / claude_harness_keyed."""
    route = lc.resolve_tier_route("tier-zero", env={"DEEPSEEK_API_KEY": "sk-test"})
    assert route.tier == "tier-zero"
    assert route.provider == "deepseek-harness"
    assert route.model == "deepseek-v4-flash"
    assert route.lane == "claude_harness_keyed"
    # Fallback chain is kimi then codex (OI-1185: one chain for all causes).
    assert route.fallback is not None
    assert route.fallback.provider == "kimi"
    assert route.fallback.model == "kimi-k3"
    assert route.fallback.lane == "kimi_cli"
    assert route.fallback.fallback is not None
    assert route.fallback.fallback.provider == "codex"
    assert route.fallback.fallback.model == "gpt-5.5"
    assert route.fallback.fallback.lane == "provider"
