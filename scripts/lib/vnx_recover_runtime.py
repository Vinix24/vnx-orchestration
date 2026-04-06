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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from runtime_coordination import (
    DISPATCH_TRANSITIONS,
    InvalidTransitionError,
    get_connection,
    init_schema,
    transition_dispatch,
    _append_event,
)
from lease_manager import LeaseManager
from runtime_reconciler import ReconcilerConfig, RuntimeReconciler
from vnx_doctor_runtime import run_runtime_checks, FAIL
from incident_log import (
    generate_incident_summary,
    get_active_incidents,
    resolve_incident,
    reset_budget,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECOVERY_ACTOR = "vnx_recover"
"""Actor identity used in all coordination events emitted by recovery."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RecoveryAction:
    """A single recovery action taken or skipped."""
    phase: str          # "preflight" | "lease" | "incident" | "tmux" | "dispatch" | "cutover"
    action: str         # What was done
    target: str         # Entity affected
    outcome: str        # "applied" | "skipped" | "blocked" | "error"
    detail: str = ""    # Human-readable explanation


@dataclass
class RecoveryReport:
    """Full recovery report for operator/T0 review."""
    run_at: str
    dry_run: bool
    preflight_status: str = ""        # "pass" | "warn" | "fail"
    preflight_blockers: List[str] = field(default_factory=list)
    actions: List[RecoveryAction] = field(default_factory=list)
    escalation_items: List[Dict[str, Any]] = field(default_factory=list)
    remaining_blockers: List[str] = field(default_factory=list)
    incident_summary: Dict[str, Any] = field(default_factory=dict)
    leases_reconciled: int = 0
    dispatches_reconciled: int = 0
    tmux_remapped: int = 0
    incidents_resolved: int = 0
    budgets_reset: int = 0

    @property
    def overall_status(self) -> str:
        if self.remaining_blockers:
            return "blocked"
        if any(a.outcome == "error" for a in self.actions):
            return "partial"
        # Informational phases (preflight, cutover) don't count as "recovery work"
        recovery_phases = {"lease", "dispatch", "incident", "tmux"}
        recovery_actions = [a for a in self.actions if a.phase in recovery_phases]
        if not recovery_actions or all(a.outcome == "skipped" for a in recovery_actions):
            return "clean"
        # Check if any real reconciliation work was done
        if self.leases_reconciled == 0 and self.dispatches_reconciled == 0 and self.tmux_remapped == 0 and self.incidents_resolved == 0 and self.budgets_reset == 0:
            return "clean"
        return "recovered"

    def summary_text(self) -> str:
        """Generate operator-readable summary."""
        lines = [
            f"VNX Recovery Report — {self.run_at}",
            f"Mode: {'dry-run' if self.dry_run else 'live'}",
            f"Status: {self.overall_status.upper()}",
            "",
        ]

        if self.preflight_blockers:
            lines.append("Preflight:")
            for b in self.preflight_blockers:
                lines.append(f"  {b}")
            lines.append("")

        # Group actions by phase
        phases = {}
        for a in self.actions:
            phases.setdefault(a.phase, []).append(a)

        for phase, acts in phases.items():
            lines.append(f"{phase.upper()} ({len(acts)} action(s)):")
            for a in acts:
                icon = {"applied": "+", "skipped": "~", "blocked": "X", "error": "!"}
                lines.append(f"  [{icon.get(a.outcome, '?')}] {a.action}: {a.target}")
                if a.detail:
                    lines.append(f"      {a.detail}")
            lines.append("")

        lines.append("Totals:")
        lines.append(f"  Leases reconciled:    {self.leases_reconciled}")
        lines.append(f"  Dispatches reconciled:{self.dispatches_reconciled}")
        lines.append(f"  tmux remapped:        {self.tmux_remapped}")
        lines.append(f"  Incidents resolved:   {self.incidents_resolved}")
        lines.append(f"  Budgets reset:        {self.budgets_reset}")

        if self.escalation_items:
            lines.append("")
            lines.append(f"Escalation items ({len(self.escalation_items)}):")
            for item in self.escalation_items:
                lines.append(
                    f"  [{item.get('incident_class', '?')}] "
                    f"{item.get('dispatch_id', '?')}: {item.get('reason', '?')}"
                )

        if self.remaining_blockers:
            lines.append("")
            lines.append("REMAINING BLOCKERS:")
            for b in self.remaining_blockers:
                lines.append(f"  {b}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_at": self.run_at,
            "dry_run": self.dry_run,
            "overall_status": self.overall_status,
            "preflight_status": self.preflight_status,
            "preflight_blockers": self.preflight_blockers,
            "actions": [
                {
                    "phase": a.phase,
                    "action": a.action,
                    "target": a.target,
                    "outcome": a.outcome,
                    "detail": a.detail,
                }
                for a in self.actions
            ],
            "escalation_items": self.escalation_items,
            "remaining_blockers": self.remaining_blockers,
            "incident_summary": self.incident_summary,
            "leases_reconciled": self.leases_reconciled,
            "dispatches_reconciled": self.dispatches_reconciled,
            "tmux_remapped": self.tmux_remapped,
            "incidents_resolved": self.incidents_resolved,
            "budgets_reset": self.budgets_reset,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# Recovery phases
# ---------------------------------------------------------------------------

def _phase_preflight(
    state_dir: Path,
    report: RecoveryReport,
) -> bool:
    """Run doctor preflight. Returns True if recovery can proceed."""
    doctor_report = run_runtime_checks(state_dir)
    report.preflight_status = doctor_report.overall_status

    for item in doctor_report.recovery_preflight:
        report.preflight_blockers.append(item)

    # Check for hard blockers (schema missing, etc.)
    has_hard_blocker = False
    for item in doctor_report.recovery_preflight:
        if item.startswith("BLOCKER:"):
            # Schema missing or lease table missing are hard blockers
            if "cannot recover without database" in item or "lease reconciliation impossible" in item:
                has_hard_blocker = True
                report.remaining_blockers.append(item)

    if has_hard_blocker:
        report.actions.append(RecoveryAction(
            phase="preflight",
            action="doctor_preflight",
            target="runtime",
            outcome="blocked",
            detail="Hard blockers detected — recovery cannot proceed",
        ))
        return False

    report.actions.append(RecoveryAction(
        phase="preflight",
        action="doctor_preflight",
        target="runtime",
        outcome="applied",
        detail=f"Doctor status: {doctor_report.overall_status} "
               f"({doctor_report.pass_count}P/{doctor_report.warn_count}W/{doctor_report.fail_count}F)",
    ))
    return True


def _phase_lease_reconciliation(
    state_dir: Path,
    report: RecoveryReport,
    dry_run: bool,
) -> None:
    """Reconcile leases: expire stale, recover expired to idle."""
    lease_mgr = LeaseManager(state_dir)

    # Step 1: Expire stale leases (TTL elapsed)
    if not dry_run:
        expired = lease_mgr.expire_stale(
            actor=RECOVERY_ACTOR,
            reason="vnx recover: TTL elapsed",
        )
    else:
        # Dry-run: detect but don't act
        expired = []
        for lease in lease_mgr.list_all():
            if lease.state == "leased" and lease_mgr.is_expired_by_ttl(lease.terminal_id):
                expired.append(lease.terminal_id)

    for tid in expired:
        report.actions.append(RecoveryAction(
            phase="lease",
            action="expire_stale",
            target=tid,
            outcome="applied" if not dry_run else "skipped",
            detail="Lease TTL elapsed — expired",
        ))
        report.leases_reconciled += 1

    # Step 2: Recover expired leases to idle
    all_leases = lease_mgr.list_all()
    for lease in all_leases:
        if lease.state == "expired":
            if not dry_run:
                try:
                    lease_mgr.recover(
                        lease.terminal_id,
                        actor=RECOVERY_ACTOR,
                        reason="vnx recover: recovering expired lease to idle",
                    )
                    report.actions.append(RecoveryAction(
                        phase="lease",
                        action="recover_to_idle",
                        target=lease.terminal_id,
                        outcome="applied",
                        detail=f"Expired lease recovered to idle (was dispatch={lease.dispatch_id})",
                    ))
                    report.leases_reconciled += 1
                except (InvalidTransitionError, KeyError) as exc:
                    report.actions.append(RecoveryAction(
                        phase="lease",
                        action="recover_to_idle",
                        target=lease.terminal_id,
                        outcome="error",
                        detail=str(exc),
                    ))
            else:
                report.actions.append(RecoveryAction(
                    phase="lease",
                    action="recover_to_idle",
                    target=lease.terminal_id,
                    outcome="skipped",
                    detail=f"Would recover expired lease (dispatch={lease.dispatch_id})",
                ))
                report.leases_reconciled += 1

    # Step 3: Detect lease/dispatch coherence issues
    with get_connection(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT tl.terminal_id, tl.state AS lease_state, tl.dispatch_id,
                   d.state AS dispatch_state
            FROM terminal_leases tl
            LEFT JOIN dispatches d ON tl.dispatch_id = d.dispatch_id
            WHERE tl.state = 'leased' AND tl.dispatch_id IS NOT NULL
            """
        ).fetchall()

        for row in rows:
            dispatch_state = row["dispatch_state"]
            tid = row["terminal_id"]
            dispatch_id = row["dispatch_id"]

            if dispatch_state in ("completed", "expired", "dead_letter"):
                if not dry_run:
                    try:
                        # Release the lease since its dispatch is terminal
                        lease = lease_mgr.get(tid)
                        if lease:
                            lease_mgr.release(
                                tid,
                                lease.generation,
                                actor=RECOVERY_ACTOR,
                                reason=f"vnx recover: dispatch {dispatch_id} is {dispatch_state}",
                            )
                        report.actions.append(RecoveryAction(
                            phase="lease",
                            action="release_orphan",
                            target=tid,
                            outcome="applied",
                            detail=f"Released lease — dispatch {dispatch_id} is '{dispatch_state}'",
                        ))
                        report.leases_reconciled += 1
                    except Exception as exc:
                        report.actions.append(RecoveryAction(
                            phase="lease",
                            action="release_orphan",
                            target=tid,
                            outcome="error",
                            detail=str(exc),
                        ))
                else:
                    report.actions.append(RecoveryAction(
                        phase="lease",
                        action="release_orphan",
                        target=tid,
                        outcome="skipped",
                        detail=f"Would release — dispatch {dispatch_id} is '{dispatch_state}'",
                    ))
                    report.leases_reconciled += 1

    # Step 4: Project updated lease state to terminal_state.json
    if not dry_run:
        try:
            lease_mgr.project_to_file()
            report.actions.append(RecoveryAction(
                phase="lease",
                action="project_state",
                target="terminal_state.json",
                outcome="applied",
                detail="Projected canonical lease state to terminal_state.json",
            ))
        except Exception as exc:
            report.actions.append(RecoveryAction(
                phase="lease",
                action="project_state",
                target="terminal_state.json",
                outcome="error",
                detail=str(exc),
            ))


