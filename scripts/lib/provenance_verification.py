#!/usr/bin/env python3
"""
VNX Provenance Verification — Audit views and advisory guardrails.

Implements FP-D PR-4: verification and audit surfaces that let operators and T0
check whether the provenance chain is intact and whether autonomy decisions
stayed within policy.

Key responsibilities:
  - Verify provenance chains across dispatch, receipt, commit, and PR metadata
  - Produce audit views of policy outcomes, overrides, and broken chains
  - Surface advisory guardrails (non-mutating recommendations)
  - Log verification runs to provenance_verifications table
  - Remain compatible with existing receipt and queue flows

Governance:
  G-R7: Dispatch, receipt, commit, and PR must be bidirectionally traceable
  A-R9: No silent policy mutation from recommendation logic
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from receipt_provenance import (
    CHAIN_STATUS_BROKEN,
    CHAIN_STATUS_COMPLETE,
    CHAIN_STATUS_INCOMPLETE,
    GAP_BROKEN_CHAIN,
    GAP_MISSING_DISPATCH_ID,
    GAP_MISSING_GIT_REF,
    GAP_MISSING_RECEIPT,
    GAP_MISSING_TRACE_TOKEN,
    ProvenanceGap,
    ProvenanceLink,
    find_commits_by_dispatch,
    find_receipts_by_dispatch,
    get_provenance_link,
    validate_receipt_provenance,
)
from runtime_coordination import _append_event, _now_utc

# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

VERDICT_PASS = "pass"
VERDICT_WARNING = "warning"
VERDICT_FAIL = "fail"

# ---------------------------------------------------------------------------
# Advisory types (non-mutating recommendations per A-R9)
# ---------------------------------------------------------------------------

ADVISORY_REGISTER_PROVENANCE = "register_provenance"
ADVISORY_ENRICH_RECEIPT = "enrich_receipt"
ADVISORY_ADD_TRACE_TOKEN = "add_trace_token"
ADVISORY_LINK_COMMIT = "link_commit"
ADVISORY_RESOLVE_ESCALATION = "resolve_escalation"
ADVISORY_REVIEW_OVERRIDE = "review_override"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VerificationFinding:
    """A single finding from provenance verification."""
    finding_type: str  # gap_type or custom finding type
    severity: str      # info | warning | error
    entity_type: str
    entity_id: str
    description: str
    layer: str         # receipt | registry | git | governance

    def to_dict(self) -> Dict[str, Any]:
        return {
            "finding_type": self.finding_type,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "description": self.description,
            "layer": self.layer,
        }


@dataclass
class Advisory:
    """A non-mutating recommendation from advisory guardrails."""
    advisory_type: str
    severity: str       # info | warning
    entity_type: str
    entity_id: str
    recommendation: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "advisory_type": self.advisory_type,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "recommendation": self.recommendation,
            "evidence": self.evidence,
        }


@dataclass
class VerificationResult:
    """Result of verifying a single dispatch's provenance chain."""
    dispatch_id: str
    verdict: str  # pass | warning | fail
    chain_status: str
    findings: List[VerificationFinding] = field(default_factory=list)
    advisories: List[Advisory] = field(default_factory=list)
    registry_link: Optional[Dict[str, Any]] = None
    receipt_count: int = 0
    commit_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "verdict": self.verdict,
            "chain_status": self.chain_status,
            "findings": [f.to_dict() for f in self.findings],
            "advisories": [a.to_dict() for a in self.advisories],
            "registry_link": self.registry_link,
            "receipt_count": self.receipt_count,
            "commit_count": self.commit_count,
        }


@dataclass
class BatchVerificationResult:
    """Result of verifying multiple dispatches."""
    total: int
    verdicts: Dict[str, int]  # pass/warning/fail counts
    chain_statuses: Dict[str, int]
    dispatches: List[VerificationResult] = field(default_factory=list)
    advisories: List[Advisory] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "verdicts": self.verdicts,
            "chain_statuses": self.chain_statuses,
            "dispatches": [d.to_dict() for d in self.dispatches],
            "advisories": [a.to_dict() for a in self.advisories],
        }


