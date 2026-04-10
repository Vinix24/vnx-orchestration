#!/usr/bin/env python3
"""F39 Replay Harness — runs headless T0 against scenario fixtures.

Usage:
    # Run single scenario
    python3 scripts/f39/replay_harness.py --scenario tests/f39/scenarios/level1_01_clean_receipt.json

    # Run all level-1 scenarios
    python3 scripts/f39/replay_harness.py --all --level 1

    # Use haiku for cheaper runs
    python3 scripts/f39/replay_harness.py --all --level 1 --model haiku

    # Dry-run: print context prompt only (no LLM call)
    python3 scripts/f39/replay_harness.py --scenario ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR = _REPO_ROOT / "tests" / "f39" / "scenarios"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_assembler import assemble_t0_context, _DEFAULT_STATE, _DEFAULT_FEATURE_PLAN, _DEFAULT_SKILL, _DEFAULT_CLAUDE_MD  # noqa: E402


# ---------------------------------------------------------------------------
# Result type
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


# ---------------------------------------------------------------------------
# JSON extraction from LLM output
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first valid JSON object from LLM output.

    The model may wrap the object in ```json ... ``` or emit it bare.
    """
    # Try: bare JSON object starting with {
    for match in re.finditer(r"\{", text):
        start = match.start()
        # Greedily expand to find matching close brace
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start: i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # Try next {

    return None


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


def _collect_text_from_stream(stream_output: str) -> str:
    """Collect all text content blocks from stream-json output."""
    parts: list[str] = []
    for line in stream_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Plain text line — accumulate in case output-format isn't stream-json
            parts.append(line)
            continue
        # stream-json content_block_delta
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            parts.append(delta.get("text", ""))
        # Plain assistant message
        content = obj.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        elif isinstance(content, str):
            parts.append(content)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Reason alignment (heuristic)
# ---------------------------------------------------------------------------

_REASON_KEYWORDS: dict[str, list[str]] = {
    "ACCEPT":   ["accept", "valid", "complete", "success", "criteria met", "approve"],
    "REJECT":   ["reject", "missing", "incomplete", "not found", "invalid", "unverifi"],
    "DISPATCH": ["dispatch", "next task", "next work", "assign", "send to"],
    "WAIT":     ["wait", "busy", "no action", "hold", "not yet"],
    "ESCALATE": ["escalate", "blocker", "human", "intervention", "chain-breaking"],
    "IGNORE":   ["ignore", "ghost", "duplicate", "unknown dispatch", "no receipt"],
}


def _reason_aligns(decision: str, reason_text: str) -> bool:
    """Check whether the reason text semantically fits the decision."""
    keywords = _REASON_KEYWORDS.get(decision.upper(), [])
    lower = reason_text.lower()
    return any(kw in lower for kw in keywords)


# ---------------------------------------------------------------------------
# Core replay function
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

    # Write state snapshot to a temp file for the assembler
    import tempfile, os
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

    # Call claude -p
    cmd = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        prompt,
    ]

    raw_output = ""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(_REPO_ROOT),
        )
        raw_output = result.stdout
        if result.returncode != 0 and not raw_output:
            errors.append(f"claude exited {result.returncode}: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        errors.append(f"Timed out after {timeout_seconds}s")
    except FileNotFoundError:
        errors.append("'claude' CLI not found in PATH")

    elapsed = int(time.monotonic() * 1000) - start_ms

    # Parse collected text
    collected_text = _collect_text_from_stream(raw_output)
    token_cost = _extract_tokens_from_stream(raw_output)

    # Extract JSON decision
    parsed = _extract_json(collected_text) if collected_text else None
    if parsed is None:
        # Fallback: try raw output directly
        parsed = _extract_json(raw_output)

    actual_decision = "PARSE_ERROR"
    reason_text = ""
    if parsed:
        actual_decision = str(parsed.get("decision", "PARSE_ERROR")).upper()
        reason_text = str(parsed.get("reason", ""))
    elif errors:
        actual_decision = "ERROR"

    match = actual_decision == expected_decision
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
# Batch runner
# ---------------------------------------------------------------------------

def run_all_replays(
    level: int = 1,
    model: str = "sonnet",
    dry_run: bool = False,
    timeout_seconds: int = 120,
) -> list[ReplayResult]:
    """Run all scenario fixtures for the given level."""
    pattern = f"level{level}_*.json"
    fixtures = sorted(_SCENARIOS_DIR.glob(pattern))

    if not fixtures:
        print(f"[replay] No fixtures found matching {_SCENARIOS_DIR}/{pattern}", file=sys.stderr)
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="F39 Replay Harness")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scenario", help="Path to a single scenario fixture JSON")
    mode.add_argument("--all", action="store_true", help="Run all fixtures for --level")
    parser.add_argument("--level", type=int, default=1, help="Scenario level (default: 1)")
    parser.add_argument("--model", default="sonnet", help="Claude model (default: sonnet)")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt only, no LLM call")
    parser.add_argument("--timeout", type=int, default=120, help="Per-scenario timeout (seconds)")
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output results as JSON")
    args = parser.parse_args()

    if args.all:
        results = run_all_replays(
            level=args.level,
            model=args.model,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
        )
    else:
        result = run_replay(
            Path(args.scenario),
            model=args.model,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
        )
        results = [result]

    if args.output_json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
        return 0

    # Summary table
    passed = sum(1 for r in results if r.match)
    total = len(results)
    total_tokens = sum(r.token_cost for r in results)
    print(f"\n{'='*60}")
    print(f"Level-{args.level} Replay Summary")
    print(f"  Passed : {passed}/{total}")
    print(f"  Tokens : {total_tokens}")
    print(f"{'='*60}")
    for r in results:
        status = "PASS" if r.match else "FAIL"
        errs = f" [errors: {'; '.join(r.errors)}]" if r.errors else ""
        print(f"  [{status}] {r.scenario_name:<45} exp={r.expected_decision:<10} got={r.actual_decision}{errs}")

    # Exit code: 0 if all pass (or dry-run), 1 if any fail
    if args.dry_run:
        return 0
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
