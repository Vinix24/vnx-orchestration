#!/usr/bin/env python3
"""
VNX Safe Autonomy Cutover — PR-5 orchestration layer.

Manages the transition from shadow mode to enforced autonomy evaluation
and provenance enforcement. Provides prerequisite validation, rollback
controls, and cutover status reporting.

Feature flags controlled:
  VNX_AUTONOMY_EVALUATION:     "0" = shadow, "1" = enforced
  VNX_PROVENANCE_ENFORCEMENT:  "0" = shadow, "1" = enforced

Governance invariants:
  - G-R4: Merge and completion authority remain with T0/operator
  - A-R10: Cutover is reversible by feature flag or policy switch
  - A-R9: No silent policy mutation from recommendation logic
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from governance_evaluator import (
    DECISION_TYPE_REGISTRY,
    ESCALATION_LEVELS,
    POLICY_CLASSES,
    POLICY_VERSION,
    escalation_summary,
    is_enforcement_enabled,
)
from provenance_verification import (
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARNING,
    governance_audit_view,
    pre_merge_advisory,
    provenance_audit_view,
    verify_batch,
)
from runtime_coordination import _append_event, _now_utc
from trace_token_validator import EnforcementMode, get_enforcement_mode


# ---------------------------------------------------------------------------
# Cutover phases
# ---------------------------------------------------------------------------

PHASE_SHADOW = "shadow"
PHASE_PROVENANCE_ONLY = "provenance_only"
PHASE_FULL_ENFORCEMENT = "full_enforcement"
PHASE_ROLLBACK = "rollback"

PHASE_DESCRIPTIONS = {
    PHASE_SHADOW: "All evaluations advisory-only; no blocking",
    PHASE_PROVENANCE_ONLY: "Provenance enforcement active; autonomy evaluation advisory",
    PHASE_FULL_ENFORCEMENT: "Both autonomy evaluation and provenance enforcement active",
    PHASE_ROLLBACK: "All enforcement disabled; pre-FP-D behavior",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PrerequisiteCheck:
    """Result of a single prerequisite validation."""
    name: str
    passed: bool
    description: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "description": self.description,
            "evidence": self.evidence,
        }


@dataclass
class CutoverStatus:
    """Current state of the safe autonomy cutover."""
    phase: str
    autonomy_enforcement: bool
    provenance_enforcement: bool
    prerequisites_met: bool
    prerequisites: List[PrerequisiteCheck] = field(default_factory=list)
    escalation_health: Optional[Dict[str, Any]] = None
    provenance_health: Optional[Dict[str, Any]] = None
    residual_risks: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_description": PHASE_DESCRIPTIONS.get(self.phase, "unknown"),
            "autonomy_enforcement": self.autonomy_enforcement,
            "provenance_enforcement": self.provenance_enforcement,
            "prerequisites_met": self.prerequisites_met,
            "prerequisites": [p.to_dict() for p in self.prerequisites],
            "escalation_health": self.escalation_health,
            "provenance_health": self.provenance_health,
            "residual_risks": self.residual_risks,
        }


@dataclass
class RollbackResult:
    """Result of a rollback operation."""
    success: bool
    previous_phase: str
    new_phase: str
    actions_taken: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "previous_phase": self.previous_phase,
            "new_phase": self.new_phase,
            "actions_taken": self.actions_taken,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------

def detect_current_phase() -> str:
    """Detect the current cutover phase from feature flag state."""
    autonomy = is_enforcement_enabled()
    provenance = get_enforcement_mode() == EnforcementMode.ENFORCED

    if autonomy and provenance:
        return PHASE_FULL_ENFORCEMENT
    if provenance and not autonomy:
        return PHASE_PROVENANCE_ONLY
    if not autonomy and not provenance:
        return PHASE_SHADOW
    # autonomy without provenance is an unusual state, treat as shadow
    return PHASE_SHADOW


# ---------------------------------------------------------------------------
# Prerequisite validation
# ---------------------------------------------------------------------------

def validate_prerequisites(
    conn: sqlite3.Connection,
    receipts_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> List[PrerequisiteCheck]:
    """Validate all prerequisites for safe autonomy cutover.

    Checks:
      1. Policy matrix completeness
      2. Governance evaluator operational
      3. Escalation state machine functional
      4. Receipt provenance layer available
      5. Git traceability hooks present
      6. Provenance verification operational
      7. No unresolved blocking escalations
      8. Merge/completion authority preserved
    """
    checks: List[PrerequisiteCheck] = []

    # 1. Policy matrix completeness
    automatic_count = sum(1 for _, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "automatic")
    gated_count = sum(1 for _, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "gated")
    forbidden_count = sum(1 for _, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "forbidden")
    total = len(DECISION_TYPE_REGISTRY)

    checks.append(PrerequisiteCheck(
        name="policy_matrix_complete",
        passed=total >= 30 and automatic_count > 0 and gated_count > 0 and forbidden_count > 0,
        description="Policy matrix has all action classes populated",
        evidence={
            "total_decision_types": total,
            "automatic": automatic_count,
            "gated": gated_count,
            "forbidden": forbidden_count,
            "policy_version": POLICY_VERSION,
        },
    ))

    # 2. Governance evaluator schema present
    schema_ok = _check_table_exists(conn, "escalation_state") and _check_table_exists(conn, "governance_overrides")
    checks.append(PrerequisiteCheck(
        name="governance_schema_ready",
        passed=schema_ok,
        description="Governance evaluator tables exist (escalation_state, governance_overrides)",
        evidence={"escalation_state": _check_table_exists(conn, "escalation_state"),
                  "governance_overrides": _check_table_exists(conn, "governance_overrides")},
    ))

    # 3. Provenance registry schema present
    registry_ok = _check_table_exists(conn, "provenance_registry")
    checks.append(PrerequisiteCheck(
        name="provenance_registry_ready",
        passed=registry_ok,
        description="Provenance registry table exists",
        evidence={"provenance_registry": registry_ok},
    ))

    # 4. Verification table present
    verification_ok = _check_table_exists(conn, "provenance_verifications")
    checks.append(PrerequisiteCheck(
        name="verification_table_ready",
        passed=verification_ok,
        description="Provenance verifications table exists",
        evidence={"provenance_verifications": verification_ok},
    ))

    # 5. Git hooks present
    hooks_dir = (repo_root or Path.cwd()) / "hooks" / "git"
    prepare_hook = hooks_dir / "prepare-commit-msg"
    commit_hook = hooks_dir / "commit-msg"
    hooks_present = prepare_hook.exists() and commit_hook.exists()
    checks.append(PrerequisiteCheck(
        name="git_hooks_present",
        passed=hooks_present,
        description="Git traceability hooks installed",
        evidence={
            "prepare-commit-msg": prepare_hook.exists(),
            "commit-msg": commit_hook.exists(),
        },
    ))

    # 6. No unresolved blocking escalations
    summary = escalation_summary(conn)
    no_blockers = summary["blocking_count"] == 0
    checks.append(PrerequisiteCheck(
        name="no_blocking_escalations",
        passed=no_blockers,
        description="No unresolved hold/escalate escalations blocking cutover",
        evidence={
            "blocking_count": summary["blocking_count"],
            "holds": summary["holds"],
            "escalations": summary["escalations"],
        },
    ))

    # 7. Merge and completion remain gated (structural check)
    merge_forbidden = DECISION_TYPE_REGISTRY.get("branch_merge", (None, None))[1] == "forbidden"
    force_push_forbidden = DECISION_TYPE_REGISTRY.get("force_push", (None, None))[1] == "forbidden"
    complete_gated = DECISION_TYPE_REGISTRY.get("dispatch_complete", (None, None))[1] == "gated"
    pr_close_gated = DECISION_TYPE_REGISTRY.get("pr_close", (None, None))[1] == "gated"
    authority_preserved = merge_forbidden and force_push_forbidden and complete_gated and pr_close_gated
    checks.append(PrerequisiteCheck(
        name="authority_preserved",
        passed=authority_preserved,
        description="Merge/force-push forbidden; completion/PR-close gated (G-R4)",
        evidence={
            "branch_merge": "forbidden" if merge_forbidden else "NOT forbidden",
            "force_push": "forbidden" if force_push_forbidden else "NOT forbidden",
            "dispatch_complete": "gated" if complete_gated else "NOT gated",
            "pr_close": "gated" if pr_close_gated else "NOT gated",
        },
    ))

    # 8. Policy classes coverage
    used_classes = {pc for _, (pc, _) in DECISION_TYPE_REGISTRY.items()}
    all_covered = used_classes == POLICY_CLASSES
    checks.append(PrerequisiteCheck(
        name="policy_classes_covered",
        passed=all_covered,
        description="All policy classes have at least one decision type",
        evidence={
            "covered": sorted(used_classes),
            "missing": sorted(POLICY_CLASSES - used_classes),
        },
    ))

    return checks


def _check_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Cutover status
# ---------------------------------------------------------------------------

def get_cutover_status(
    conn: sqlite3.Connection,
    receipts_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> CutoverStatus:
    """Get comprehensive cutover status for operator review."""
    phase = detect_current_phase()
    prerequisites = validate_prerequisites(conn, receipts_path, repo_root)
    all_met = all(p.passed for p in prerequisites)

    esc_health = escalation_summary(conn)

    residual_risks = _get_residual_risks(phase)

    return CutoverStatus(
        phase=phase,
        autonomy_enforcement=is_enforcement_enabled(),
        provenance_enforcement=get_enforcement_mode() == EnforcementMode.ENFORCED,
        prerequisites_met=all_met,
        prerequisites=prerequisites,
        escalation_health=esc_health,
        residual_risks=residual_risks,
    )


def _get_residual_risks(phase: str) -> List[Dict[str, str]]:
    """Return documented residual risks for the current phase."""
    risks = [
        {
            "risk": "Policy classification may need refinement after real-world evaluation",
            "mitigation": "Monitor policy evaluation distribution and escalation frequency",
            "owner": "T0",
        },
        {
            "risk": "Legacy trace token acceptance allows weaker provenance during transition",
            "mitigation": "Track legacy format usage; set sunset date via VNX_PROVENANCE_LEGACY_ACCEPTED=0",
            "owner": "T0",
        },
        {
            "risk": "Feature flag rollback may leave partial state (enriched receipts coexist with unenriched)",
            "mitigation": "Rollback is behavioral only; enriched data remains valid and backward-compatible",
            "owner": "PR-5",
        },
        {
            "risk": "Override mechanism could be abused without social controls",
            "mitigation": "Override frequency visible in audit views; T0 reviews override patterns",
            "owner": "T0",
        },
    ]

    if phase == PHASE_FULL_ENFORCEMENT:
        risks.append({
            "risk": "Enforcement may block legitimate actions if policy matrix is incomplete",
            "mitigation": "Rollback to shadow mode via VNX_AUTONOMY_EVALUATION=0",
            "owner": "Operator",
        })

    return risks


# ---------------------------------------------------------------------------
# Cutover operations
# ---------------------------------------------------------------------------

def prepare_cutover(
    conn: sqlite3.Connection,
    receipts_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run pre-cutover validation and return readiness report.

    Does NOT modify any state (A-R9 compliant).
    """
    status = get_cutover_status(conn, receipts_path, repo_root)
    prerequisites = status.prerequisites
    all_met = all(p.passed for p in prerequisites)
    failed = [p.name for p in prerequisites if not p.passed]

    report = {
        "ready": all_met,
        "current_phase": status.phase,
        "failed_prerequisites": failed,
        "prerequisites": [p.to_dict() for p in prerequisites],
        "escalation_health": status.escalation_health,
        "residual_risks": status.residual_risks,
        "recommendation": (
            "All prerequisites met. Safe to proceed with cutover."
            if all_met
            else f"Prerequisites not met: {', '.join(failed)}. Resolve before cutover."
        ),
    }

    # Emit preparation event
    _append_event(
        conn,
        event_type="cutover_prepared",
        entity_type="system",
        entity_id="fpd_cutover",
        actor="safe_autonomy_cutover",
        reason=f"Cutover preparation: {'ready' if all_met else 'not ready'}",
        metadata={
            "ready": all_met,
            "failed": failed,
            "phase": status.phase,
        },
    )

    return report