def _phase_dispatch_reconciliation(
    state_dir: Path,
    report: RecoveryReport,
    dry_run: bool,
) -> None:
    """Reconcile dispatches: use RuntimeReconciler for stuck/timed-out dispatches."""
    config = ReconcilerConfig(
        auto_recover_expired_leases=False,  # Already handled in lease phase
        auto_recover_dispatches=False,       # Flag for review, don't auto-recover
        dispatch_stuck_seconds=300,          # 5 minutes
    )
    reconciler = RuntimeReconciler(state_dir, config=config)
    result = reconciler.run(dry_run=dry_run)

    for action in result.timed_out_dispatches:
        report.actions.append(RecoveryAction(
            phase="dispatch",
            action="timeout_stuck",
            target=action.entity_id,
            outcome="applied" if not dry_run else "skipped",
            detail=action.reason,
        ))
        report.dispatches_reconciled += 1

    for action in result.expired_dispatches:
        report.actions.append(RecoveryAction(
            phase="dispatch",
            action="expire_over_attempted",
            target=action.entity_id,
            outcome="applied" if not dry_run else "skipped",
            detail=action.reason,
        ))
        report.dispatches_reconciled += 1

    for action in result.failed_attempts:
        report.actions.append(RecoveryAction(
            phase="dispatch",
            action="fail_orphan_attempt",
            target=action.entity_id,
            outcome="applied" if not dry_run else "skipped",
            detail=action.reason,
        ))

    for action in result.needs_review:
        report.escalation_items.append({
            "entity_type": action.entity_type,
            "dispatch_id": action.entity_id,
            "reason": action.reason,
            "incident_class": "dispatch_stuck",
            "from_state": action.from_state,
        })

    for err in result.errors:
        report.actions.append(RecoveryAction(
            phase="dispatch",
            action="reconciliation_error",
            target="runtime",
            outcome="error",
            detail=err,
        ))


