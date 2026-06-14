from __future__ import annotations

import copy
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .models import ChainScenario, ChainStep, ChainStepResult, ChainReplayResult
from .prefilter import _code_prefilter
from .single_replay import (
    _call_claude,
    _extract_tokens_from_stream,
    _parse_decision,
    _fixture_matches_mode,
    _REPO_ROOT,
    _SCENARIOS_DIR,
)

log = logging.getLogger(__name__)

from f39_context_assembler import (  # noqa: E402
    assemble_t0_context,
    _DEFAULT_FEATURE_PLAN,
    _DEFAULT_SKILL,
    _DEFAULT_CLAUDE_MD,
)
from decision_parser import collect_text_from_stream as _collect_text_from_stream  # noqa: E402


def _apply_state_delta(state: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge delta into state, returning a new state dict."""
    result = copy.deepcopy(state)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _apply_state_delta(result[key], value)
        else:
            result[key] = value
    return result


def _build_prior_decisions_section(step_results: list[ChainStepResult]) -> str:
    """Build a prompt section summarising prior T0 decisions in this chain."""
    if not step_results:
        return ""
    lines = [
        "# SECTION 6: Prior T0 Decisions in This Chain",
        "",
        "You have already processed the following steps in this multi-step chain.",
        "Use these prior decisions as context — they reflect your earlier reasoning.",
        "",
    ]
    for i, sr in enumerate(step_results, 1):
        lines.append(f"Step {i} — {sr.step_name}:")
        lines.append(f"  Decision     : {sr.actual_decision}")
        excerpt = sr.actual_output[:300].replace("\n", " ")
        lines.append(f"  Output excerpt: {excerpt}")
        lines.append("")
    return "\n".join(lines)


def _load_chain_scenario(scenario_path: Path) -> ChainScenario | None:
    """Load and validate a chain scenario fixture."""
    try:
        data: dict[str, Any] = json.loads(scenario_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[chain] Failed to load {scenario_path}: {exc}", file=sys.stderr)
        return None

    steps_raw = data.get("steps", [])
    steps: list[ChainStep] = []
    for s in steps_raw:
        steps.append(ChainStep(
            step_name=s.get("step_name", "unnamed"),
            receipt=s.get("receipt", {}),
            state_delta=s.get("state_delta", {}),
            expected_decision=s.get("expected_decision", "UNKNOWN").upper(),
            expected_next_action=s.get("expected_next_action", ""),
        ))

    return ChainScenario(
        name=data.get("name", scenario_path.stem),
        level=data.get("level", 2),
        description=data.get("description", ""),
        initial_state=data.get("initial_state", {}),
        steps=steps,
    )


def _assemble_chain_step_prompt(
    step: ChainStep,
    current_state: dict[str, Any],
    step_results: list[ChainStepResult],
    step_errors: list[str],
) -> str:
    """Assemble prompt for a chain step, injecting prior decisions."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(current_state, tmp)
        tmp_state_path = Path(tmp.name)
    try:
        base_prompt = assemble_t0_context(
            state_path=tmp_state_path,
            receipt=step.receipt,
            feature_plan_path=_DEFAULT_FEATURE_PLAN if _DEFAULT_FEATURE_PLAN.exists() else Path("/dev/null"),
            skill_path=_DEFAULT_SKILL,
            claude_md_path=_DEFAULT_CLAUDE_MD,
        )
    except Exception as exc:
        step_errors.append(f"Context assembly failed: {exc}")
        base_prompt = ""
    finally:
        try:
            os.unlink(tmp_state_path)
        except OSError as exc:
            log.debug("Failed to unlink tmp state path: %s", exc)

    prior_section = _build_prior_decisions_section(step_results)
    return base_prompt + "\n\n" + prior_section if prior_section else base_prompt


def _run_chain_step_prefilter(
    chain: ChainScenario,
    step: ChainStep,
    current_state: dict[str, Any],
    step_start_ms: int,
    dry_run: bool = False,
) -> ChainStepResult | None:
    """Run prefilter for a chain step. Returns result if deterministic, else None.

    Dry-run mode skips prefilter entirely so LLM dry-run output is produced —
    matches pre-refactor behavior (replay_harness.py:434 `not dry_run` guard).
    """
    if dry_run:
        return None
    prefilter_decision = _code_prefilter(step.receipt, current_state)
    if prefilter_decision is None:
        return None

    step_elapsed = int(time.monotonic() * 1000) - step_start_ms
    match = prefilter_decision == step.expected_decision
    status = "PASS" if match else "FAIL"
    print(
        f"[chain] {status}: {chain.name}/{step.step_name} "
        f"(expected={step.expected_decision}, actual={prefilter_decision}, "
        f"prefilter=true, {step_elapsed}ms)",
        file=sys.stderr,
        flush=True,
    )
    return ChainStepResult(
        step_name=step.step_name,
        expected_decision=step.expected_decision,
        actual_decision=prefilter_decision,
        match=match,
        actual_output=f"[prefilter: {prefilter_decision}]",
        token_cost=0,
        duration_ms=step_elapsed,
        errors=[],
    )


def _run_chain_step_llm(
    chain: ChainScenario,
    step: ChainStep,
    current_state: dict[str, Any],
    step_results: list[ChainStepResult],
    model: str,
    timeout_seconds: int,
    dry_run: bool,
    step_start_ms: int,
) -> ChainStepResult:
    """Execute a single chain step via LLM (or dry-run path)."""
    step_errors: list[str] = []
    prompt = _assemble_chain_step_prompt(step, current_state, step_results, step_errors)

    if dry_run:
        print(f"=== DRY RUN CHAIN: {chain.name} / {step.step_name} ===")
        print(prompt[:1500])
        print(f"[... {len(prompt)} total chars]")
        step_elapsed = int(time.monotonic() * 1000) - step_start_ms
        return ChainStepResult(
            step_name=step.step_name,
            expected_decision=step.expected_decision,
            actual_decision="DRY_RUN",
            match=False,
            actual_output=prompt[:300],
            token_cost=0,
            duration_ms=step_elapsed,
            errors=step_errors,
        )

    raw_output, call_errors = _call_claude(prompt, model, timeout_seconds, str(_REPO_ROOT))
    step_errors.extend(call_errors)
    step_elapsed = int(time.monotonic() * 1000) - step_start_ms

    token_cost = _extract_tokens_from_stream(raw_output)
    collected_text = _collect_text_from_stream(raw_output)
    actual_decision, _ = _parse_decision(raw_output, step_errors)
    match = actual_decision == step.expected_decision

    status = "PASS" if match else "FAIL"
    print(
        f"[chain] {status}: {chain.name}/{step.step_name} "
        f"(expected={step.expected_decision}, actual={actual_decision}, "
        f"tokens={token_cost}, {step_elapsed}ms)",
        file=sys.stderr,
        flush=True,
    )
    return ChainStepResult(
        step_name=step.step_name,
        expected_decision=step.expected_decision,
        actual_decision=actual_decision,
        match=match,
        actual_output=collected_text or raw_output,
        token_cost=token_cost,
        duration_ms=step_elapsed,
        errors=step_errors,
    )


def _aggregate_chain_results(
    chain: ChainScenario,
    step_results: list[ChainStepResult],
    chain_start_ms: int,
    chain_errors: list[str],
) -> ChainReplayResult:
    """Compute final ChainReplayResult from collected step results."""
    if step_results:
        passed_steps = sum(1 for s in step_results if s.match)
        step_accuracy = passed_steps / len(step_results)
        all_steps_pass = passed_steps == len(step_results)
    else:
        step_accuracy = 0.0
        all_steps_pass = False

    total_tokens = sum(s.token_cost for s in step_results)
    total_elapsed = int(time.monotonic() * 1000) - chain_start_ms

    return ChainReplayResult(
        scenario_name=chain.name,
        level=chain.level,
        steps=step_results,
        all_steps_pass=all_steps_pass,
        step_accuracy=step_accuracy,
        total_token_cost=total_tokens,
        total_duration_ms=total_elapsed,
        errors=chain_errors,
    )


def run_chain_replay(
    scenario_path: Path,
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
) -> ChainReplayResult:
    """Run a multi-step chain scenario."""
    chain_start_ms = int(time.monotonic() * 1000)
    chain_errors: list[str] = []

    chain = _load_chain_scenario(scenario_path)
    if chain is None:
        elapsed = int(time.monotonic() * 1000) - chain_start_ms
        return ChainReplayResult(
            scenario_name=scenario_path.stem,
            level=2,
            steps=[],
            all_steps_pass=False,
            step_accuracy=0.0,
            total_token_cost=0,
            total_duration_ms=elapsed,
            errors=[f"Failed to load chain scenario: {scenario_path}"],
        )

    current_state = copy.deepcopy(chain.initial_state)
    step_results: list[ChainStepResult] = []

    for step in chain.steps:
        current_state = _apply_state_delta(current_state, step.state_delta)
        step_start_ms = int(time.monotonic() * 1000)

        prefilter_result = _run_chain_step_prefilter(chain, step, current_state, step_start_ms, dry_run=dry_run)
        if prefilter_result is not None:
            step_results.append(prefilter_result)
            continue

        step_result = _run_chain_step_llm(
            chain, step, current_state, step_results,
            model, timeout_seconds, dry_run, step_start_ms,
        )
        step_results.append(step_result)

    return _aggregate_chain_results(chain, step_results, chain_start_ms, chain_errors)


def run_all_chain_replays(
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
    mode_filter: str | None = None,
) -> list[ChainReplayResult]:
    """Run all level-2 chain scenario fixtures.

    mode_filter: None (all), "headless", or "interactive".
    """
    pattern = "level2_*.json"
    all_fixtures = sorted(_SCENARIOS_DIR.glob(pattern))
    fixtures = [f for f in all_fixtures if _fixture_matches_mode(f, mode_filter)]

    if not fixtures:
        print(f"[chain] No fixtures found for mode={mode_filter or 'all'}", file=sys.stderr)
        return []

    results: list[ChainReplayResult] = []
    for fixture in fixtures:
        print(f"[chain] Running chain {fixture.name} ...", file=sys.stderr, flush=True)
        result = run_chain_replay(fixture, model=model, dry_run=dry_run, timeout_seconds=timeout_seconds)
        results.append(result)
        status = "PASS" if result.all_steps_pass else f"PARTIAL({result.step_accuracy:.0%})"
        print(
            f"[chain] {status}: {result.scenario_name} "
            f"(steps={len(result.steps)}, accuracy={result.step_accuracy:.0%}, "
            f"tokens={result.total_token_cost}, {result.total_duration_ms}ms)",
            file=sys.stderr,
            flush=True,
        )
    return results