def execute_cutover(
    conn: sqlite3.Connection,
    *,
    target_phase: str = PHASE_FULL_ENFORCEMENT,
    actor: str = "t0",
    justification: str = "",
) -> Dict[str, Any]:
    """Record a cutover transition event.

    This does NOT set environment variables — the operator must set
    VNX_AUTONOMY_EVALUATION and VNX_PROVENANCE_ENFORCEMENT externally.
    This function records the governance event and returns instructions.

    Args:
        conn: Database connection.
        target_phase: Target phase to transition to.
        actor: Who is authorizing the cutover (must be t0 or operator).
        justification: Reason for cutover.
    """
    if actor not in ("t0", "operator"):
        return {
            "success": False,
            "error": f"Cutover requires t0 or operator authority, got: {actor}",
        }

    if not justification:
        return {
            "success": False,
            "error": "Cutover justification is required",
        }

    current_phase = detect_current_phase()

    flag_instructions = _phase_to_flags(target_phase)

    _append_event(
        conn,
        event_type="cutover_executed",
        entity_type="system",
        entity_id="fpd_cutover",
        actor=actor,
        from_state=current_phase,
        to_state=target_phase,
        reason=justification,
        metadata={
            "target_phase": target_phase,
            "flag_instructions": flag_instructions,
        },
    )

    return {
        "success": True,
        "previous_phase": current_phase,
        "target_phase": target_phase,
        "flag_instructions": flag_instructions,
        "message": (
            f"Cutover event recorded. Set the following environment variables "
            f"to activate {target_phase}:"
        ),
    }


