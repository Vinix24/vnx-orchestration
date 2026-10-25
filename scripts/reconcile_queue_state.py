#!/usr/bin/env python3
"""CLI for deterministic queue state reconciliation.

Derives PR queue status from canonical runtime evidence (dispatch filesystem
and receipts) per docs/core/70_QUEUE_TRUTH_CONTRACT.md. No plan file is read:
a PR is in the queue when a dispatch names it (ADR-047 §4). The projection
files written by --repair are a cache, never a source.

Usage:
  python scripts/reconcile_queue_state.py
  python scripts/reconcile_queue_state.py --repair
  python scripts/reconcile_queue_state.py --repair --json

Exit codes:
  0  Reconciliation succeeded, no blocking drift
  1  Reconciliation succeeded, blocking drift detected
"""

from __future__ import annotations

import argparse
import json
import sys
import yaml
from pathlib import Path
from typing import Any, Dict

# Resolve lib path
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from vnx_paths import ensure_env  # noqa: E402
from queue_reconciler import (  # noqa: E402
    QueueReconciler,
    ReconcileResult,
)

try:
    from state_integrity import write_checksum
except ImportError:
    write_checksum = None


# ---------------------------------------------------------------------------
# Projection file writers
# ---------------------------------------------------------------------------

def _build_queue_state_json(result: ReconcileResult) -> Dict[str, Any]:
    """Build pr_queue_state.json payload from reconciled state.

    There is no feature block: the queue has no plan, and a feature is a
    track (ADR-047 §3).
    """
    prs_list = []
    for p in result.prs:
        meta = p.metadata
        prs_list.append({
            "id": p.pr_id,
            "title": meta.get("title", ""),
            "dependencies": meta.get("dependencies", []),
            "track": meta.get("track", "?"),
            "skill": meta.get("skill", ""),
            "gate": meta.get("gate", ""),
            "risk_class": meta.get("risk_class", "high"),
            "merge_policy": meta.get("merge_policy", "human"),
            "review_stack": meta.get("review_stack", []),
            "status": _state_to_status(p.state),
            "provenance": p.provenance,
        })

    completed = [p.pr_id for p in result.prs if p.state == "completed"]
    active = [p.pr_id for p in result.prs if p.state == "active"]
    blocked = [p.pr_id for p in result.prs if p.state == "blocked"]

    return {
        "prs": prs_list,
        "completed": completed,
        "active": active,
        "blocked": blocked,
        "reconciled_at": result.reconciled_at,
        "source": "queue_reconciler",
        "updated_at": result.reconciled_at,
    }


def _state_to_status(state: str) -> str:
    mapping = {
        "completed": "completed",
        "active": "in_progress",
        "pending": "queued",
        "blocked": "blocked",
    }
    return mapping.get(state, state)


# ---------------------------------------------------------------------------
# Repair: write projection files
# ---------------------------------------------------------------------------

def repair_projections(result: ReconcileResult, state_dir: Path) -> None:
    """Overwrite the projection cache with reconciled state (Section 5.3)."""
    state_dir.mkdir(parents=True, exist_ok=True)

    # pr_queue_state.json
    state_json_path = state_dir / "pr_queue_state.json"
    payload = _build_queue_state_json(result)
    state_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # pr_queue.json (same content, snapshot format)
    queue_json_path = state_dir / "pr_queue.json"
    from pr_queue_state_snapshot import build_vnx_state_snapshot  # type: ignore
    execution_order = [p.pr_id for p in result.prs]
    snapshot = build_vnx_state_snapshot(payload, True, execution_order)
    snapshot["reconciled_at"] = result.reconciled_at
    queue_json_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    if write_checksum:
        try:
            write_checksum(queue_json_path)
        except Exception as exc:
            print(f"Warning: checksum write failed for pr_queue.json: {exc}", file=sys.stderr)

    # pr_queue_state.yaml
    yaml_path = state_dir / "pr_queue_state.yaml"
    yaml_path.write_text(
        yaml.dump(snapshot, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    print(f"[ok] Written: {state_json_path}", file=sys.stderr)
    print(f"[ok] Written: {queue_json_path}", file=sys.stderr)
    print(f"[ok] Written: {yaml_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcile PR queue state from canonical runtime evidence"
    )
    p.add_argument(
        "--repair",
        action="store_true",
        help="Write reconciled state to projection files (default: read-only)",
    )
    p.add_argument(
        "--dispatch-dir",
        default=None,
        help="Path to dispatches directory (default: VNX_DISPATCH_DIR)",
    )
    p.add_argument(
        "--receipts-file",
        default=None,
        help="Path to t0_receipts.ndjson (default: VNX_STATE_DIR/t0_receipts.ndjson)",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        help="Path to VNX state directory (default: VNX_STATE_DIR)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print JSON result to stdout",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    paths = ensure_env()

    state_dir = Path(args.state_dir or paths["VNX_STATE_DIR"])
    dispatch_dir = Path(args.dispatch_dir or paths["VNX_DISPATCH_DIR"])
    receipts_file = Path(args.receipts_file or (state_dir / "t0_receipts.ndjson"))
    projection_file = state_dir / "pr_queue_state.json"

    reconciler = QueueReconciler(
        dispatch_dir=dispatch_dir,
        receipts_file=receipts_file,
        projection_file=projection_file if projection_file.is_file() else None,
    )

    result = reconciler.reconcile()

    if args.json_output:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        # Human-readable summary
        print(f"Reconciled at: {result.reconciled_at}")
        print()
        for pr in result.prs:
            icon = {"completed": "✅", "active": "🔄", "pending": "⏳", "blocked": "🚧"}.get(pr.state, "?")
            src = pr.provenance.get("source", "?")
            confirmed = pr.provenance.get("receipt_confirmed")
            note = ""
            if pr.state == "completed" and not confirmed:
                note = " [unconfirmed]"
            print(f"  {icon} {pr.pr_id}: {pr.state.upper()}{note}  (via {src})")
        if result.drift_warnings:
            print()
            print(f"Drift warnings ({len(result.drift_warnings)}):")
            for w in result.drift_warnings:
                print(f"  [{w.severity.upper()}] {w.message}")
        if result.has_blocking_drift:
            print()
            print("[!] BLOCKING DRIFT DETECTED — projection must be repaired before promotion/closure", file=sys.stderr)

    if args.repair:
        repair_projections(result, state_dir)

    return 1 if result.has_blocking_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
