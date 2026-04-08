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
# Re-exports from provenance_audit_views (backward compatibility)
# ---------------------------------------------------------------------------

from provenance_audit_views import (  # noqa: E402, F401
    _parse_json,
    governance_audit_view,
    pre_merge_advisory,
    provenance_audit_view,
    get_verification_history,
    get_failed_verifications,
)