def _phase_headless_reconciliation(
    state_dir: Path,
    report: RecoveryReport,
    dry_run: bool,
) -> None:
    """Reconcile headless runs: detect stale/hung runs, transition to failed.

    PR-3: Uses headless observability signals (heartbeat, last_output) to
    identify runs that are stuck and transition them to terminal state so
    operators get clear diagnostics instead of ambiguous "running" entries.
    """
    try:
        from headless_run_registry import HeadlessRunRegistry
    except ImportError:
        report.actions.append(RecoveryAction(
            phase="headless",
            action="import_check",
            target="headless_run_registry",
            outcome="skipped",
            detail="headless_run_registry module not available",
        ))
        return

    registry = HeadlessRunRegistry(state_dir)
    headless_reconciled = 0

    # Detect stale heartbeats (running but no heartbeat for > 2x interval)
    stale_runs = registry.list_stale()
    for run in stale_runs:
        if not dry_run:
            try:
                registry.transition(
                    run.run_id,
                    "failing",
                    actor=RECOVERY_ACTOR,
                    reason="vnx recover: stale heartbeat detected",
                )
                registry.transition(
                    run.run_id,
                    "failed",
                    failure_class="INFRA_FAIL",
                    actor=RECOVERY_ACTOR,
                    reason="vnx recover: run failed due to stale heartbeat",
                )
                headless_reconciled += 1
            except Exception as exc:
                report.actions.append(RecoveryAction(
                    phase="headless",
                    action="fail_stale_run",
                    target=run.run_id,
                    outcome="error",
                    detail=str(exc),
                ))
                continue

        report.actions.append(RecoveryAction(
            phase="headless",
            action="fail_stale_run",
            target=run.run_id[:12],
            outcome="applied" if not dry_run else "skipped",
            detail=f"Stale heartbeat — dispatch {run.dispatch_id[:12]}",
        ))

    # Detect hung runs (running but no output for > threshold)
    hung_runs = registry.list_hung()
    for run in hung_runs:
        # Skip if already handled as stale above
        if any(r.run_id == run.run_id for r in stale_runs):
            continue

        if not dry_run:
            try:
                registry.transition(
                    run.run_id,
                    "failing",
                    actor=RECOVERY_ACTOR,
                    reason="vnx recover: no-output hang detected",
                )
                registry.transition(
                    run.run_id,
                    "failed",
                    failure_class="NO_OUTPUT",
                    actor=RECOVERY_ACTOR,
                    reason="vnx recover: run failed due to no-output hang",
                )
                headless_reconciled += 1
            except Exception as exc:
                report.actions.append(RecoveryAction(
                    phase="headless",
                    action="fail_hung_run",
                    target=run.run_id,
                    outcome="error",
                    detail=str(exc),
                ))
                continue

        report.actions.append(RecoveryAction(
            phase="headless",
            action="fail_hung_run",
            target=run.run_id[:12],
            outcome="applied" if not dry_run else "skipped",
            detail=f"No-output hang — dispatch {run.dispatch_id[:12]}",
        ))

    # Detect init runs that never started (stuck in init)
    init_runs = registry.list_by_state("init")
    for run in init_runs:
        if run.started_at:
            from headless_run_registry import _seconds_since
            age = _seconds_since(run.started_at)
            if age > 300:  # 5 minutes stuck in init
                if not dry_run:
                    try:
                        registry.transition(
                            run.run_id,
                            "running",
                            actor=RECOVERY_ACTOR,
                            reason="vnx recover: forcing init->running for stuck run",
                        )
                        registry.transition(
                            run.run_id,
                            "failing",
                            actor=RECOVERY_ACTOR,
                            reason="vnx recover: init stuck for >5m",
                        )
                        registry.transition(
                            run.run_id,
                            "failed",
                            failure_class="INFRA_FAIL",
                            actor=RECOVERY_ACTOR,
                            reason="vnx recover: run never started",
                        )
                        headless_reconciled += 1
                    except Exception as exc:
                        report.actions.append(RecoveryAction(
                            phase="headless",
                            action="fail_stuck_init",
                            target=run.run_id,
                            outcome="error",
                            detail=str(exc),
                        ))
                        continue

                report.actions.append(RecoveryAction(
                    phase="headless",
                    action="fail_stuck_init",
                    target=run.run_id[:12],
                    outcome="applied" if not dry_run else "skipped",
                    detail=f"Stuck in init for {age:.0f}s — dispatch {run.dispatch_id[:12]}",
                ))

    if headless_reconciled == 0 and not stale_runs and not hung_runs:
        report.actions.append(RecoveryAction(
            phase="headless",
            action="check_headless_runs",
            target="headless_registry",
            outcome="applied",
            detail="No stuck headless runs detected",
        ))


