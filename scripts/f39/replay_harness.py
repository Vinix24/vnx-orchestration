#!/usr/bin/env python3
"""F39 Replay Harness — runs headless T0 against scenario fixtures.

Usage:
    # Run single scenario
    python3 scripts/f39/replay_harness.py --scenario tests/f39/scenarios/level1_01_clean_receipt.json

    # Run all level-1 scenarios
    python3 scripts/f39/replay_harness.py --all --level 1

    # Run all level-2 chain scenarios
    python3 scripts/f39/replay_harness.py --all --level 2

    # Run all level-3 edge case scenarios
    python3 scripts/f39/replay_harness.py --all --level 3

    # Run all levels
    python3 scripts/f39/replay_harness.py --all

    # Use haiku for cheaper runs
    python3 scripts/f39/replay_harness.py --all --level 1 --model haiku

    # Dry-run: print context prompt only (no LLM call)
    python3 scripts/f39/replay_harness.py --scenario ... --dry-run
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR = _REPO_ROOT / "tests" / "f39" / "scenarios"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_assembler import assemble_t0_context, _DEFAULT_STATE, _DEFAULT_FEATURE_PLAN, _DEFAULT_SKILL, _DEFAULT_CLAUDE_MD  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
from decision_parser import (  # noqa: E402
    extract_json as _extract_json,
    extract_decision_from_stream as _extract_decision_from_stream,
    collect_text_from_stream as _collect_text_from_stream,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    scenario_name: str
    expected_decision: str
    actual_decision: str
    match: bool
    reason_match: bool          # Semantic alignment of reasoning (heuristic)
    actual_output: str          # Raw LLM output
    token_cost: int
    duration_ms: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "expected_decision": self.expected_decision,
            "actual_decision": self.actual_decision,
            "match": self.match,
            "reason_match": self.reason_match,
            "token_cost": self.token_cost,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "actual_output_excerpt": self.actual_output[:500],
        }


@dataclass
class ChainStep:
    step_name: str
    receipt: dict[str, Any]
    state_delta: dict[str, Any]
    expected_decision: str
    expected_next_action: str


@dataclass
class ChainScenario:
    name: str
    level: int
    description: str
    initial_state: dict[str, Any]
    steps: list[ChainStep]


@dataclass
class ChainStepResult:
    step_name: str
    expected_decision: str
    actual_decision: str
    match: bool
    actual_output: str
    token_cost: int
    duration_ms: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_name": self.step_name,
            "expected_decision": self.expected_decision,
            "actual_decision": self.actual_decision,
            "match": self.match,
            "token_cost": self.token_cost,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "actual_output_excerpt": self.actual_output[:300],
        }


@dataclass
class ChainReplayResult:
    scenario_name: str
    level: int
    steps: list[ChainStepResult]
    all_steps_pass: bool
    step_accuracy: float          # Fraction of steps with correct decision
    total_token_cost: int
    total_duration_ms: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "level": self.level,
            "all_steps_pass": self.all_steps_pass,
            "step_accuracy": self.step_accuracy,
            "total_token_cost": self.total_token_cost,
            "total_duration_ms": self.total_duration_ms,
            "errors": self.errors,
            "steps": [s.to_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# JSON extraction from LLM output
# (imported from scripts/lib/decision_parser — see import block above)
# ---------------------------------------------------------------------------


def _extract_tokens_from_stream(stream_output: str) -> int:
    """Sum input+output tokens from stream-json lines."""
    total = 0
    for line in stream_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # stream-json usage fields
        usage = obj.get("usage") or {}
        total += usage.get("input_tokens", 0)
        total += usage.get("output_tokens", 0)
        # Also check message-level
        if obj.get("type") == "message_delta":
            usage2 = (obj.get("usage") or {})
            total += usage2.get("output_tokens", 0)
    return total


# _collect_text_from_stream imported from scripts/lib/decision_parser above.


# ---------------------------------------------------------------------------
# Reason alignment (heuristic)
# ---------------------------------------------------------------------------

_REASON_KEYWORDS: dict[str, list[str]] = {
    "DISPATCH": ["dispatch", "next task", "next work", "assign", "send to", "proceed"],
    "COMPLETE": ["complete", "merge", "done", "finished", "closure", "close pr"],
    "REJECT":   ["reject", "missing", "incomplete", "not found", "invalid", "unverifi"],
    "WAIT":     ["wait", "busy", "no action", "hold", "not yet", "ghost", "duplicate", "unknown"],
    "ESCALATE": ["escalate", "blocker", "human", "intervention", "chain-breaking", "architectural"],
}


def _reason_aligns(decision: str, reason_text: str) -> bool:
    """Check whether the reason text semantically fits the decision."""
    keywords = _REASON_KEYWORDS.get(decision.upper(), [])
    lower = reason_text.lower()
    return any(kw in lower for kw in keywords)


# ---------------------------------------------------------------------------
# State delta application
# ---------------------------------------------------------------------------

def _apply_state_delta(state: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge delta into state, returning a new state dict."""
    result = copy.deepcopy(state)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _apply_state_delta(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# LLM invocation helper
# ---------------------------------------------------------------------------

def _call_claude(
    prompt: str,
    model: str,
    timeout_seconds: int,
    cwd: str,
) -> tuple[str, list[str]]:
    """Call claude -p and return (raw_output, errors)."""
    cmd = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        prompt,
    ]
    raw_output = ""
    errors: list[str] = []
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
        )
        raw_output = result.stdout
        if result.returncode != 0 and not raw_output:
            errors.append(f"claude exited {result.returncode}: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        errors.append(f"Timed out after {timeout_seconds}s")
    except FileNotFoundError:
        errors.append("'claude' CLI not found in PATH")
    return raw_output, errors


def _parse_decision(raw_output: str, errors: list[str]) -> tuple[str, str]:
    """Return (actual_decision, reason_text) from raw LLM output."""
    # First: extract from the result event in the NDJSON stream (most reliable)
    parsed = _extract_decision_from_stream(raw_output)
    # Fallback: collect text blocks and extract JSON from those
    if parsed is None:
        collected_text = _collect_text_from_stream(raw_output)
        parsed = _extract_json(collected_text) if collected_text else None

    if parsed:
        actual_decision = str(parsed.get("decision", "PARSE_ERROR")).upper()
        reason_text = str(parsed.get("reason", ""))
    elif errors:
        actual_decision = "ERROR"
        reason_text = ""
    else:
        actual_decision = "PARSE_ERROR"
        reason_text = ""

    return actual_decision, reason_text


# ---------------------------------------------------------------------------
# Code pre-filter — deterministic decisions before LLM call
# ---------------------------------------------------------------------------

def _code_prefilter(receipt: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Deterministic pre-filter. Returns decision string or None (needs LLM)."""
    dispatch_id = receipt.get("dispatch_id", "")

    # Rule 1: Ghost receipt
    if not dispatch_id or dispatch_id.startswith("unknown-"):
        return "WAIT"

    # Rule 2: Duplicate receipt
    recent = [r.get("dispatch_id") for r in state.get("recent_receipts", [])]
    if dispatch_id in recent:
        return "WAIT"

    # Rule 6: All terminals busy
    terminals = state.get("terminals", {})
    if terminals and all(not t.get("ready", False) for t in terminals.values()):
        return "WAIT"

    # Hard gate lock check — required gates must be completed before COMPLETE or DISPATCH
    # Uses flat review_gates structure: {gate_name: {required: bool, status: str}}
    flat_gates = state.get("review_gates", {})
    pending_required_gates = [
        gate_name
        for gate_name, gate_data in flat_gates.items()
        if isinstance(gate_data, dict)
        and gate_data.get("required", False)
        and gate_data.get("status", "") not in ("completed", "passed", "pass")
    ]
    if pending_required_gates:
        return "WAIT"  # Hard block — no LLM can override required gates

    # Rule 7: Feature complete — but only when all required review gates have results
    pr = state.get("pr_progress", {})
    oi = state.get("open_items", {})
    if (
        pr.get("completion_pct", 0) >= 100
        and oi.get("blocker_count", 0) == 0
        and state.get("queues", {}).get("pending_count", 0) == 0
    ):
        # Check for incomplete review gates (any gate null, "requested", or "queued")
        review_gates = state.get("review_gates", {})
        pending_gates = []
        for pr_gates in review_gates.values():
            if not isinstance(pr_gates, dict):
                continue
            for gate_val in pr_gates.values():
                if gate_val is None:
                    pending_gates.append(gate_val)
                elif isinstance(gate_val, dict) and gate_val.get("status") in ("requested", "queued"):
                    pending_gates.append(gate_val)
        if pending_gates:
            return None  # Has pending gates — let LLM decide WAIT

        return "COMPLETE"

    # Check 9: Dependency block — unmet PR dependencies prevent progress
    pr_progress = state.get("pr_progress", {})
    if pr_progress.get("blocked", []):
        return "WAIT"  # Hard block — dependent PRs not yet merged

    # Check 7: Worker failure auto-retry within budget
    if receipt.get("status") == "failure" and receipt.get("retry_count", 0) < 3:
        return "DISPATCH"  # Re-dispatch same task within retry budget

    # Check 10: CI failure detected in receipt — auto-dispatch fix task
    if (
        receipt.get("status") == "success"
        and receipt.get("ci_status") == "failure"
        and receipt.get("ci_failure_check")
    ):
        return "DISPATCH"  # CI fix task needed

    # Don't fast-path if receipt claims file changes but state has no git evidence
    files_claimed = receipt.get("files_modified") or (
        receipt.get("provenance", {}).get("diff_summary", {}).get("files_changed", 0)
    )
    git_evidence = receipt.get("provenance", {}).get("git_ref")
    if files_claimed and not git_evidence:
        return None  # Needs LLM verification — unverified file claims

    return None  # Needs LLM judgment


# ---------------------------------------------------------------------------
# Prior decisions section builder
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core single-step replay function
# ---------------------------------------------------------------------------

def run_replay(
    scenario_path: Path,
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
) -> ReplayResult:
    """Run a single replay scenario against headless T0."""
    start_ms = int(time.monotonic() * 1000)
    errors: list[str] = []

    # Load scenario fixture
    try:
        scenario: dict[str, Any] = json.loads(scenario_path.read_text(encoding="utf-8"))
    except Exception as exc:
        elapsed = int(time.monotonic() * 1000) - start_ms
        return ReplayResult(
            scenario_name=scenario_path.stem,
            expected_decision="UNKNOWN",
            actual_decision="ERROR",
            match=False,
            reason_match=False,
            actual_output="",
            token_cost=0,
            duration_ms=elapsed,
            errors=[f"Failed to load scenario: {exc}"],
        )

    name = scenario.get("name", scenario_path.stem)
    receipt = scenario.get("receipt", {})
    state_snapshot = scenario.get("state", {})
    expected = scenario.get("expected", {})
    expected_decision = expected.get("decision", "UNKNOWN").upper()
    acceptable_decisions = [d.upper() for d in expected.get("acceptable_decisions", [])]
    if not acceptable_decisions:
        acceptable_decisions = [expected_decision]

    # Deterministic pre-filter — skip LLM for obvious cases
    prefilter_decision = _code_prefilter(receipt, state_snapshot)
    if prefilter_decision is not None and not dry_run:
        elapsed = int(time.monotonic() * 1000) - start_ms
        match = prefilter_decision in acceptable_decisions
        return ReplayResult(
            scenario_name=name,
            expected_decision=expected_decision,
            actual_decision=prefilter_decision,
            match=match,
            reason_match=True,
            actual_output=f"[prefilter: {prefilter_decision}]",
            token_cost=0,
            duration_ms=elapsed,
            errors=errors,
        )

    # Write state snapshot to a temp file for the assembler
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(state_snapshot, tmp)
        tmp_state_path = Path(tmp.name)

    try:
        prompt = assemble_t0_context(
            state_path=tmp_state_path,
            receipt=receipt,
            feature_plan_path=_DEFAULT_FEATURE_PLAN if _DEFAULT_FEATURE_PLAN.exists() else Path("/dev/null"),
            skill_path=_DEFAULT_SKILL,
            claude_md_path=_DEFAULT_CLAUDE_MD,
        )
    except Exception as exc:
        errors.append(f"Context assembly failed: {exc}")
        prompt = ""
    finally:
        try:
            os.unlink(tmp_state_path)
        except Exception:
            pass

    if dry_run:
        print(f"=== DRY RUN: {name} ===")
        print(prompt[:2000])
        print(f"[... {len(prompt)} total chars]")
        elapsed = int(time.monotonic() * 1000) - start_ms
        return ReplayResult(
            scenario_name=name,
            expected_decision=expected_decision,
            actual_decision="DRY_RUN",
            match=False,
            reason_match=False,
            actual_output=prompt[:500],
            token_cost=0,
            duration_ms=elapsed,
            errors=errors,
        )

    raw_output, call_errors = _call_claude(prompt, model, timeout_seconds, str(_REPO_ROOT))
    errors.extend(call_errors)
    elapsed = int(time.monotonic() * 1000) - start_ms

    token_cost = _extract_tokens_from_stream(raw_output)
    collected_text = _collect_text_from_stream(raw_output)
    actual_decision, reason_text = _parse_decision(raw_output, errors)

    match = actual_decision in acceptable_decisions
    reason_match = _reason_aligns(actual_decision, reason_text) if reason_text else False

    return ReplayResult(
        scenario_name=name,
        expected_decision=expected_decision,
        actual_decision=actual_decision,
        match=match,
        reason_match=reason_match,
        actual_output=collected_text or raw_output,
        token_cost=token_cost,
        duration_ms=elapsed,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Chain replay function
# ---------------------------------------------------------------------------

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


def run_chain_replay(
    scenario_path: Path,
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
) -> ChainReplayResult:
    """Run a multi-step chain scenario.

    For each step:
    1. Apply state_delta to current state (cumulative)
    2. Assemble context with updated state + step receipt
    3. Inject prior T0 decisions as additional context section
    4. Call claude -p
    5. Parse decision
    6. Compare against expected
    7. Feed decision output into next step's context (chain memory)
    """
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
        step_start_ms = int(time.monotonic() * 1000)
        step_errors: list[str] = []

        # 1. Apply state_delta cumulatively
        current_state = _apply_state_delta(current_state, step.state_delta)

        # Deterministic pre-filter — skip LLM for obvious cases
        prefilter_decision = _code_prefilter(step.receipt, current_state)
        if prefilter_decision is not None and not dry_run:
            step_elapsed = int(time.monotonic() * 1000) - step_start_ms
            match = prefilter_decision == step.expected_decision
            step_result = ChainStepResult(
                step_name=step.step_name,
                expected_decision=step.expected_decision,
                actual_decision=prefilter_decision,
                match=match,
                actual_output=f"[prefilter: {prefilter_decision}]",
                token_cost=0,
                duration_ms=step_elapsed,
                errors=step_errors,
            )
            step_results.append(step_result)
            status = "PASS" if match else "FAIL"
            print(
                f"[chain] {status}: {chain.name}/{step.step_name} "
                f"(expected={step.expected_decision}, actual={prefilter_decision}, "
                f"prefilter=true, {step_elapsed}ms)",
                file=sys.stderr,
                flush=True,
            )
            continue

        # 2. Write current state to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
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
            except Exception:
                pass

        # 3. Inject prior decisions
        prior_section = _build_prior_decisions_section(step_results)
        if prior_section:
            prompt = base_prompt + "\n\n" + prior_section
        else:
            prompt = base_prompt

        if dry_run:
            print(f"=== DRY RUN CHAIN: {chain.name} / {step.step_name} ===")
            print(prompt[:1500])
            print(f"[... {len(prompt)} total chars]")
            step_elapsed = int(time.monotonic() * 1000) - step_start_ms
            step_results.append(ChainStepResult(
                step_name=step.step_name,
                expected_decision=step.expected_decision,
                actual_decision="DRY_RUN",
                match=False,
                actual_output=prompt[:300],
                token_cost=0,
                duration_ms=step_elapsed,
                errors=step_errors,
            ))
            continue

        # 4. Call claude
        raw_output, call_errors = _call_claude(prompt, model, timeout_seconds, str(_REPO_ROOT))
        step_errors.extend(call_errors)
        step_elapsed = int(time.monotonic() * 1000) - step_start_ms

        # 5. Parse decision
        token_cost = _extract_tokens_from_stream(raw_output)
        collected_text = _collect_text_from_stream(raw_output)
        actual_decision, _ = _parse_decision(raw_output, step_errors)

        # 6. Compare
        match = actual_decision == step.expected_decision

        step_result = ChainStepResult(
            step_name=step.step_name,
            expected_decision=step.expected_decision,
            actual_decision=actual_decision,
            match=match,
            actual_output=collected_text or raw_output,
            token_cost=token_cost,
            duration_ms=step_elapsed,
            errors=step_errors,
        )
        step_results.append(step_result)

        status = "PASS" if match else "FAIL"
        print(
            f"[chain] {status}: {chain.name}/{step.step_name} "
            f"(expected={step.expected_decision}, actual={actual_decision}, "
            f"tokens={token_cost}, {step_elapsed}ms)",
            file=sys.stderr,
            flush=True,
        )

    # Aggregate
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


# ---------------------------------------------------------------------------
# Batch runners
# ---------------------------------------------------------------------------

def _fixture_matches_mode(fixture_path: Path, mode_filter: str | None) -> bool:
    """Return True if the fixture's mode field is compatible with mode_filter.

    mode_filter=None → all fixtures pass.
    mode_filter="headless" → fixtures with mode "headless_only" or "both" pass.
    mode_filter="interactive" → fixtures with mode "interactive_only" or "both" pass.
    """
    if mode_filter is None:
        return True
    try:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture_mode = data.get("mode", "both")
    except Exception:
        return True  # Unknown mode — include by default
    if mode_filter == "headless":
        return fixture_mode in ("headless_only", "both")
    if mode_filter == "interactive":
        return fixture_mode in ("interactive_only", "both")
    return True


def run_all_replays(
    level: int = 1,
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
    mode_filter: str | None = None,
) -> list[ReplayResult]:
    """Run all single-step scenario fixtures for the given level (1 or 3).

    mode_filter: None (all), "headless", or "interactive".
    """
    pattern = f"level{level}_*.json"
    all_fixtures = sorted(_SCENARIOS_DIR.glob(pattern))
    fixtures = [f for f in all_fixtures if _fixture_matches_mode(f, mode_filter)]

    if not fixtures:
        label = f"level={level}, mode={mode_filter or 'all'}"
        print(f"[replay] No fixtures found for {label}", file=sys.stderr)
        return []

    results: list[ReplayResult] = []
    for fixture in fixtures:
        print(f"[replay] Running {fixture.name} ...", file=sys.stderr, flush=True)
        result = run_replay(fixture, model=model, dry_run=dry_run, timeout_seconds=timeout_seconds)
        results.append(result)
        status = "PASS" if result.match else "FAIL"
        print(
            f"[replay] {status}: {result.scenario_name} "
            f"(expected={result.expected_decision}, actual={result.actual_decision}, "
            f"tokens={result.token_cost}, {result.duration_ms}ms)",
            file=sys.stderr,
            flush=True,
        )

    return results


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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_single_step_summary(results: list[ReplayResult], level: int) -> None:
    passed = sum(1 for r in results if r.match)
    total = len(results)
    total_tokens = sum(r.token_cost for r in results)
    print(f"\n{'='*60}")
    print(f"Level-{level} Replay Summary")
    print(f"  Passed : {passed}/{total}")
    print(f"  Tokens : {total_tokens}")
    print(f"{'='*60}")
    for r in results:
        status = "PASS" if r.match else "FAIL"
        errs = f" [errors: {'; '.join(r.errors)}]" if r.errors else ""
        print(f"  [{status}] {r.scenario_name:<45} exp={r.expected_decision:<10} got={r.actual_decision}{errs}")


def _print_chain_summary(results: list[ChainReplayResult]) -> None:
    all_step_results = [s for r in results for s in r.steps]
    passed_steps = sum(1 for s in all_step_results if s.match)
    total_steps = len(all_step_results)
    total_tokens = sum(r.total_token_cost for r in results)
    print(f"\n{'='*60}")
    print(f"Level-2 Chain Replay Summary")
    print(f"  Chains         : {len(results)}")
    print(f"  Steps passed   : {passed_steps}/{total_steps}")
    print(f"  Tokens         : {total_tokens}")
    print(f"{'='*60}")
    for r in results:
        status = "PASS" if r.all_steps_pass else "FAIL"
        print(f"  [{status}] {r.scenario_name} (accuracy={r.step_accuracy:.0%})")
        for s in r.steps:
            s_status = "  PASS" if s.match else "  FAIL"
            errs = f" [{'; '.join(s.errors)}]" if s.errors else ""
            print(f"       [{s_status}] {s.step_name:<40} exp={s.expected_decision:<10} got={s.actual_decision}{errs}")


def main() -> int:
    parser = argparse.ArgumentParser(description="F39 Replay Harness")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scenario", help="Path to a single scenario fixture JSON")
    mode.add_argument("--all", action="store_true", help="Run all fixtures (for --level, or all levels)")
    parser.add_argument("--level", type=int, default=None, help="Scenario level (1, 2, or 3; omit for all)")
    parser.add_argument("--model", default="sonnet", help="Claude model (default: sonnet)")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt only, no LLM call")
    parser.add_argument("--timeout", type=int, default=120, help="Per-scenario timeout (seconds)")
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output results as JSON")
    parser.add_argument(
        "--mode",
        choices=["headless", "interactive"],
        default=None,
        help="Filter fixtures by mode: headless (headless_only+both), interactive (interactive_only+both). Default: all.",
    )
    args = parser.parse_args()

    any_failure = False

    if args.all:
        levels_to_run = [args.level] if args.level is not None else [1, 2, 3]

        all_single_results: dict[int, list[ReplayResult]] = {}
        all_chain_results: list[ChainReplayResult] = []

        for lvl in levels_to_run:
            if lvl == 2:
                chain_results = run_all_chain_replays(
                    model=args.model,
                    dry_run=args.dry_run,
                    timeout_seconds=args.timeout,
                    mode_filter=args.mode,
                )
                all_chain_results.extend(chain_results)
            else:
                single_results = run_all_replays(
                    level=lvl,
                    model=args.model,
                    dry_run=args.dry_run,
                    timeout_seconds=args.timeout,
                    mode_filter=args.mode,
                )
                all_single_results[lvl] = single_results

        if args.output_json:
            output: dict[str, Any] = {}
            for lvl, results in all_single_results.items():
                output[f"level{lvl}"] = [r.to_dict() for r in results]
            if all_chain_results:
                output["level2"] = [r.to_dict() for r in all_chain_results]
            print(json.dumps(output, indent=2))
            return 0

        for lvl, results in all_single_results.items():
            _print_single_step_summary(results, lvl)
            if not args.dry_run:
                passed = sum(1 for r in results if r.match)
                if passed < len(results):
                    any_failure = True

        if all_chain_results:
            _print_chain_summary(all_chain_results)
            if not args.dry_run:
                for r in all_chain_results:
                    if not r.all_steps_pass:
                        any_failure = True

    else:
        # Single scenario
        scenario_path = Path(args.scenario)
        # Detect chain scenario by level prefix or type field
        is_chain = False
        if scenario_path.name.startswith("level2_"):
            is_chain = True
        else:
            try:
                data = json.loads(scenario_path.read_text(encoding="utf-8"))
                is_chain = data.get("type") == "chain"
            except Exception:
                pass

        if is_chain:
            result = run_chain_replay(
                scenario_path,
                model=args.model,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout,
            )
            if args.output_json:
                print(json.dumps(result.to_dict(), indent=2))
                return 0
            _print_chain_summary([result])
            if not args.dry_run and not result.all_steps_pass:
                any_failure = True
        else:
            result_single = run_replay(
                scenario_path,
                model=args.model,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout,
            )
            if args.output_json:
                print(json.dumps(result_single.to_dict(), indent=2))
                return 0
            _print_single_step_summary([result_single], args.level or 1)
            if not args.dry_run and not result_single.match:
                any_failure = True

    if args.dry_run:
        return 0
    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
