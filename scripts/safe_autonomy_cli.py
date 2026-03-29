#!/usr/bin/env python3
"""
VNX Safe Autonomy CLI — Operator interface for cutover management.

Usage:
    python scripts/safe_autonomy_cli.py status
    python scripts/safe_autonomy_cli.py prerequisites
    python scripts/safe_autonomy_cli.py prepare
    python scripts/safe_autonomy_cli.py cutover --actor t0 --justification "..."
    python scripts/safe_autonomy_cli.py rollback --actor t0 --justification "..."
    python scripts/safe_autonomy_cli.py verify-envelope
    python scripts/safe_autonomy_cli.py certify [--json]
    python scripts/safe_autonomy_cli.py review [--dispatch-ids D1,D2]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env  # noqa: E402
from runtime_coordination import get_connection, init_schema  # noqa: E402


def _get_state_dir(args) -> str:
    """Resolve state directory from args or environment."""
    import os
    state_dir = getattr(args, "state_dir", None)
    if state_dir:
        return state_dir
    env_dir = os.environ.get("VNX_DATA_DIR")
    if env_dir:
        sd = str(Path(env_dir) / "state")
        if Path(sd).exists():
            return sd
    return str(Path.cwd() / ".vnx-data" / "state")


def _ensure_schema(state_dir: str) -> None:
    """Ensure schema is initialized with all migrations."""
    init_schema(state_dir)
    schema_dir = SCRIPT_DIR.parent / "schemas"
    with get_connection(state_dir) as conn:
        for v in (5, 6, 7):
            sql_path = schema_dir / f"runtime_coordination_v{v}.sql"
            if sql_path.exists():
                conn.executescript(sql_path.read_text())
        conn.commit()


def cmd_status(args) -> int:
    """Show current cutover status."""
    from safe_autonomy_cutover import get_cutover_status

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    with get_connection(state_dir) as conn:
        status = get_cutover_status(conn)

    d = status.to_dict()

    if args.json:
        print(json.dumps(d, indent=2))
        return 0

    print(f"\nVNX Safe Autonomy — Cutover Status")
    print(f"{'='*50}")
    print(f"  Phase:          {d['phase']}")
    print(f"  Description:    {d['phase_description']}")
    print(f"  Autonomy:       {'ENFORCED' if d['autonomy_enforcement'] else 'shadow'}")
    print(f"  Provenance:     {'ENFORCED' if d['provenance_enforcement'] else 'shadow'}")
    print(f"  Prerequisites:  {'MET' if d['prerequisites_met'] else 'NOT MET'}")

    if d.get("escalation_health"):
        eh = d["escalation_health"]
        print(f"\n  Escalation Health:")
        print(f"    Blocking: {eh.get('blocking_count', 0)}")
        print(f"    Holds:    {len(eh.get('holds', []))}")
        print(f"    Escalate: {len(eh.get('escalations', []))}")

    if d.get("residual_risks"):
        print(f"\n  Residual Risks: {len(d['residual_risks'])}")

    print()
    return 0


def cmd_prerequisites(args) -> int:
    """Validate cutover prerequisites."""
    from safe_autonomy_cutover import validate_prerequisites

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    with get_connection(state_dir) as conn:
        checks = validate_prerequisites(conn, repo_root=Path.cwd())

    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=2))
        return 0

    print(f"\nVNX Safe Autonomy — Prerequisites")
    print(f"{'='*50}")
    for c in checks:
        icon = "[ok]" if c.passed else "[x]"
        print(f"  {icon} {c.name}: {c.description}")
    print()

    all_met = all(c.passed for c in checks)
    return 0 if all_met else 1


def cmd_prepare(args) -> int:
    """Run pre-cutover validation."""
    from safe_autonomy_cutover import prepare_cutover

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    with get_connection(state_dir) as conn:
        report = prepare_cutover(conn, repo_root=Path.cwd())
        conn.commit()

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"\nVNX Safe Autonomy — Pre-Cutover Report")
    print(f"{'='*50}")
    print(f"  Ready:   {'YES' if report['ready'] else 'NO'}")
    print(f"  Phase:   {report['current_phase']}")
    if report["failed_prerequisites"]:
        print(f"  Failed:  {', '.join(report['failed_prerequisites'])}")
    print(f"\n  {report['recommendation']}")
    print()
    return 0 if report["ready"] else 1


def cmd_cutover(args) -> int:
    """Execute cutover transition."""
    from safe_autonomy_cutover import execute_cutover, PHASE_FULL_ENFORCEMENT, PHASE_PROVENANCE_ONLY

    if not args.justification:
        print("Error: --justification is required for cutover", file=sys.stderr)
        return 1

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    target = PHASE_FULL_ENFORCEMENT
    if args.provenance_only:
        target = PHASE_PROVENANCE_ONLY

    with get_connection(state_dir) as conn:
        result = execute_cutover(
            conn, target_phase=target,
            actor=args.actor, justification=args.justification,
        )
        conn.commit()

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if not result["success"]:
        print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
        return 1

    print(f"\nVNX Safe Autonomy — Cutover Recorded")
    print(f"{'='*50}")
    print(f"  Previous: {result['previous_phase']}")
    print(f"  Target:   {result['target_phase']}")
    print(f"\n  {result['message']}")
    flags = result.get("flag_instructions", {})
    for k, v in flags.items():
        print(f"    export {k}={v}")
    print()
    return 0


def cmd_rollback(args) -> int:
    """Execute rollback to shadow mode."""
    from safe_autonomy_cutover import execute_rollback

    if not args.justification:
        print("Error: --justification is required for rollback", file=sys.stderr)
        return 1

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    with get_connection(state_dir) as conn:
        result = execute_rollback(conn, actor=args.actor, justification=args.justification)
        conn.commit()

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    if not result.success:
        print(f"Error: {result.warnings}", file=sys.stderr)
        return 1

    print(f"\nVNX Safe Autonomy — Rollback Recorded")
    print(f"{'='*50}")
    print(f"  Previous: {result.previous_phase}")
    print(f"  New:      {result.new_phase}")
    for action in result.actions_taken:
        print(f"    - {action}")
    for w in result.warnings:
        print(f"  [!] {w}")
    print()
    return 0


def cmd_verify_envelope(args) -> int:
    """Verify autonomy envelope constraints."""
    from safe_autonomy_cutover import verify_autonomy_envelope

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    with get_connection(state_dir) as conn:
        result = verify_autonomy_envelope(conn)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"\nVNX Safe Autonomy — Envelope Verification")
    print(f"{'='*50}")
    print(f"  Passed:    {'YES' if result['passed'] else 'NO'}")
    print(f"  Automatic: {result['automatic_count']}")
    print(f"  Gated:     {result['gated_count']}")
    print(f"  Forbidden: {result['forbidden_count']}")
    if result["findings"]:
        print(f"\n  Findings:")
        for f in result["findings"]:
            icon = "[!]" if f["severity"] == "warning" else "[x]"
            print(f"    {icon} {f['decision_type']}: {f['description']}")
    print()
    return 0 if result["passed"] else 1


def cmd_certify(args) -> int:
    """Run FP-D certification."""
    sys.path.insert(0, str(SCRIPT_DIR))
    from fpd_certification import run_certification

    sections = args.section if args.section else None
    report = run_certification(sections=sections)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    print(f"\nVNX FP-D Certification Report")
    print(f"{'='*60}")
    for row in report.rows:
        icon = {"pass": "[ok]", "fail": "[x]", "skip": "[~]"}.get(row.status, "[ ]")
        print(f"  {icon} {row.row_id}: {row.scenario}")
        if row.evidence:
            print(f"       {row.evidence}")

    certified = "CERTIFIED" if report.certified else "NOT CERTIFIED"
    print(f"\n  Result: {certified}")
    print(f"  Passed: {report.passed} | Failed: {report.failed} | Skipped: {report.skipped}")
    print()
    return 0 if report.certified else 1


def cmd_review(args) -> int:
    """Generate T0 review summary."""
    from safe_autonomy_cutover import t0_review_summary

    state_dir = _get_state_dir(args)
    _ensure_schema(state_dir)

    dispatch_ids = args.dispatch_ids.split(",") if args.dispatch_ids else None

    with get_connection(state_dir) as conn:
        summary = t0_review_summary(conn, dispatch_ids=dispatch_ids)

    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="VNX Safe Autonomy CLI")
    parser.add_argument("--state-dir", help="State directory override")
    parser.add_argument("--json", action="store_true", help="JSON output")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show cutover status")
    subparsers.add_parser("prerequisites", help="Validate prerequisites")
    subparsers.add_parser("prepare", help="Pre-cutover validation")

    cutover_p = subparsers.add_parser("cutover", help="Execute cutover")
    cutover_p.add_argument("--actor", default="t0", choices=["t0", "operator"])
    cutover_p.add_argument("--justification", required=True)
    cutover_p.add_argument("--provenance-only", action="store_true")

    rollback_p = subparsers.add_parser("rollback", help="Rollback to shadow")
    rollback_p.add_argument("--actor", default="t0", choices=["t0", "operator"])
    rollback_p.add_argument("--justification", required=True)

    subparsers.add_parser("verify-envelope", help="Verify autonomy envelope")

    certify_p = subparsers.add_parser("certify", help="Run FP-D certification")
    certify_p.add_argument("--section", type=int, action="append")

    review_p = subparsers.add_parser("review", help="T0 review summary")
    review_p.add_argument("--dispatch-ids", help="Comma-separated dispatch IDs")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "status": cmd_status,
        "prerequisites": cmd_prerequisites,
        "prepare": cmd_prepare,
        "cutover": cmd_cutover,
        "rollback": cmd_rollback,
        "verify-envelope": cmd_verify_envelope,
        "certify": cmd_certify,
        "review": cmd_review,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