def _phase_incident_reconciliation(
    state_dir: Path,
    report: RecoveryReport,
    dry_run: bool,
) -> None:
    """Generate incident summary and resolve process-crash incidents that are no longer active."""
    summary = generate_incident_summary(state_dir)
    report.incident_summary = summary

    # Collect pending escalations for the report
    with get_connection(state_dir) as conn:
        esc_rows = conn.execute(
            """
            SELECT * FROM escalation_log
            WHERE acknowledged = 0
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()

        for row in esc_rows:
            r = dict(row)
            report.escalation_items.append({
                "escalation_id": r["escalation_id"],
                "incident_id": r["incident_id"],
                "dispatch_id": r.get("dispatch_id"),
                "terminal_id": r.get("terminal_id"),
                "incident_class": r["incident_class"],
                "severity": r["severity"],
                "reason": r["reason"],
                "auto_recovery_halted": bool(r.get("auto_recovery_halted", 0)),
            })

    # Resolve process_crash incidents where the process is no longer crashing
    # (i.e., recovery has been completed by the existing supervisor)
    active = get_active_incidents(
        state_dir,
        incident_class="process_crash",
    )
    for incident in active:
        # Only auto-resolve open (not escalated) process crashes
        if incident["state"] == "open":
            if not dry_run:
                try:
                    resolve_incident(
                        state_dir,
                        incident["incident_id"],
                        actor=RECOVERY_ACTOR,
                    )
                    report.incidents_resolved += 1
                except Exception:
                    pass  # Non-fatal
            else:
                report.incidents_resolved += 1

    report.actions.append(RecoveryAction(
        phase="incident",
        action="generate_summary",
        target="incident_log",
        outcome="applied",
        detail=(
            f"Open: {summary['total_open']}, "
            f"Escalated: {summary['total_escalated']}, "
            f"Critical: {summary['critical_count']}, "
            f"Budgets exhausted: {summary['budgets_exhausted']}"
        ),
    ))

    # Reset exhausted budgets for entities that have been reconciled
    # (leases recovered, dispatches timed out / expired)
    for budget in summary.get("exhausted_budgets", []):
        if not dry_run:
            try:
                reset_budget(
                    state_dir,
                    entity_type=budget["entity_type"],
                    entity_id=budget["entity_id"],
                    incident_class=budget["incident_class"],
                    actor=RECOVERY_ACTOR,
                )
                report.budgets_reset += 1
            except Exception:
                pass  # Non-fatal
        else:
            report.budgets_reset += 1


def _phase_tmux_reconciliation(
    state_dir: Path,
    report: RecoveryReport,
    dry_run: bool,
) -> None:
    """Reconcile tmux bindings using session profile identity."""
    try:
        from tmux_session_profile import (
            load_session_profile,
            verify_profile_integrity,
            remap_pane_in_profile,
            save_session_profile,
            profile_to_panes_json,
        )
    except ImportError:
        report.actions.append(RecoveryAction(
            phase="tmux",
            action="import_check",
            target="tmux_session_profile",
            outcome="skipped",
            detail="tmux_session_profile module not available",
        ))
        return

    profile = load_session_profile(state_dir)
    if profile is None:
        report.actions.append(RecoveryAction(
            phase="tmux",
            action="profile_check",
            target="session_profile.json",
            outcome="skipped",
            detail="No session profile found — tmux reconciliation skipped",
        ))
        return

    drift = verify_profile_integrity(profile)

    if drift.is_clean:
        report.actions.append(RecoveryAction(
            phase="tmux",
            action="verify_profile",
            target=profile.session_name,
            outcome="applied",
            detail="All pane IDs correct — no remap needed",
        ))
        return

    # Remap stale panes
    remapped = 0
    for terminal_id, new_pane_id in drift.remap_candidates.items():
        if not dry_run:
            ok = remap_pane_in_profile(profile, terminal_id, new_pane_id)
            if ok:
                remapped += 1
                report.actions.append(RecoveryAction(
                    phase="tmux",
                    action="remap_pane",
                    target=terminal_id,
                    outcome="applied",
                    detail=f"Remapped to pane {new_pane_id}",
                ))
        else:
            remapped += 1
            report.actions.append(RecoveryAction(
                phase="tmux",
                action="remap_pane",
                target=terminal_id,
                outcome="skipped",
                detail=f"Would remap to pane {new_pane_id}",
            ))

    if remapped > 0 and not dry_run:
        save_session_profile(profile, state_dir)
        # Update panes.json from profile
        panes_path = state_dir / "panes.json"
        updated_panes = profile_to_panes_json(profile)
        if panes_path.exists():
            try:
                existing = json.loads(panes_path.read_text(encoding="utf-8"))
                existing.update(updated_panes)
                panes_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                panes_path.write_text(json.dumps(updated_panes, indent=2), encoding="utf-8")

    report.tmux_remapped = remapped

    # Report missing terminals as remaining blockers
    for tid in drift.missing:
        report.remaining_blockers.append(
            f"Terminal {tid} not found in tmux session — requires manual restart or full session rebuild"
        )
        report.actions.append(RecoveryAction(
            phase="tmux",
            action="detect_missing",
            target=tid,
            outcome="blocked",
            detail="Terminal pane missing and work_dir not found in live session",
        ))


def _phase_cutover_check(
    state_dir: Path,
    report: RecoveryReport,
) -> None:
    """Verify runtime core cutover status and add rollback guidance."""
    from runtime_core import runtime_primary_active

    is_primary = runtime_primary_active()

    report.actions.append(RecoveryAction(
        phase="cutover",
        action="runtime_core_status",
        target="VNX_RUNTIME_PRIMARY",
        outcome="applied",
        detail=f"Runtime core {'ACTIVE' if is_primary else 'INACTIVE (legacy mode)'}",
    ))

    if is_primary:
        report.actions.append(RecoveryAction(
            phase="cutover",
            action="rollback_guidance",
            target="runtime_core",
            outcome="applied",
            detail=(
                "Rollback: python scripts/rollback_runtime_core.py rollback && vnx start"
            ),
        ))


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