def execute_rollback(
    conn: sqlite3.Connection,
    *,
    actor: str = "t0",
    justification: str = "",
) -> RollbackResult:
    """Record a rollback event to return to shadow mode.

    Like execute_cutover, this records the governance event but does not
    modify environment variables directly.
    """
    if actor not in ("t0", "operator"):
        return RollbackResult(
            success=False,
            previous_phase=detect_current_phase(),
            new_phase=detect_current_phase(),
            warnings=[f"Rollback requires t0 or operator authority, got: {actor}"],
        )

    if not justification:
        return RollbackResult(
            success=False,
            previous_phase=detect_current_phase(),
            new_phase=detect_current_phase(),
            warnings=["Rollback justification is required"],
        )

    previous_phase = detect_current_phase()

    _append_event(
        conn,
        event_type="cutover_rollback",
        entity_type="system",
        entity_id="fpd_cutover",
        actor=actor,
        from_state=previous_phase,
        to_state=PHASE_ROLLBACK,
        reason=justification,
        metadata={"rollback_from": previous_phase},
    )

    actions = [
        "Set VNX_AUTONOMY_EVALUATION=0",
        "Set VNX_PROVENANCE_ENFORCEMENT=0",
        "Existing enriched receipts remain valid (backward-compatible)",
        "Policy evaluation events continue in advisory mode",
    ]

    warnings = []
    if previous_phase == PHASE_FULL_ENFORCEMENT:
        warnings.append(
            "Rolling back from full enforcement — actions previously blocked "
            "will now proceed without policy checks"
        )

    return RollbackResult(
        success=True,
        previous_phase=previous_phase,
        new_phase=PHASE_ROLLBACK,
        actions_taken=actions,
        warnings=warnings,
    )


