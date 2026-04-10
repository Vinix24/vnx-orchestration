"""F39 Level-2 Chain Replay Tests — pytest wrapper.

Runs all level2_*.json chain scenario fixtures against headless T0.
Gate requirement: ≥90% step accuracy across all chains (e.g. if 5 chains × 3 steps = 15 steps, ≥14 must be correct).

Each chain has 3 steps. Context from prior steps is injected into subsequent step prompts
so the LLM maintains memory of what it decided in the same chain.

Usage:
    pytest tests/f39/test_replay_level2.py -v
    pytest tests/f39/test_replay_level2.py -v --model haiku  # cheaper
    pytest tests/f39/test_replay_level2.py -v --dry-run      # no LLM calls

Set VNX_F39_MODEL env var to override default model.
Set VNX_F39_DRY_RUN=1 to skip LLM calls (useful for fixture validation).
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

from replay_harness import run_chain_replay, run_all_chain_replays, ChainReplayResult  # noqa: E402

_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
_LEVEL = 2
_STEP_ACCURACY_THRESHOLD = 0.9  # 90% step gate


def _get_model(request: pytest.FixtureRequest) -> str:
    cli_model = request.config.getoption("--model", default=None)
    env_model = os.environ.get("VNX_F39_MODEL")
    return cli_model or env_model or "haiku"  # Default to haiku for cost control


def _is_dry_run(request: pytest.FixtureRequest) -> bool:
    cli_dry = request.config.getoption("--dry-run", default=False)
    env_dry = os.environ.get("VNX_F39_DRY_RUN", "0") == "1"
    return cli_dry or env_dry


# ---------------------------------------------------------------------------
# Fixture: load all level-2 chain scenarios
# ---------------------------------------------------------------------------

def _collect_chain_scenarios() -> list[tuple[str, Path]]:
    """Collect all level-2 chain scenario fixtures, returning (name, path) pairs."""
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


_CHAIN_SCENARIOS = _collect_chain_scenarios()


# ---------------------------------------------------------------------------
# Individual chain tests (parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,scenario_path", _CHAIN_SCENARIOS, ids=[s[0] for s in _CHAIN_SCENARIOS])
def test_level2_chain_scenario(
    name: str,
    scenario_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Each chain scenario must achieve ≥90% step accuracy."""
    model = _get_model(request)
    dry_run = _is_dry_run(request)

    result = run_chain_replay(scenario_path, model=model, dry_run=dry_run)

    # Always fail on infrastructure errors
    if result.errors:
        pytest.fail(f"Infrastructure error in chain '{name}': {'; '.join(result.errors)}")

    if dry_run:
        pytest.skip(f"Dry-run: chain fixture validated, no LLM call ({result.scenario_name})")

    # Report per-step failures for diagnosis
    step_failures: list[str] = []
    for step in result.steps:
        if not step.match:
            errs = f" [{'; '.join(step.errors)}]" if step.errors else ""
            step_failures.append(
                f"  Step '{step.step_name}': expected={step.expected_decision} got={step.actual_decision}{errs}"
            )

    assert result.step_accuracy >= _STEP_ACCURACY_THRESHOLD, (
        f"Chain '{name}' step accuracy {result.step_accuracy:.0%} < {_STEP_ACCURACY_THRESHOLD:.0%} threshold "
        f"({sum(1 for s in result.steps if s.match)}/{len(result.steps)} steps correct)\n"
        + "\n".join(step_failures)
    )


# ---------------------------------------------------------------------------
# Aggregate gate test
# ---------------------------------------------------------------------------

def test_level2_aggregate_step_accuracy(request: pytest.FixtureRequest) -> None:
    """Level-2 gate: ≥90% of all steps across all chains must produce the correct decision.

    This test runs all 5 chain scenarios (15 total steps) in batch.
    """
    model = _get_model(request)
    dry_run = _is_dry_run(request)

    if dry_run:
        pytest.skip("Dry-run: skipping aggregate gate test")

    results = run_all_chain_replays(model=model)

    if not results:
        pytest.fail(f"No chain scenario fixtures found in {_SCENARIOS_DIR}")

    all_steps = [step for r in results for step in r.steps]
    if not all_steps:
        pytest.fail("No steps collected from chain scenarios")

    passed_steps = sum(1 for s in all_steps if s.match)
    total_steps = len(all_steps)
    step_accuracy = passed_steps / total_steps

    # Collect failure details
    failures: list[str] = []
    for r in results:
        for s in r.steps:
            if not s.match:
                errs = f" [{'; '.join(s.errors)}]" if s.errors else ""
                failures.append(
                    f"  Chain '{r.scenario_name}' / step '{s.step_name}'\n"
                    f"    Expected: {s.expected_decision}\n"
                    f"    Actual  : {s.actual_decision}\n"
                    f"    Output  : {s.actual_output[:200]}{errs}"
                )

    failure_detail = "\n".join(failures) if failures else ""

    assert step_accuracy >= _STEP_ACCURACY_THRESHOLD, (
        f"Level-{_LEVEL} chain step accuracy {step_accuracy:.0%} < {_STEP_ACCURACY_THRESHOLD:.0%} threshold "
        f"({passed_steps}/{total_steps} steps passed across {len(results)} chains)\n\nFailures:\n{failure_detail}"
    )