# ---------------------------------------------------------------------------
# Single dispatch verification
# ---------------------------------------------------------------------------

def verify_dispatch_provenance(
    conn: sqlite3.Connection,
    dispatch_id: str,
    receipts_path: Path,
    repo_root: Optional[Path] = None,
) -> VerificationResult:
    """Verify the provenance chain for a single dispatch.

    Checks four layers:
      1. Registry — is the dispatch registered in provenance_registry?
      2. Receipts — are there receipts linked to this dispatch?
      3. Git — are there commits with trace tokens for this dispatch?
      4. Cross-layer — do registry, receipt, and git data agree?

    Returns a VerificationResult with verdict, findings, and advisories.
    """
    findings: List[VerificationFinding] = []
    advisories: List[Advisory] = []

    # Layer 1: Registry check
    link = get_provenance_link(conn, dispatch_id)
    registry_link = link.to_dict() if link else None

    if not link:
        findings.append(VerificationFinding(
            finding_type=GAP_MISSING_DISPATCH_ID,
            severity="warning",
            entity_type="provenance",
            entity_id=dispatch_id,
            description=f"Dispatch {dispatch_id} not found in provenance registry",
            layer="registry",
        ))
        advisories.append(Advisory(
            advisory_type=ADVISORY_REGISTER_PROVENANCE,
            severity="warning",
            entity_type="provenance",
            entity_id=dispatch_id,
            recommendation=f"Register provenance link for dispatch {dispatch_id}",
        ))

    # Layer 2: Receipt check
    receipts = find_receipts_by_dispatch(receipts_path, dispatch_id)
    receipt_count = len(receipts)

    if not receipts:
        findings.append(VerificationFinding(
            finding_type=GAP_MISSING_RECEIPT,
            severity="warning",
            entity_type="dispatch",
            entity_id=dispatch_id,
            description=f"No receipts found for dispatch {dispatch_id}",
            layer="receipt",
        ))
    else:
        for receipt in receipts:
            validation = validate_receipt_provenance(receipt)
            for gap in validation.gaps:
                findings.append(VerificationFinding(
                    finding_type=gap.gap_type,
                    severity=gap.severity,
                    entity_type=gap.entity_type,
                    entity_id=gap.entity_id,
                    description=gap.description,
                    layer="receipt",
                ))
            if not validation.trace_token:
                advisories.append(Advisory(
                    advisory_type=ADVISORY_ADD_TRACE_TOKEN,
                    severity="info",
                    entity_type="receipt",
                    entity_id=receipt.get("run_id") or receipt.get("task_id") or dispatch_id,
                    recommendation="Receipt missing trace_token — enrich for full traceability",
                ))

    # Layer 3: Git check
    commits = find_commits_by_dispatch(dispatch_id, repo_root)
    commit_count = len(commits)

    if not commits and receipts:
        findings.append(VerificationFinding(
            finding_type=GAP_MISSING_TRACE_TOKEN,
            severity="info",
            entity_type="dispatch",
            entity_id=dispatch_id,
            description=f"No commits found with trace token for dispatch {dispatch_id}",
            layer="git",
        ))
        advisories.append(Advisory(
            advisory_type=ADVISORY_LINK_COMMIT,
            severity="info",
            entity_type="dispatch",
            entity_id=dispatch_id,
            recommendation="Add Dispatch-ID trace token to commit messages for this dispatch",
        ))

    # Layer 4: Cross-layer consistency
    if link and receipts:
        if link.receipt_id:
            receipt_ids = {
                r.get("run_id") or r.get("task_id") for r in receipts
            }
            if link.receipt_id not in receipt_ids:
                findings.append(VerificationFinding(
                    finding_type=GAP_BROKEN_CHAIN,
                    severity="error",
                    entity_type="provenance",
                    entity_id=dispatch_id,
                    description=(
                        f"Registry receipt_id '{link.receipt_id}' not found "
                        f"in actual receipts for dispatch {dispatch_id}"
                    ),
                    layer="registry",
                ))

    if link and commits:
        if link.commit_sha and link.commit_sha not in commits:
            findings.append(VerificationFinding(
                finding_type=GAP_BROKEN_CHAIN,
                severity="error",
                entity_type="provenance",
                entity_id=dispatch_id,
                description=(
                    f"Registry commit_sha '{link.commit_sha}' not found "
                    f"in commits with trace token for dispatch {dispatch_id}"
                ),
                layer="registry",
            ))

    # Determine verdict
    chain_status, verdict = _determine_verdict(findings, link, receipt_count, commit_count)

    return VerificationResult(
        dispatch_id=dispatch_id,
        verdict=verdict,
        chain_status=chain_status,
        findings=findings,
        advisories=advisories,
        registry_link=registry_link,
        receipt_count=receipt_count,
        commit_count=commit_count,
    )


