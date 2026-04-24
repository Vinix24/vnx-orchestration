#!/usr/bin/env python3
"""
VNX Provenance Audit Views — Audit surfaces and advisory guardrails.

Extracted from provenance_verification.py. Provides governance audit views,
provenance audit views, pre-merge advisory guardrails, and verification history
queries for operator/T0 review.

Governance:
  A-R9: No silent policy mutation from recommendation logic
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from provenance_verification import (
    ADVISORY_ENRICH_RECEIPT,
    ADVISORY_LINK_COMMIT,
    ADVISORY_REGISTER_PROVENANCE,
    ADVISORY_RESOLVE_ESCALATION,
    ADVISORY_REVIEW_OVERRIDE,
    CHAIN_STATUS_BROKEN,
    CHAIN_STATUS_COMPLETE,
    CHAIN_STATUS_INCOMPLETE,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARNING,
    Advisory,
    verify_dispatch_provenance,
)


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
