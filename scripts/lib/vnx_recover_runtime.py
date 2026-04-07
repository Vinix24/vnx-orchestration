#!/usr/bin/env python3
"""
VNX Recover Runtime — Operator-facing recovery engine against canonical state.

PR-5 deliverable: implements the bounded, governance-compatible recovery entry
point for the VNX runtime. Reconciles leases, incidents, and tmux bindings
before any resume attempt.

Design:
  - Recovery runs doctor preflight first — blocks on FAIL conditions (A-R7)
  - Lease reconciliation uses canonical lease_manager, not terminal_state.json (G-R6)
  - Incident summary is generated from durable incident_log (G-R3)
  - tmux reheal uses session profile identity, not pane folklore (A-R5, G-R4)
  - All actions are idempotent — repeated runs produce no compound incidents (A-R8)
  - Legacy recovery paths are available as fallback when runtime core is off (A-R9)
  - Recovery output includes explicit summary, escalation items, and blockers

Governance references:
  G-R3: Every recovery action must emit an incident trail
  G-R5: Dead-letter is explicit
  G-R6: vnx recover must operate on canonical runtime state
  G-R7: Operator teardown and runtime supervision remain distinct
  G-R8: Final recovery authority remains governance-aware
  A-R7: vnx recover must reconcile leases, incidents, and tmux bindings before resuming
  A-R8: Recovery commands must be idempotent
  A-R9: Legacy bash supervisor paths stay available until cutover is certified
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from runtime_coordination import (
    _append_event,
    get_connection,
)
from vnx_recovery_phases import (  # noqa: F401
    RECOVERY_ACTOR,
    RecoveryAction,
    RecoveryReport,
    _phase_cutover_check,
    _phase_dispatch_reconciliation,
    _phase_headless_reconciliation,
    _phase_incident_reconciliation,
    _phase_lease_reconciliation,
    _phase_preflight,
    _phase_tmux_reconciliation,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# Emit recovery event to coordination log
# ---------------------------------------------------------------------------

def _emit_recovery_event(
    state_dir: Path,
    report: RecoveryReport,
) -> None:
    """Write a coordination event summarising the recovery run (G-R3)."""
    with get_connection(state_dir) as conn:
        _append_event(
            conn,
            event_type="recovery_completed",
            entity_type="runtime",
            entity_id="vnx_recover",
            actor=RECOVERY_ACTOR,
            reason=f"Recovery {report.overall_status}: "
                   f"{report.leases_reconciled}L/{report.dispatches_reconciled}D/"
                   f"{report.tmux_remapped}T/{report.incidents_resolved}I",
            metadata={
                "overall_status": report.overall_status,
                "dry_run": report.dry_run,
                "leases_reconciled": report.leases_reconciled,
                "dispatches_reconciled": report.dispatches_reconciled,
                "tmux_remapped": report.tmux_remapped,
                "incidents_resolved": report.incidents_resolved,
                "budgets_reset": report.budgets_reset,
                "escalation_count": len(report.escalation_items),
                "blocker_count": len(report.remaining_blockers),
            },
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_recovery(
    state_dir: str | Path,
    *,
    dry_run: bool = False,
) -> RecoveryReport:
    """Execute the full vnx recover flow against canonical runtime state.

    Phases:
    1. Preflight — run doctor checks, abort on hard blockers
    2. Lease reconciliation — expire stale, recover expired, release orphans
    3. Dispatch reconciliation — timeout stuck, flag for review
    4. Headless reconciliation — detect stale/hung headless runs (PR-3)
    5. Incident reconciliation — summarize, resolve stale, reset budgets
    6. tmux reconciliation — verify profile, remap stale panes
    7. Cutover check — report runtime core status and rollback guidance

    Args:
        state_dir: Runtime state directory, resolved via VNX_STATE_DIR.
        dry_run: When True, detect issues but do not modify state.

    Returns:
        RecoveryReport with full audit trail for operator/T0 review.
    """
    sd = Path(state_dir)
    report = RecoveryReport(run_at=_now_utc(), dry_run=dry_run)

    # Phase 1: Preflight
    can_proceed = _phase_preflight(sd, report)
    if not can_proceed:
        return report

    # Phase 2: Lease reconciliation
    _phase_lease_reconciliation(sd, report, dry_run)

    # Phase 3: Dispatch reconciliation
    _phase_dispatch_reconciliation(sd, report, dry_run)

    # Phase 4: Headless run reconciliation (PR-3)
    _phase_headless_reconciliation(sd, report, dry_run)

    # Phase 5: Incident reconciliation
    _phase_incident_reconciliation(sd, report, dry_run)

    # Phase 6: tmux reconciliation
    _phase_tmux_reconciliation(sd, report, dry_run)

    # Phase 7: Cutover check
    _phase_cutover_check(sd, report)

    # Emit recovery event (G-R3)
    if not dry_run:
        try:
            _emit_recovery_event(sd, report)
        except Exception:
            pass  # Non-fatal — recovery itself succeeded

    return report


# ---------------------------------------------------------------------------
# CLI interface for recover.sh integration
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entry point called from recover.sh."""
    import argparse

    parser = argparse.ArgumentParser(description="VNX Runtime Recovery")
    parser.add_argument(
        "--state-dir",
        required=True,
        help="Path to .vnx-data/state directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be recovered without making changes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON",
    )
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    if not state_dir.exists():
        print(f"[recover] FAIL: State directory not found: {state_dir}", file=sys.stderr)
        return 1

    report = run_recovery(state_dir, dry_run=args.dry_run)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary_text())

    if report.overall_status == "blocked":
        return 2
    if report.overall_status == "partial":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
