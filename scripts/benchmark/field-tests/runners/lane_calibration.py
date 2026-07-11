"""lane_calibration.py — field-test: does the smart router pick the right lane
for realistic field-tests tasks?

Feeds each case's REAL instruction.md text + target_loc (from tasks.yaml)
through the production routing pipeline:

    providers.smart_router.cost_tier.classify_dispatch(...) -> tier
    providers.smart_router.tier_routing.resolve_tier_route(tier) -> provider/lane

and compares the result against the documented expected outcome in
lane_calibration.yaml. No subprocess dispatch, no network call, no model
spawn — pure classifier logic, so this runs in well under a second and never
touches external credits.

A mismatch means routing behavior drifted for one of these realistic tasks
since the calibration table was captured — investigate before updating the
table (see lane_calibration.yaml header for what "known miscalibration"
notes mean vs. genuine drift).

Usage:
    python3 lane_calibration.py            # human-readable table, exit 1 on mismatch
    python3 lane_calibration.py --json     # machine-readable result list
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

HERE = Path(__file__).resolve().parent
FIELD_TESTS = HERE.parent
REPO_ROOT = FIELD_TESTS.parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
from providers.smart_router.cost_tier import classify_dispatch  # noqa: E402
from providers.smart_router.tier_routing import resolve_tier_route  # noqa: E402


@dataclass
class CalibrationResult:
    task_id: str
    passed: bool
    expected_tier: str
    actual_tier: str
    expected_provider: str
    actual_provider: str
    expected_lane: str
    actual_lane: str
    note: Optional[str] = None


def load_tasks_by_id(field_tests_dir: Path = FIELD_TESTS) -> dict:
    cfg = yaml.safe_load((field_tests_dir / "tasks.yaml").read_text(encoding="utf-8"))
    return {t["id"]: t for t in cfg["tasks"]}


def load_calibration_cases(field_tests_dir: Path = FIELD_TESTS) -> list[dict]:
    cfg = yaml.safe_load(
        (field_tests_dir / "lane_calibration.yaml").read_text(encoding="utf-8")
    )
    return cfg["cases"]


def run_case(case: dict, tasks_by_id: dict, field_tests_dir: Path = FIELD_TESTS) -> CalibrationResult:
    task = tasks_by_id[case["task_id"]]
    instruction = (field_tests_dir / task["folder"] / "instruction.md").read_text(
        encoding="utf-8"
    )
    file_paths = [None] * case["n_files"]
    loc_estimate = task["target_loc"]

    actual_tier = classify_dispatch({"instruction": instruction}, file_paths, loc_estimate)
    route = resolve_tier_route(actual_tier, env={})

    passed = (
        actual_tier == case["expected_tier"]
        and route.provider == case["expected_provider"]
        and route.lane == case["expected_lane"]
    )
    return CalibrationResult(
        task_id=case["task_id"],
        passed=passed,
        expected_tier=case["expected_tier"],
        actual_tier=actual_tier,
        expected_provider=case["expected_provider"],
        actual_provider=route.provider,
        expected_lane=case["expected_lane"],
        actual_lane=route.lane,
        note=case.get("note"),
    )


def run_all(field_tests_dir: Path = FIELD_TESTS) -> list[CalibrationResult]:
    tasks_by_id = load_tasks_by_id(field_tests_dir)
    cases = load_calibration_cases(field_tests_dir)
    return [run_case(c, tasks_by_id, field_tests_dir) for c in cases]


def _print_table(results: list[CalibrationResult]) -> None:
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.task_id}")
        print(f"       tier:     expected={r.expected_tier:<10} actual={r.actual_tier}")
        print(f"       provider: expected={r.expected_provider:<10} actual={r.actual_provider}")
        print(f"       lane:     expected={r.expected_lane:<16} actual={r.actual_lane}")
        if r.note:
            print(f"       note: {r.note.strip()}")
    n_pass = sum(1 for r in results if r.passed)
    print(f"\n{n_pass}/{len(results)} lane-calibration cases passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    results = run_all()

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        _print_table(results)

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
