"""F39 Level-3 Edge Case Replay Tests — pytest wrapper.

Runs all level3_*.json edge case scenario fixtures against headless T0.
Gate requirement: ≥80% correct decisions (7/9 scenarios must match expected).

Level-3 scenarios require complex multi-source reasoning:
- Contradictory evidence (JSON says pass, text says fail)
- Trust verification (worker self-report vs git log)
- Unknown terminal detection
- Risk advisory override of clean surface appearance
- Lease conflict detection before dispatch
- Gate discipline enforcement (codex gate required)
- Duplicate receipt detection
- Scope drift detection with open item creation
- Dependency graph enforcement

The lower threshold (80% vs 90%) reflects the genuine difficulty of these scenarios.

Usage:
    pytest tests/f39/test_replay_level3.py -v
    pytest tests/f39/test_replay_level3.py -v --model haiku
    pytest tests/f39/test_replay_level3.py -v --dry-run

Set VNX_F39_MODEL env var to override default model.
Set VNX_F39_DRY_RUN=1 to skip LLM calls.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make scripts/f39 importable
_F39_DIR = Path(__file__).resolve().parents[2] / "scripts" / "f39"
sys.path.insert(0, str(_F39_DIR))

from replay_harness import run_replay, run_all_replays, ReplayResult  # noqa: E402

_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
_LEVEL = 3
_PASS_RATE_THRESHOLD = 0.8  # 80% gate — edge cases are harder


def _get_model(request: pytest.FixtureRequest) -> str:
    cli_model = request.config.getoption("--model", default=None)
    env_model = os.environ.get("VNX_F39_MODEL")
    return cli_model or env_model or "haiku"  # Default to haiku for cost control


def _is_dry_run(request: pytest.FixtureRequest) -> bool:
    cli_dry = request.config.getoption("--dry-run", default=False)
    env_dry = os.environ.get("VNX_F39_DRY_RUN", "0") == "1"
    return cli_dry or env_dry


# ---------------------------------------------------------------------------
# Fixture: load all level-3 scenarios
# ---------------------------------------------------------------------------

def _collect_scenarios() -> list[tuple[str, Path]]:
    """Collect all level-3 scenario fixtures, returning (name, path) pairs."""
    fixtures = sorted(_SCENARIOS_DIR.glob(f"level{_LEVEL}_*.json"))
    result = []
    for path in fixtures:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data.get("name", path.stem)
        except Exception:
            name = path.stem
        result.append((name, path))
    return result


_SCENARIOS = _collect_scenarios()


# ---------------------------------------------------------------------------
# Individual scenario tests (parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,scenario_path", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_level3_scenario(
    name: str,
    scenario_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Each edge case scenario must produce the expected decision."""
    model = _get_model(request)
    dry_run = _is_dry_run(request)

    result = run_replay(scenario_path, model=model, dry_run=dry_run)

    # Always fail on infrastructure errors
    if result.actual_decision == "ERROR" and result.errors:
        pytest.fail(f"Infrastructure error: {'; '.join(result.errors)}")

    if dry_run:
        pytest.skip(f"Dry-run: edge case fixture validated, no LLM call ({result.scenario_name})")

    assert result.match, (
        f"Decision mismatch for edge case '{name}'\n"
        f"  Expected : {result.expected_decision}\n"
        f"  Actual   : {result.actual_decision}\n"
        f"  Reason   : {result.actual_output[:400]}\n"
        f"  Errors   : {result.errors}"
    )


# ---------------------------------------------------------------------------
# Aggregate gate test
# ---------------------------------------------------------------------------

def test_level3_aggregate_pass_rate(request: pytest.FixtureRequest) -> None:
    """Level-3 gate: ≥80% of all edge case scenarios must produce the correct decision.

    This test runs all 9 scenarios in batch and verifies the pass rate.
    The lower threshold (vs 90% for level-1) reflects the genuine difficulty
    of scenarios requiring cross-source reasoning and adversarial inputs.
    """
    model = _get_model(request)
    dry_run = _is_dry_run(request)

    if dry_run:
        pytest.skip("Dry-run: skipping aggregate gate test")

    results = run_all_replays(level=_LEVEL, model=model)

    if not results:
        pytest.fail(f"No scenario fixtures found in {_SCENARIOS_DIR}")

    passed = sum(1 for r in results if r.match)
    total = len(results)
    pass_rate = passed / total

    # Collect failure details
    failures: list[str] = []
    for r in results:
        if not r.match:
            errs = f" [{'; '.join(r.errors)}]" if r.errors else ""
            failures.append(
                f"  FAIL: {r.scenario_name}\n"
                f"    Expected: {r.expected_decision}\n"
                f"    Actual  : {r.actual_decision}\n"
                f"    Output  : {r.actual_output[:200]}{errs}"
            )

    failure_detail = "\n".join(failures) if failures else ""

    assert pass_rate >= _PASS_RATE_THRESHOLD, (
        f"Level-{_LEVEL} edge case pass rate {pass_rate:.0%} < {_PASS_RATE_THRESHOLD:.0%} threshold "
        f"({passed}/{total} passed)\n\nFailures:\n{failure_detail}"
    )
