#!/usr/bin/env python3
"""CLI for deterministic queue state reconciliation.

Derives PR queue status from canonical runtime evidence (dispatch filesystem,
receipts, FEATURE_PLAN.md) per docs/core/70_QUEUE_TRUTH_CONTRACT.md.

Usage:
  python scripts/reconcile_queue_state.py
  python scripts/reconcile_queue_state.py --repair
  python scripts/reconcile_queue_state.py --feature-plan /path/to/FEATURE_PLAN.md
  python scripts/reconcile_queue_state.py --repair --json

Exit codes:
  0  Reconciliation succeeded, no blocking drift
  1  Reconciliation succeeded, blocking drift detected
  2  Fatal error (missing FEATURE_PLAN.md, etc.)
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
    PRReconciled,
)

try:
    from state_integrity import write_checksum
except ImportError:
    write_checksum = None


# ---------------------------------------------------------------------------
# PR_QUEUE.md generation from reconciled state
# ---------------------------------------------------------------------------

def _render_pr_queue_md(result: ReconcileResult) -> str:
    """Render PR_QUEUE.md content from reconciled state."""
    prs = result.prs
    total = len(prs)
    completed = sum(1 for p in prs if p.state == "completed")
    active = sum(1 for p in prs if p.state == "active")
    pending = sum(1 for p in prs if p.state == "pending")
    blocked = sum(1 for p in prs if p.state == "blocked")

    percent = int(completed / total * 100) if total > 0 else 0
    bar = "█" * (percent // 10) + "░" * (10 - percent // 10)

    lines = [
        f"# PR Queue - Feature: {result.feature_name}",
        "",
        "## Progress Overview",
        f"Total: {total} PRs | Complete: {completed} | Active: {active} | Queued: {pending} | Blocked: {blocked}",
        f"Progress: {bar} {percent}%",
        "",
        f"<!-- reconciled_at: {result.reconciled_at} -->",
        f"<!-- source: queue_reconciler (canonical runtime evidence) -->",
        "",
    ]

    # Governance metadata from first PR (feature-level)
    first = next((p for p in prs), None)
    if first:
        meta = first.metadata
        review_stack = ",".join(meta.get("review_stack") or []) or "none"
        lines += [
            "## Governance Metadata",
            f"Risk-Class: {meta.get('risk_class', 'unknown')}",
            f"Merge-Policy: {meta.get('merge_policy', 'human')}",
            f"Review-Stack: {review_stack}",
            "",
            "## Status",
        ]
    else:
        lines.append("## Status")

    # Sections by state
    completed_prs = [p for p in prs if p.state == "completed"]
    if completed_prs:
        lines.append("")
        lines.append("### ✅ Completed PRs")
        for p in completed_prs:
            receipt_note = "" if p.provenance.get("receipt_confirmed") else " ⚠️ unconfirmed"
            lines.append(f"- {p.pr_id}: {p.metadata['title']}{receipt_note}")

    active_prs = [p for p in prs if p.state == "active"]
    if active_prs:
        lines.append("")
        lines.append("### 🔄 Currently Active")
        for p in active_prs:
            meta = p.metadata
            lines.append(
                f"- {p.pr_id}: {meta['title']} "
                f"(Track {meta.get('track', '?')}, skill: {meta.get('skill', '?')}, "
                f"risk: {meta.get('risk_class', 'unknown')}, merge: {meta.get('merge_policy', 'human')})"
            )

    pending_prs = [p for p in prs if p.state == "pending"]
    if pending_prs:
        lines.append("")
        lines.append("### ⏳ Queued PRs")
        for p in pending_prs:
            meta = p.metadata
            deps = meta.get("dependencies") or []
            dep_str = f" (dependencies: {', '.join(deps)})" if deps else " (dependencies: none)"
            review = ",".join(meta.get("review_stack") or []) or "none"
            lines.append(
                f"- {p.pr_id}: {meta['title']}{dep_str} "
                f"[risk={meta.get('risk_class', 'unknown')}, merge={meta.get('merge_policy', 'human')}, "
                f"review={review}]"
            )

    blocked_prs = [p for p in prs if p.state == "blocked"]
    if blocked_prs:
        lines.append("")
        lines.append("### 🚧 Blocked PRs")
        for p in blocked_prs:
            blocking = p.provenance.get("blocking_dependencies", [])
            block_str = f" (waiting for: {', '.join(blocking)})" if blocking else ""
            lines.append(f"- {p.pr_id}: {p.metadata['title']}{block_str}")

    # Drift warnings
    if result.drift_warnings:
        lines.append("")
        lines.append("## Drift Warnings")
        for w in result.drift_warnings:
            icon = "🔴" if w.severity == "blocking" else ("🟡" if w.severity == "warning" else "ℹ️")
            lines.append(f"- {icon} [{w.severity.upper()}] {w.message}")

    # Dependency flow
    if total > 0:
        lines.append("")
        lines.append("## Dependency Flow")
        lines.append("```")
        for p in prs:
            deps = p.metadata.get("dependencies") or []
            if not deps:
                lines.append(f"{p.pr_id} (no dependencies)")
            else:
                lines.append(f"{', '.join(deps)} → {p.pr_id}")
        lines.append("```")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Projection file writers
# ---------------------------------------------------------------------------

def _build_queue_state_json(result: ReconcileResult) -> Dict[str, Any]:
    """Build pr_queue_state.json payload from reconciled state."""
    feature_meta: Dict[str, Any] = {}
    if result.prs:
        meta = result.prs[0].metadata
        feature_meta = {
            "risk_class": meta.get("risk_class", "medium"),
            "merge_policy": meta.get("merge_policy", "human"),
            "review_stack": meta.get("review_stack", []),
        }

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
            "risk_class": meta.get("risk_class", "medium"),
            "merge_policy": meta.get("merge_policy", "human"),
            "review_stack": meta.get("review_stack", []),
            "status": _state_to_status(p.state),
            "provenance": p.provenance,
        })

    completed = [p.pr_id for p in result.prs if p.state == "completed"]
    active = [p.pr_id for p in result.prs if p.state == "active"]
    blocked = [p.pr_id for p in result.prs if p.state == "blocked"]

    return {
        "feature": result.feature_name,
        "feature_metadata": feature_meta,
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

def repair_projections(
    result: ReconcileResult,
    state_dir: Path,
    project_root: Path,
) -> None:
    """Overwrite projection files with reconciled state (Section 5.3)."""
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

    # PR_QUEUE.md
    queue_md_path = project_root / "PR_QUEUE.md"
    queue_md_path.write_text(_render_pr_queue_md(result), encoding="utf-8")

    print(f"[ok] Written: {state_json_path}", file=sys.stderr)
    print(f"[ok] Written: {queue_json_path}", file=sys.stderr)
    print(f"[ok] Written: {yaml_path}", file=sys.stderr)
    print(f"[ok] Written: {queue_md_path}", file=sys.stderr)


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
        "--feature-plan",
        default=None,
        help="Path to FEATURE_PLAN.md (default: PROJECT_ROOT/FEATURE_PLAN.md)",
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

    project_root = Path(paths["PROJECT_ROOT"])
    state_dir = Path(args.state_dir or paths["VNX_STATE_DIR"])
    dispatch_dir = Path(args.dispatch_dir or paths["VNX_DISPATCH_DIR"])
    receipts_file = Path(args.receipts_file or (state_dir / "t0_receipts.ndjson"))
    feature_plan = Path(args.feature_plan or (project_root / "FEATURE_PLAN.md"))

    if not feature_plan.is_file():
        print(f"[x] FEATURE_PLAN.md not found: {feature_plan}", file=sys.stderr)
        return 2

    projection_file = state_dir / "pr_queue_state.json"

    reconciler = QueueReconciler(
        dispatch_dir=dispatch_dir,
        receipts_file=receipts_file,
        feature_plan=feature_plan,
        projection_file=projection_file if projection_file.is_file() else None,
    )

    result = reconciler.reconcile()

    if args.json_output:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        # Human-readable summary
        print(f"Feature: {result.feature_name}")
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
        repair_projections(result, state_dir, project_root)

    return 1 if result.has_blocking_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
