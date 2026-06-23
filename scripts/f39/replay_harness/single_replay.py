from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .models import ReplayResult
from .prefilter import _code_prefilter, _reason_aligns

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCENARIOS_DIR = _REPO_ROOT / "tests" / "f39" / "scenarios"

# Populated by __init__.py after sys.path setup; accessed via module reference for testability.
from replay_context_assembler import (  # noqa: E402
    assemble_t0_context,
    _DEFAULT_FEATURE_PLAN,
    _DEFAULT_SKILL,
    _DEFAULT_CLAUDE_MD,
)
from decision_parser import (  # noqa: E402
    extract_json as _extract_json,
    extract_decision_from_stream as _extract_decision_from_stream,
    collect_text_from_stream as _collect_text_from_stream,
)


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
        usage = obj.get("usage") or {}
        total += usage.get("input_tokens", 0)
        total += usage.get("output_tokens", 0)
        if obj.get("type") == "message_delta":
            usage2 = (obj.get("usage") or {})
            total += usage2.get("output_tokens", 0)
    return total


def _call_claude(
    prompt: str,
    model: str,
    timeout_seconds: int,
    cwd: str,
) -> tuple[str, list[str]]:
    """Call claude -p and return (raw_output, errors)."""
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model, prompt]
    raw_output = ""
    errors: list[str] = []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, cwd=cwd)
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
    parsed = _extract_decision_from_stream(raw_output)
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


def _assemble_replay_prompt(receipt: dict[str, Any], state_snapshot: dict[str, Any]) -> tuple[str, list[str]]:
    """Write state to a temp file, assemble context prompt, clean up. Returns (prompt, errors)."""
    errors: list[str] = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
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
        except OSError as exc:
            log.debug("Failed to unlink tmp state path: %s", exc)
    return prompt, errors


def _build_dry_run_result(
    name: str,
    expected_decision: str,
    prompt: str,
    start_ms: int,
    errors: list[str],
) -> ReplayResult:
    elapsed = int(time.monotonic() * 1000) - start_ms
    print(f"=== DRY RUN: {name} ===")
    print(prompt[:2000])
    print(f"[... {len(prompt)} total chars]")
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


def _build_prefilter_replay_result(
    prefilter_decision: str,
    name: str,
    expected_decision: str,
    acceptable_decisions: list[str],
    start_ms: int,
    errors: list[str],
) -> ReplayResult:
    elapsed = int(time.monotonic() * 1000) - start_ms
    return ReplayResult(
        scenario_name=name,
        expected_decision=expected_decision,
        actual_decision=prefilter_decision,
        match=prefilter_decision in acceptable_decisions,
        reason_match=True,
        actual_output=f"[prefilter: {prefilter_decision}]",
        token_cost=0,
        duration_ms=elapsed,
        errors=errors,
    )


def run_replay(
    scenario_path: Path,
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
) -> ReplayResult:
    """Run a single replay scenario against headless T0."""
    start_ms = int(time.monotonic() * 1000)
    errors: list[str] = []

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

    prefilter_decision = _code_prefilter(receipt, state_snapshot)
    if prefilter_decision is not None and not dry_run:
        return _build_prefilter_replay_result(
            prefilter_decision, name, expected_decision, acceptable_decisions, start_ms, errors
        )

    prompt, prompt_errors = _assemble_replay_prompt(receipt, state_snapshot)
    errors.extend(prompt_errors)

    if dry_run:
        return _build_dry_run_result(name, expected_decision, prompt, start_ms, errors)

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


def _fixture_matches_mode(fixture_path: Path, mode_filter: str | None) -> bool:
    """Return True if the fixture's mode field is compatible with mode_filter."""
    if mode_filter is None:
        return True
    try:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture_mode = data.get("mode", "both")
    except Exception:
        return True
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
    import sys
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