def _phase_to_flags(phase: str) -> Dict[str, str]:
    """Map a phase to the required environment variable settings."""
    return {
        PHASE_SHADOW: {
            "VNX_AUTONOMY_EVALUATION": "0",
            "VNX_PROVENANCE_ENFORCEMENT": "0",
        },
        PHASE_PROVENANCE_ONLY: {
            "VNX_AUTONOMY_EVALUATION": "0",
            "VNX_PROVENANCE_ENFORCEMENT": "1",
        },
        PHASE_FULL_ENFORCEMENT: {
            "VNX_AUTONOMY_EVALUATION": "1",
            "VNX_PROVENANCE_ENFORCEMENT": "1",
        },
        PHASE_ROLLBACK: {
            "VNX_AUTONOMY_EVALUATION": "0",
            "VNX_PROVENANCE_ENFORCEMENT": "0",
        },
    }.get(phase, {
        "VNX_AUTONOMY_EVALUATION": "0",
        "VNX_PROVENANCE_ENFORCEMENT": "0",
    })


# ---------------------------------------------------------------------------
# Integrated T0 review surface
# ---------------------------------------------------------------------------

def t0_review_summary(
    conn: sqlite3.Connection,
    dispatch_ids: Optional[List[str]] = None,
    receipts_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate an integrated T0 review surface combining governance and provenance.

    Combines:
      - Cutover status
      - Governance audit view
      - Provenance audit view
      - Pre-merge advisory (if dispatch_ids provided)
    """
    status = get_cutover_status(conn, receipts_path, repo_root)

    gov_audit = governance_audit_view(conn)
    prov_audit = provenance_audit_view(conn)

    merge_advisory = None
    if dispatch_ids and receipts_path:
        merge_advisory = pre_merge_advisory(conn, dispatch_ids, receipts_path, repo_root)

    return {
        "cutover": status.to_dict(),
        "governance_audit": gov_audit,
        "provenance_audit": prov_audit,
        "merge_advisory": merge_advisory,
        "generated_at": _now_utc(),
    }


# ---------------------------------------------------------------------------
# Autonomy envelope verification
# ---------------------------------------------------------------------------

def verify_autonomy_envelope(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Verify that autonomy is limited to approved automatic action classes.

    Checks that:
      1. All automatic actions map to safe policy classes
      2. All gated actions require explicit authority
      3. All forbidden actions are blocked for non-human actors
      4. Merge and completion authority remain with T0
    """
    findings: List[Dict[str, Any]] = []
    passed = True

    # Check automatic actions are within safe policy classes
    safe_auto_classes = {"operational", "dispatch_lifecycle", "recovery", "routing", "intelligence"}
    for dt, (pc, ac) in DECISION_TYPE_REGISTRY.items():
        if ac == "automatic" and pc not in safe_auto_classes:
            # Escalation class has some automatic entries (escalation_emit, hold_enter, escalate_to_t0)
            if pc == "escalation" and dt in ("escalation_emit", "hold_enter", "escalate_to_t0"):
                continue
            findings.append({
                "type": "unexpected_automatic",
                "severity": "warning",
                "decision_type": dt,
                "policy_class": pc,
                "description": f"Automatic action {dt} in non-safe policy class {pc}",
            })

    # Verify merge authority
    for merge_action in ("branch_merge", "force_push"):
        entry = DECISION_TYPE_REGISTRY.get(merge_action)
        if not entry or entry[1] != "forbidden":
            passed = False
            findings.append({
                "type": "merge_authority_violation",
                "severity": "error",
                "decision_type": merge_action,
                "description": f"{merge_action} must be forbidden for autonomous actors (G-R4)",
            })

    # Verify completion authority
    for completion_action in ("dispatch_complete", "pr_close", "feature_certify"):
        entry = DECISION_TYPE_REGISTRY.get(completion_action)
        if not entry or entry[1] != "gated":
            passed = False
            findings.append({
                "type": "completion_authority_violation",
                "severity": "error",
                "decision_type": completion_action,
                "description": f"{completion_action} must be gated (G-R4)",
            })

    # Verify configuration is gated
    for config_action in ("policy_update", "feature_flag_toggle", "budget_adjust"):
        entry = DECISION_TYPE_REGISTRY.get(config_action)
        if not entry or entry[1] != "gated":
            passed = False
            findings.append({
                "type": "config_authority_violation",
                "severity": "error",
                "decision_type": config_action,
                "description": f"{config_action} must be gated",
            })

    return {
        "passed": passed and len([f for f in findings if f["severity"] == "error"]) == 0,
        "findings": findings,
        "automatic_count": sum(1 for _, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "automatic"),
        "gated_count": sum(1 for _, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "gated"),
        "forbidden_count": sum(1 for _, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "forbidden"),
    }
