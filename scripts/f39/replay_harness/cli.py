from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .models import ReplayResult, ChainReplayResult
from .single_replay import run_replay, run_all_replays
from .chain_replay import run_chain_replay, run_all_chain_replays

log = logging.getLogger(__name__)


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


def _build_arg_parser() -> argparse.ArgumentParser:
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
    return parser


def _detect_chain_scenario(scenario_path: Path) -> bool:
    """Detect chain scenario by level prefix or type field."""
    if scenario_path.name.startswith("level2_"):
        return True
    try:
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
        return data.get("type") == "chain"
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Failed to detect chain type from scenario file: %s", exc)
        return False


def _run_all_mode(args: argparse.Namespace) -> bool:
    """Handle --all mode. Returns True if any failure occurred."""
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
        return False

    any_failure = False
    for lvl, results in all_single_results.items():
        _print_single_step_summary(results, lvl)
        if not args.dry_run and sum(1 for r in results if r.match) < len(results):
            any_failure = True

    if all_chain_results:
        _print_chain_summary(all_chain_results)
        if not args.dry_run and any(not r.all_steps_pass for r in all_chain_results):
            any_failure = True

    return any_failure


def _run_single_mode(args: argparse.Namespace) -> bool:
    """Handle single --scenario mode. Returns True if failure occurred."""
    scenario_path = Path(args.scenario)
    is_chain = _detect_chain_scenario(scenario_path)

    if is_chain:
        result = run_chain_replay(
            scenario_path,
            model=args.model,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
        )
        if args.output_json:
            print(json.dumps(result.to_dict(), indent=2))
            return False
        _print_chain_summary([result])
        return not args.dry_run and not result.all_steps_pass

    result_single = run_replay(
        scenario_path,
        model=args.model,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
    )
    if args.output_json:
        print(json.dumps(result_single.to_dict(), indent=2))
        return False
    _print_single_step_summary([result_single], args.level or 1)
    return not args.dry_run and not result_single.match


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.all:
        any_failure = _run_all_mode(args)
    else:
        any_failure = _run_single_mode(args)

    if args.dry_run:
        return 0
    return 1 if any_failure else 0
