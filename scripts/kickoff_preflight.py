#!/usr/bin/env python3
"""Kickoff preflight — reconcile queue truth before promotion or dispatch.

Runs deterministic queue reconciliation per docs/core/70_QUEUE_TRUTH_CONTRACT.md
Section 4.4: drift detection MUST run before dispatch promotion and PR closure.

Usage:
  python scripts/kickoff_preflight.py
  python scripts/kickoff_preflight.py --pr-id PR-2
  python scripts/kickoff_preflight.py --repair --json

Exit codes:
  0  Queue truth is fresh, safe to promote/dispatch
  1  Blocking drift detected — must repair before proceeding
  2  Fatal error (missing files, reconciliation failure)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(_SCRIPTS_DIR))

from vnx_paths import ensure_env  # noqa: E402
from queue_reconciler import (  # noqa: E402
    QueueReconciler,
    ReconcileResult,
)

# Routing preflight (PR-3: Kickoff, Preset, and Preflight Provider Readiness)
sys.path.insert(0, str(_SCRIPTS_DIR))
from routing_preflight import (  # noqa: E402
    extract_requirements_from_feature_plan,
    run_routing_preflight,
)


def run_preflight(
    *,
    project_root: Path,
    dispatch_dir: Path,
    receipts_file: Path,
    feature_plan: Path,
    state_dir: Path,
    pr_id: Optional[str] = None,
    repair: bool = False,
) -> Dict[str, Any]:
    """Run kickoff preflight reconciliation.

    Returns a result dict with:
      - safe_to_promote: bool
      - reconciled_state: dict (full reconcile result)
      - pr_status: dict (per-PR status if pr_id given)
      - drift_warnings: list
      - blocking_drift: list
    """
    if not feature_plan.is_file():
        return {
            "safe_to_promote": False,
            "error": f"FEATURE_PLAN.md not found: {feature_plan}",
            "reconciled_state": None,
            "pr_status": None,
            "drift_warnings": [],
            "blocking_drift": [],
            "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    projection_file = state_dir / "pr_queue_state.json"

    reconciler = QueueReconciler(
        dispatch_dir=dispatch_dir,
        receipts_file=receipts_file,
        feature_plan=feature_plan,
        projection_file=projection_file if projection_file.is_file() else None,
    )

    result = reconciler.reconcile()

    blocking = [w for w in result.drift_warnings if w.severity == "blocking"]
    safe = not result.has_blocking_drift

    pr_status: Optional[Dict[str, Any]] = None
    if pr_id:
        for pr in result.prs:
            if pr.pr_id == pr_id:
                pr_status = {
                    "pr_id": pr.pr_id,
                    "state": pr.state,
                    "provenance": pr.provenance,
                    "metadata": pr.metadata,
                }
                break
        if pr_status is None:
            return {
                "safe_to_promote": False,
                "error": f"PR {pr_id} not found in FEATURE_PLAN.md",
                "reconciled_state": result.as_dict(),
                "pr_status": None,
                "drift_warnings": [
                    {
                        "pr_id": pr_id,
                        "severity": "blocking",
                        "message": f"{pr_id} is not defined in FEATURE_PLAN.md",
                    }
                ],
                "blocking_drift": [
                    {
                        "pr_id": pr_id,
                        "severity": "blocking",
                        "message": f"{pr_id} is not defined in FEATURE_PLAN.md",
                    }
                ],
                "checked_at": datetime.now(tz=timezone.utc).isoformat(),
            }

    if repair and result.has_blocking_drift:
        from reconcile_queue_state import repair_projections
        repair_projections(result, state_dir, project_root)

    # --- Routing preflight (PR-3) ---
    # Check provider/model readiness from FEATURE_PLAN requirements
    routing_reqs = extract_requirements_from_feature_plan(feature_plan, pr_id)
    routing_report = run_routing_preflight(routing_reqs, check_pinned=True)

    routing_blocking = [
        {
            "terminal": b.terminal_id,
            "dimension": b.dimension,
            "required": b.required_value,
            "actual": b.actual_value,
            "strength": b.strength,
            "gap": b.gap,
            "diagnostic": b.diagnostic,
        }
        for b in routing_report.blocking
    ]
    routing_warnings = [
        {
            "terminal": w.terminal_id,
            "dimension": w.dimension,
            "required": w.required_value,
            "actual": w.actual_value,
            "diagnostic": w.diagnostic,
        }
        for w in routing_report.warnings
    ]

    # Routing blocks override queue-truth safe status
    if routing_blocking:
        safe = False

    return {
        "safe_to_promote": safe,
        "error": None,
        "reconciled_state": result.as_dict(),
        "pr_status": pr_status,
        "drift_warnings": [
            {
                "pr_id": w.pr_id,
                "severity": w.severity,
                "derived_state": w.derived_state,
                "projected_state": w.projected_state,
                "message": w.message,
            }
            for w in result.drift_warnings
        ],
        "blocking_drift": [
            {
                "pr_id": w.pr_id,
                "severity": w.severity,
                "derived_state": w.derived_state,
                "projected_state": w.projected_state,
                "message": w.message,
            }
            for w in blocking
        ],
        "routing_ready": routing_report.ready,
        "routing_blocking": routing_blocking,
        "routing_warnings": routing_warnings,
        "routing_pinned": [
            {
                "terminal": p.terminal_id,
                "provider_ok": p.provider_ok,
                "model_ok": p.model_ok,
                "expected_provider": p.expected_provider,
                "actual_provider": p.actual_provider,
                "expected_model": p.expected_model,
                "actual_model": p.actual_model,
            }
            for p in routing_report.pinned
        ],
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Kickoff preflight — reconcile queue truth before promotion"
    )
    p.add_argument(
        "--pr-id",
        default=None,
        help="Check a specific PR (optional; default checks all)",
    )
    p.add_argument(
        "--repair",
        action="store_true",
        help="Auto-repair projections when blocking drift detected",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print JSON result to stdout",
    )
    p.add_argument("--feature-plan", default=None)
    p.add_argument("--dispatch-dir", default=None)
    p.add_argument("--receipts-file", default=None)
    p.add_argument("--state-dir", default=None)
    return p


def main() -> int:
    args = _build_parser().parse_args()
    paths = ensure_env()

    project_root = Path(paths["PROJECT_ROOT"])
    state_dir = Path(args.state_dir or paths["VNX_STATE_DIR"])
    dispatch_dir = Path(args.dispatch_dir or paths["VNX_DISPATCH_DIR"])
    receipts_file = Path(args.receipts_file or (state_dir / "t0_receipts.ndjson"))
    feature_plan = Path(args.feature_plan or (project_root / "FEATURE_PLAN.md"))

    result = run_preflight(
        project_root=project_root,
        dispatch_dir=dispatch_dir,
        receipts_file=receipts_file,
        feature_plan=feature_plan,
        state_dir=state_dir,
        pr_id=args.pr_id,
        repair=args.repair,
    )

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        if result.get("error"):
            print(f"[x] {result['error']}", file=sys.stderr)
        elif result["safe_to_promote"]:
            print("[ok] Queue truth is fresh — safe to promote/dispatch")
            if result.get("pr_status"):
                ps = result["pr_status"]
                print(f"  {ps['pr_id']}: {ps['state'].upper()} (via {ps['provenance'].get('source', '?')})")
        else:
            print("[!] BLOCKING — cannot promote/dispatch", file=sys.stderr)
            for w in result.get("blocking_drift", []):
                print(f"  [QUEUE DRIFT] {w['message']}", file=sys.stderr)
            if result.get("drift_warnings"):
                non_blocking = [w for w in result["drift_warnings"] if w["severity"] != "blocking"]
                for w in non_blocking:
                    print(f"  [{w['severity'].upper()}] {w['message']}", file=sys.stderr)

        # Routing preflight diagnostics
        if result.get("routing_blocking"):
            print("\n  [ROUTING] Required capability gaps:", file=sys.stderr)
            for b in result["routing_blocking"]:
                print(f"    [{b['gap'].upper()}] {b['diagnostic']}", file=sys.stderr)
        elif result.get("routing_ready") is True:
            print("  [ROUTING] All provider/model requirements satisfied")

        if result.get("routing_warnings"):
            print("  [ROUTING] Advisory warnings:", file=sys.stderr)
            for w in result["routing_warnings"]:
                print(f"    [WARN] {w['diagnostic']}", file=sys.stderr)

    if result.get("error"):
        return 2
    return 0 if result["safe_to_promote"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