def _determine_verdict(
    findings: List[VerificationFinding],
    link: Optional[ProvenanceLink],
    receipt_count: int,
    commit_count: int,
) -> tuple:
    """Determine chain_status and verdict from findings."""
    has_errors = any(f.severity == "error" for f in findings)
    has_warnings = any(f.severity == "warning" for f in findings)

    if has_errors:
        return CHAIN_STATUS_BROKEN, VERDICT_FAIL

    if link and link.chain_status == CHAIN_STATUS_COMPLETE and receipt_count > 0:
        if has_warnings:
            return CHAIN_STATUS_COMPLETE, VERDICT_WARNING
        return CHAIN_STATUS_COMPLETE, VERDICT_PASS

    if receipt_count > 0 and commit_count > 0 and not has_warnings:
        return CHAIN_STATUS_COMPLETE, VERDICT_PASS

    if has_warnings:
        return CHAIN_STATUS_INCOMPLETE, VERDICT_WARNING

    return CHAIN_STATUS_INCOMPLETE, VERDICT_WARNING


# ---------------------------------------------------------------------------
# Record verification to audit trail
# ---------------------------------------------------------------------------

def record_verification(
    conn: sqlite3.Connection,
    result: VerificationResult,
    verified_by: str = "provenance_verification",
) -> str:
    """Record a verification run in the provenance_verifications table.

    Emits a provenance_verified coordination event.
    Returns the verification_id.
    """
    verification_id = str(uuid.uuid4())
    now = _now_utc()

    conn.execute(
        """
        INSERT INTO provenance_verifications
            (verification_id, dispatch_id, verdict, chain_status,
             findings_json, advisory_json, verified_by, verified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verification_id,
            result.dispatch_id,
            result.verdict,
            result.chain_status,
            json.dumps([f.to_dict() for f in result.findings]),
            json.dumps([a.to_dict() for a in result.advisories]),
            verified_by,
            now,
        ),
    )

    _append_event(
        conn,
        event_type="provenance_verified",
        entity_type="provenance",
        entity_id=result.dispatch_id,
        actor=verified_by,
        reason=f"Verification verdict: {result.verdict}",
        metadata={
            "verification_id": verification_id,
            "verdict": result.verdict,
            "chain_status": result.chain_status,
            "finding_count": len(result.findings),
            "advisory_count": len(result.advisories),
        },
    )

    # Update provenance_registry verified_at if registry link exists
    conn.execute(
        """
        UPDATE provenance_registry
        SET verified_at = ?, verified_by = ?
        WHERE dispatch_id = ?
        """,
        (now, verified_by, result.dispatch_id),
    )

    return verification_id


# ---------------------------------------------------------------------------
# Batch verification
# ---------------------------------------------------------------------------

def verify_batch(
    conn: sqlite3.Connection,
    dispatch_ids: List[str],
    receipts_path: Path,
    repo_root: Optional[Path] = None,
    record: bool = False,
) -> BatchVerificationResult:
    """Verify provenance for multiple dispatches.

    Args:
        conn: Database connection.
        dispatch_ids: List of dispatch IDs to verify.
        receipts_path: Path to NDJSON receipts file.
        repo_root: Git repository root (for commit lookups).
        record: If True, record each verification to the audit trail.

    Returns:
        BatchVerificationResult with aggregate statistics and per-dispatch details.
    """
    verdicts = {VERDICT_PASS: 0, VERDICT_WARNING: 0, VERDICT_FAIL: 0}
    chain_statuses = {
        CHAIN_STATUS_COMPLETE: 0,
        CHAIN_STATUS_INCOMPLETE: 0,
        CHAIN_STATUS_BROKEN: 0,
    }
    results: List[VerificationResult] = []
    batch_advisories: List[Advisory] = []

    for dispatch_id in dispatch_ids:
        result = verify_dispatch_provenance(conn, dispatch_id, receipts_path, repo_root)
        results.append(result)
        verdicts[result.verdict] = verdicts.get(result.verdict, 0) + 1
        chain_statuses[result.chain_status] = chain_statuses.get(result.chain_status, 0) + 1

        if record:
            record_verification(conn, result)

    # Batch-level advisory guardrails
    fail_count = verdicts[VERDICT_FAIL]
    warning_count = verdicts[VERDICT_WARNING]
    total = len(dispatch_ids)

    if total > 0 and fail_count / total > 0.2:
        batch_advisories.append(Advisory(
            advisory_type=ADVISORY_REGISTER_PROVENANCE,
            severity="warning",
            entity_type="batch",
            entity_id="verification_sweep",
            recommendation=(
                f"{fail_count}/{total} dispatches have broken provenance chains — "
                f"investigate systemic provenance gaps before merge"
            ),
            evidence={"fail_rate": fail_count / total},
        ))

    if total > 0 and (fail_count + warning_count) / total > 0.5:
        batch_advisories.append(Advisory(
            advisory_type=ADVISORY_ENRICH_RECEIPT,
            severity="warning",
            entity_type="batch",
            entity_id="verification_sweep",
            recommendation=(
                f"{fail_count + warning_count}/{total} dispatches have incomplete or broken "
                f"provenance — consider batch provenance enrichment"
            ),
            evidence={"issue_rate": (fail_count + warning_count) / total},
        ))

    return BatchVerificationResult(
        total=total,
        verdicts=verdicts,
        chain_statuses=chain_statuses,
        dispatches=results,
        advisories=batch_advisories,
    )


# ---------------------------------------------------------------------------
# Audit views
# ---------------------------------------------------------------------------

def governance_audit_view(
    conn: sqlite3.Connection,
    *,
    dispatch_id: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Generate an audit view of governance decisions for operator/T0 review.

    Combines policy evaluation events, escalation state, and override records
    into a unified timeline for the given dispatch (or system-wide).

    Returns a dict with:
      - policy_evaluations: recent policy evaluation events
      - escalations: current unresolved escalation states
      - overrides: recent governance override records
      - summary: aggregate counts
    """
    # Policy evaluation events
    if dispatch_id:
        eval_rows = conn.execute(
            """
            SELECT * FROM coordination_events
            WHERE event_type = 'policy_evaluation' AND entity_id = ?
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (dispatch_id, limit),
        ).fetchall()
    else:
        eval_rows = conn.execute(
            """
            SELECT * FROM coordination_events
            WHERE event_type = 'policy_evaluation'
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    evaluations = []
    outcome_counts = {"automatic": 0, "gated": 0, "forbidden": 0}
    for row in eval_rows:
        d = dict(row)
        metadata = _parse_json(d.get("metadata_json"))
        outcome = metadata.get("outcome", "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        evaluations.append({
            "event_id": d["event_id"],
            "entity": f"{d['entity_type']}:{d['entity_id']}",
            "action": metadata.get("action"),
            "outcome": outcome,
            "policy_class": metadata.get("policy_class"),
            "escalation_level": metadata.get("escalation_level"),
            "enforcement": metadata.get("enforcement"),
            "occurred_at": d["occurred_at"],
        })

    # Escalation states
    if dispatch_id:
        esc_rows = conn.execute(
            """
            SELECT * FROM escalation_state
            WHERE entity_id = ? AND resolved_at IS NULL
            ORDER BY updated_at DESC
            """,
            (dispatch_id,),
        ).fetchall()
    else:
        esc_rows = conn.execute(
            """
            SELECT * FROM escalation_state
            WHERE resolved_at IS NULL
            ORDER BY
                CASE escalation_level
                    WHEN 'escalate' THEN 3
                    WHEN 'hold' THEN 2
                    WHEN 'review_required' THEN 1
                    ELSE 0
                END DESC,
                updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    escalations = []
    esc_counts = {"info": 0, "review_required": 0, "hold": 0, "escalate": 0}
    for row in esc_rows:
        d = dict(row)
        level = d["escalation_level"]
        esc_counts[level] = esc_counts.get(level, 0) + 1
        escalations.append({
            "entity": f"{d['entity_type']}:{d['entity_id']}",
            "level": level,
            "trigger": d.get("trigger_description"),
            "policy_class": d.get("policy_class"),
            "since": d.get("updated_at"),
        })

    # Override records
    if dispatch_id:
        ovr_rows = conn.execute(
            """
            SELECT * FROM governance_overrides
            WHERE entity_id = ?
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (dispatch_id, limit),
        ).fetchall()
    else:
        ovr_rows = conn.execute(
            """
            SELECT * FROM governance_overrides
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    overrides = []
    ovr_counts = {"granted": 0, "denied": 0}
    for row in ovr_rows:
        d = dict(row)
        ovr_counts[d["outcome"]] = ovr_counts.get(d["outcome"], 0) + 1
        overrides.append({
            "override_id": d["override_id"],
            "entity": f"{d['entity_type']}:{d['entity_id']}",
            "actor": d["actor"],
            "type": d["override_type"],
            "outcome": d["outcome"],
            "justification": d["justification"],
            "previous_level": d.get("previous_level"),
            "new_level": d.get("new_level"),
            "occurred_at": d["occurred_at"],
        })

    return {
        "dispatch_id": dispatch_id,
        "policy_evaluations": evaluations,
        "escalations": escalations,
        "overrides": overrides,
        "summary": {
            "evaluation_count": len(evaluations),
            "outcome_counts": outcome_counts,
            "escalation_counts": esc_counts,
            "blocking_count": esc_counts["hold"] + esc_counts["escalate"],
            "override_counts": ovr_counts,
        },
    }


def provenance_audit_view(
    conn: sqlite3.Connection,
    *,
    dispatch_id: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Generate an audit view of provenance state for operator/T0 review.

    Shows provenance registry entries, verification history, and gap events.

    Returns a dict with:
      - registry_entries: provenance registry rows
      - verifications: recent verification runs
      - gap_events: provenance gap events
      - summary: aggregate chain status counts
    """
    # Registry entries
    if dispatch_id:
        reg_rows = conn.execute(
            "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchall()
    else:
        reg_rows = conn.execute(
            """
            SELECT * FROM provenance_registry
            ORDER BY registered_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    registry_entries = []
    status_counts = {
        CHAIN_STATUS_COMPLETE: 0,
        CHAIN_STATUS_INCOMPLETE: 0,
        CHAIN_STATUS_BROKEN: 0,
    }
    for row in reg_rows:
        d = dict(row)
        status = d["chain_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        registry_entries.append({
            "dispatch_id": d["dispatch_id"],
            "receipt_id": d.get("receipt_id"),
            "commit_sha": d.get("commit_sha"),
            "pr_number": d.get("pr_number"),
            "chain_status": status,
            "gaps": _parse_json(d.get("gaps_json")),
            "verified_at": d.get("verified_at"),
            "registered_at": d.get("registered_at"),
        })

    # Verification history
    if dispatch_id:
        ver_rows = conn.execute(
            """
            SELECT * FROM provenance_verifications
            WHERE dispatch_id = ?
            ORDER BY verified_at DESC LIMIT ?
            """,
            (dispatch_id, limit),
        ).fetchall()
    else:
        ver_rows = conn.execute(
            """
            SELECT * FROM provenance_verifications
            ORDER BY verified_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    verifications = []
    verdict_counts = {VERDICT_PASS: 0, VERDICT_WARNING: 0, VERDICT_FAIL: 0}
    for row in ver_rows:
        d = dict(row)
        verdict = d["verdict"]
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        verifications.append({
            "verification_id": d["verification_id"],
            "dispatch_id": d["dispatch_id"],
            "verdict": verdict,
            "chain_status": d["chain_status"],
            "findings": _parse_json(d.get("findings_json")),
            "advisories": _parse_json(d.get("advisory_json")),
            "verified_by": d.get("verified_by"),
            "verified_at": d["verified_at"],
        })

    # Provenance gap events
    if dispatch_id:
        gap_rows = conn.execute(
            """
            SELECT * FROM coordination_events
            WHERE event_type = 'provenance_gap' AND entity_id = ?
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (dispatch_id, limit),
        ).fetchall()
    else:
        gap_rows = conn.execute(
            """
            SELECT * FROM coordination_events
            WHERE event_type = 'provenance_gap'
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    gap_events = []
    for row in gap_rows:
        d = dict(row)
        metadata = _parse_json(d.get("metadata_json"))
        gap_events.append({
            "event_id": d["event_id"],
            "entity": f"{d['entity_type']}:{d['entity_id']}",
            "gap_type": metadata.get("gap_type"),
            "severity": metadata.get("severity"),
            "reason": d.get("reason"),
            "occurred_at": d["occurred_at"],
        })

    return {
        "dispatch_id": dispatch_id,
        "registry_entries": registry_entries,
        "verifications": verifications,
        "gap_events": gap_events,
        "summary": {
            "registry_count": len(registry_entries),
            "chain_status_counts": status_counts,
            "verification_count": len(verifications),
            "verdict_counts": verdict_counts,
            "gap_event_count": len(gap_events),
        },
    }


# ---------------------------------------------------------------------------
# Advisory guardrails
# ---------------------------------------------------------------------------

def pre_merge_advisory(
    conn: sqlite3.Connection,
    dispatch_ids: List[str],
    receipts_path: Path,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate pre-merge advisory guardrails for a set of dispatches.

    Non-mutating: reads state and produces recommendations without
    modifying policy, registry, or escalation state (A-R9).

    Designed to be run before merge/closure steps so operators can
    see provenance and governance health at a glance.

    Returns a dict with:
      - ready: bool (True if no blocking issues found)
      - blockers: list of blocking findings
      - warnings: list of warning findings
      - advisories: list of recommendations
      - governance: governance health summary
    """
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    advisories: List[Advisory] = []

    # Check provenance for each dispatch
    for dispatch_id in dispatch_ids:
        result = verify_dispatch_provenance(conn, dispatch_id, receipts_path, repo_root)

        for finding in result.findings:
            entry = finding.to_dict()
            entry["dispatch_id"] = dispatch_id
            if finding.severity == "error":
                blockers.append(entry)
            elif finding.severity == "warning":
                warnings.append(entry)

        advisories.extend(result.advisories)

    # Check governance health
    esc_rows = conn.execute(
        "SELECT * FROM escalation_state WHERE resolved_at IS NULL"
    ).fetchall()

    holds = []
    escalations = []
    for row in esc_rows:
        d = dict(row)
        if d["entity_id"] in dispatch_ids:
            entry = {
                "entity": f"{d['entity_type']}:{d['entity_id']}",
                "level": d["escalation_level"],
                "trigger": d.get("trigger_description"),
            }
            if d["escalation_level"] == "hold":
                holds.append(entry)
                blockers.append({
                    "finding_type": "escalation_hold",
                    "severity": "error",
                    "entity_type": d["entity_type"],
                    "entity_id": d["entity_id"],
                    "dispatch_id": d["entity_id"],
                    "description": f"Dispatch on hold: {d.get('trigger_description', 'unknown')}",
                    "layer": "governance",
                })
            elif d["escalation_level"] == "escalate":
                escalations.append(entry)
                blockers.append({
                    "finding_type": "escalation_escalate",
                    "severity": "error",
                    "entity_type": d["entity_type"],
                    "entity_id": d["entity_id"],
                    "dispatch_id": d["entity_id"],
                    "description": f"Dispatch escalated to T0: {d.get('trigger_description', 'unknown')}",
                    "layer": "governance",
                })

    if holds:
        advisories.append(Advisory(
            advisory_type=ADVISORY_RESOLVE_ESCALATION,
            severity="warning",
            entity_type="batch",
            entity_id="pre_merge",
            recommendation=f"{len(holds)} dispatch(es) on hold — resolve before merge",
            evidence={"holds": holds},
        ))

    # Check for recent overrides
    recent_overrides = conn.execute(
        """
        SELECT * FROM governance_overrides
        WHERE outcome = 'granted'
        ORDER BY occurred_at DESC LIMIT 20
        """,
    ).fetchall()

    override_dispatches = []
    for row in recent_overrides:
        d = dict(row)
        if d["entity_id"] in dispatch_ids:
            override_dispatches.append({
                "override_id": d["override_id"],
                "entity": f"{d['entity_type']}:{d['entity_id']}",
                "type": d["override_type"],
                "justification": d["justification"],
            })

    if override_dispatches:
        advisories.append(Advisory(
            advisory_type=ADVISORY_REVIEW_OVERRIDE,
            severity="info",
            entity_type="batch",
            entity_id="pre_merge",
            recommendation=(
                f"{len(override_dispatches)} governance override(s) granted for "
                f"dispatches in scope — review justifications before merge"
            ),
            evidence={"overrides": override_dispatches},
        ))

    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "advisories": [a.to_dict() for a in advisories],
        "governance": {
            "holds": holds,
            "escalations": escalations,
            "override_count": len(override_dispatches),
        },
    }


# ---------------------------------------------------------------------------
# Verification history queries
# ---------------------------------------------------------------------------

def get_verification_history(
    conn: sqlite3.Connection,
    dispatch_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get verification history for a dispatch, newest first."""
    rows = conn.execute(
        """
        SELECT * FROM provenance_verifications
        WHERE dispatch_id = ?
        ORDER BY verified_at DESC LIMIT ?
        """,
        (dispatch_id, limit),
    ).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        results.append({
            "verification_id": d["verification_id"],
            "verdict": d["verdict"],
            "chain_status": d["chain_status"],
            "findings": _parse_json(d.get("findings_json")),
            "advisories": _parse_json(d.get("advisory_json")),
            "verified_by": d.get("verified_by"),
            "verified_at": d["verified_at"],
        })
    return results


def get_failed_verifications(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Get recent verification failures for operator attention."""
    rows = conn.execute(
        """
        SELECT * FROM provenance_verifications
        WHERE verdict = 'fail'
        ORDER BY verified_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        results.append({
            "verification_id": d["verification_id"],
            "dispatch_id": d["dispatch_id"],
            "chain_status": d["chain_status"],
            "findings": _parse_json(d.get("findings_json")),
            "verified_at": d["verified_at"],
        })
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(value: Optional[str]) -> Any:
    """Parse a JSON string, returning empty list/dict on failure."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
