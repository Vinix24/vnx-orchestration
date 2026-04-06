#!/usr/bin/env python3
"""
VNX Receipt Provenance Enrichment — Bidirectional linkage between dispatches,
receipts, commits, and PRs.

Implements FP-D PR-2: strengthens the receipt layer so provenance can be
reconstructed from receipts without manual digging.

Provenance contract: docs/core/42_FPD_PROVENANCE_CONTRACT.md

Key responsibilities:
  - Enrich receipt payloads with provenance fields (dispatch_id, trace_token,
    pr_number, feature_plan_pr)
  - Map between dispatches, receipts, and commit identities
  - Validate receipt provenance links and detect gaps
  - Produce operator-readable provenance summaries
  - Register provenance links in the provenance_registry table
  - Preserve backward compatibility with existing cmd_id-based receipts
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import _append_event, _now_utc

# ── Reuse trace token regexes from PR-0 ──────────────────────────────────

PREFERRED_RE = re.compile(r"^Dispatch-ID:\s+(\S+)$", re.MULTILINE)
LEGACY_DISPATCH_RE = re.compile(r"dispatch:(\S+)")
LEGACY_PR_RE = re.compile(r"\bPR-(\d+)\b")
LEGACY_FP_RE = re.compile(r"\bFP-([A-Z])\b")
DISPATCH_ID_RE = re.compile(r"^\d{8}-\d{6}-.+-[A-Z]$")

# ── Provenance gap types (Section 5.1 of provenance contract) ────────────

GAP_MISSING_DISPATCH_ID = "missing_dispatch_id"
GAP_MISSING_GIT_REF = "missing_git_ref"
GAP_MISSING_TRACE_TOKEN = "missing_trace_token"
GAP_UNRESOLVABLE_TOKEN = "unresolvable_token"
GAP_MISSING_RECEIPT = "missing_receipt"
GAP_BROKEN_CHAIN = "broken_chain"
GAP_CMD_ID_FALLBACK = "cmd_id_fallback"

CHAIN_STATUS_COMPLETE = "complete"
CHAIN_STATUS_INCOMPLETE = "incomplete"
CHAIN_STATUS_BROKEN = "broken"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProvenanceGap:
    """A detected gap in the provenance chain."""
    gap_type: str
    severity: str  # info | warning | error
    entity_type: str  # receipt | dispatch | commit
    entity_id: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gap_type": self.gap_type,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "description": self.description,
        }


@dataclass
class ProvenanceValidation:
    """Result of validating a receipt's provenance links."""
    valid: bool
    dispatch_id: Optional[str]
    git_ref: Optional[str]
    trace_token: Optional[str]
    pr_number: Optional[int]
    feature_plan_pr: Optional[str]
    chain_status: str  # complete | incomplete | broken
    gaps: List[ProvenanceGap] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "dispatch_id": self.dispatch_id,
            "git_ref": self.git_ref,
            "trace_token": self.trace_token,
            "pr_number": self.pr_number,
            "feature_plan_pr": self.feature_plan_pr,
            "chain_status": self.chain_status,
            "gaps": [g.to_dict() for g in self.gaps],
        }

    @property
    def has_blocking_gaps(self) -> bool:
        return any(g.severity == "error" for g in self.gaps)


@dataclass
class ProvenanceLink:
    """A single entry in the provenance registry."""
    dispatch_id: str
    receipt_id: Optional[str] = None
    commit_sha: Optional[str] = None
    pr_number: Optional[int] = None
    feature_plan_pr: Optional[str] = None
    trace_token: Optional[str] = None
    chain_status: str = CHAIN_STATUS_INCOMPLETE
    gaps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "receipt_id": self.receipt_id,
            "commit_sha": self.commit_sha,
            "pr_number": self.pr_number,
            "feature_plan_pr": self.feature_plan_pr,
            "trace_token": self.trace_token,
            "chain_status": self.chain_status,
            "gaps": self.gaps,
        }


# ---------------------------------------------------------------------------
# Receipt provenance enrichment
# ---------------------------------------------------------------------------

def enrich_receipt_provenance(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a receipt with provenance fields per PR-2 contract (Section 6).

    Adds dispatch_id, trace_token, pr_number, and feature_plan_pr fields.
    Preserves backward compatibility: populates both dispatch_id and cmd_id
    during transition.

    Args:
        receipt: Receipt payload to enrich.

    Returns:
        Enriched receipt (modified in place and returned).
    """
    # Resolve dispatch_id from receipt or cmd_id fallback
    dispatch_id = _resolve_dispatch_id(receipt)
    if dispatch_id:
        receipt["dispatch_id"] = dispatch_id
        # Backward compat: keep cmd_id in sync during transition
        if "cmd_id" not in receipt:
            receipt["cmd_id"] = dispatch_id

    # Build trace token from dispatch_id
    if dispatch_id and "trace_token" not in receipt:
        receipt["trace_token"] = f"Dispatch-ID: {dispatch_id}"

    # Extract feature_plan_pr from dispatch context or receipt metadata
    if "feature_plan_pr" not in receipt:
        fp_pr = _resolve_feature_plan_pr(receipt)
        if fp_pr:
            receipt["feature_plan_pr"] = fp_pr

    # pr_number: leave as-is if already set, otherwise None
    if "pr_number" not in receipt:
        receipt["pr_number"] = None

    return receipt


def _resolve_dispatch_id(receipt: Dict[str, Any]) -> Optional[str]:
    """Resolve dispatch_id with priority: dispatch_id > cmd_id > env.

    Per Section 6.2: check dispatch_id first, then fall back to cmd_id.
    """
    # Priority 1: explicit dispatch_id
    did = str(receipt.get("dispatch_id") or "").strip()
    if did:
        return did

    # Priority 2: cmd_id fallback
    cmd_id = str(receipt.get("cmd_id") or "").strip()
    if cmd_id:
        return cmd_id

    # Priority 3: metadata.dispatch_id
    metadata = receipt.get("metadata")
    if isinstance(metadata, dict):
        meta_did = str(metadata.get("dispatch_id") or "").strip()
        if meta_did:
            return meta_did

    # Priority 4: environment variable
    env_did = os.environ.get("VNX_CURRENT_DISPATCH_ID", "").strip()
    if env_did:
        return env_did

    return None


def _resolve_feature_plan_pr(receipt: Dict[str, Any]) -> Optional[str]:
    """Resolve feature_plan_pr from receipt metadata or dispatch context."""
    # Check metadata first
    metadata = receipt.get("metadata")
    if isinstance(metadata, dict):
        fp = str(metadata.get("feature_plan_pr") or "").strip()
        if fp:
            return fp

    # Extract from dispatch_id pattern or summary/title if PR-N mentioned
    for text_field in ("summary", "title"):
        text = str(receipt.get(text_field) or "")
        pr_matches = LEGACY_PR_RE.findall(text)
        if pr_matches:
            return f"PR-{pr_matches[0]}"

    return None


# ---------------------------------------------------------------------------
# Provenance validation
# ---------------------------------------------------------------------------

def validate_receipt_provenance(receipt: Dict[str, Any]) -> ProvenanceValidation:
    """Validate a receipt's provenance links and detect gaps.

    Checks:
      - dispatch_id present (or cmd_id fallback)
      - git_ref present in provenance
      - trace_token consistency
      - Feature plan linkage

    Returns:
        ProvenanceValidation with gap details.
    """
    gaps: List[ProvenanceGap] = []
    dispatch_id = _resolve_dispatch_id(receipt)
    receipt_id = str(receipt.get("run_id") or receipt.get("task_id") or "")

    # Check dispatch_id
    if not dispatch_id:
        gaps.append(ProvenanceGap(
            gap_type=GAP_MISSING_DISPATCH_ID,
            severity="warning",
            entity_type="receipt",
            entity_id=receipt_id,
            description="Receipt has no dispatch_id or cmd_id",
        ))
    elif receipt.get("dispatch_id") is None and receipt.get("cmd_id"):
        # Using cmd_id fallback — not a gap per se, but worth noting
        gaps.append(ProvenanceGap(
            gap_type=GAP_CMD_ID_FALLBACK,
            severity="info",
            entity_type="receipt",
            entity_id=receipt_id,
            description=f"Receipt uses cmd_id fallback: {receipt.get('cmd_id')}",
        ))

    # Check git provenance
    provenance = receipt.get("provenance")
    git_ref = None
    if isinstance(provenance, dict):
        git_ref = str(provenance.get("git_ref") or "").strip()
        if not git_ref or git_ref in ("unknown", "not_a_repo"):
            git_ref = None
            gaps.append(ProvenanceGap(
                gap_type=GAP_MISSING_GIT_REF,
                severity="warning",
                entity_type="receipt",
                entity_id=receipt_id,
                description="Receipt provenance has no valid git_ref",
            ))
    else:
        gaps.append(ProvenanceGap(
            gap_type=GAP_MISSING_GIT_REF,
            severity="warning",
            entity_type="receipt",
            entity_id=receipt_id,
            description="Receipt has no provenance block",
        ))

    # Check trace token
    trace_token = str(receipt.get("trace_token") or "").strip() or None

    # Check feature plan PR
    feature_plan_pr = str(receipt.get("feature_plan_pr") or "").strip() or None
    pr_number = receipt.get("pr_number")
    if isinstance(pr_number, str):
        try:
            pr_number = int(pr_number)
        except ValueError:
            pr_number = None

    # Determine chain status
    has_dispatch = dispatch_id is not None
    has_git = git_ref is not None
    has_blocking = any(g.severity == "error" for g in gaps)

    if has_blocking:
        chain_status = CHAIN_STATUS_BROKEN
    elif has_dispatch and has_git:
        chain_status = CHAIN_STATUS_COMPLETE if (trace_token and feature_plan_pr) else CHAIN_STATUS_INCOMPLETE
    elif has_dispatch or has_git:
        chain_status = CHAIN_STATUS_INCOMPLETE
    else:
        chain_status = CHAIN_STATUS_BROKEN

    return ProvenanceValidation(
        valid=not has_blocking,
        dispatch_id=dispatch_id,
        git_ref=git_ref,
        trace_token=trace_token,
        pr_number=pr_number,
        feature_plan_pr=feature_plan_pr,
        chain_status=chain_status,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Provenance registry operations
# ---------------------------------------------------------------------------

def register_provenance_link(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    receipt_id: Optional[str] = None,
    commit_sha: Optional[str] = None,
    pr_number: Optional[int] = None,
    feature_plan_pr: Optional[str] = None,
    trace_token: Optional[str] = None,
    chain_status: str = CHAIN_STATUS_INCOMPLETE,
    gaps: Optional[List[Dict[str, Any]]] = None,
) -> ProvenanceLink:
    """Register or update a provenance link in the registry.

    Upserts: if dispatch_id exists, merges non-null fields and recalculates
    chain status. This allows links to be discovered incrementally.

    Returns the current ProvenanceLink state.
    """
    now = _now_utc()
    gaps_json = json.dumps(gaps or [])

    existing = conn.execute(
        "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()

    if existing:
        # Merge: only overwrite fields that are currently NULL
        merged = dict(existing)
        if receipt_id and not merged.get("receipt_id"):
            merged["receipt_id"] = receipt_id
        if commit_sha and not merged.get("commit_sha"):
            merged["commit_sha"] = commit_sha
        if pr_number is not None and merged.get("pr_number") is None:
            merged["pr_number"] = pr_number
        if feature_plan_pr and not merged.get("feature_plan_pr"):
            merged["feature_plan_pr"] = feature_plan_pr
        if trace_token and not merged.get("trace_token"):
            merged["trace_token"] = trace_token

        # Recalculate chain status from merged fields
        new_status = _calculate_chain_status(merged, gaps)
        new_gaps = gaps_json if gaps else merged.get("gaps_json", "[]")

        conn.execute(
            """
            UPDATE provenance_registry
            SET receipt_id = ?, commit_sha = ?, pr_number = ?,
                feature_plan_pr = ?, trace_token = ?,
                chain_status = ?, gaps_json = ?
            WHERE dispatch_id = ?
            """,
            (
                merged.get("receipt_id"),
                merged.get("commit_sha"),
                merged.get("pr_number"),
                merged.get("feature_plan_pr"),
                merged.get("trace_token"),
                new_status,
                new_gaps,
                dispatch_id,
            ),
        )

        return ProvenanceLink(
            dispatch_id=dispatch_id,
            receipt_id=merged.get("receipt_id"),
            commit_sha=merged.get("commit_sha"),
            pr_number=merged.get("pr_number"),
            feature_plan_pr=merged.get("feature_plan_pr"),
            trace_token=merged.get("trace_token"),
            chain_status=new_status,
            gaps=json.loads(new_gaps) if isinstance(new_gaps, str) else [],
        )

    # Calculate chain status from provided fields
    initial_fields = {
        "receipt_id": receipt_id,
        "commit_sha": commit_sha,
        "pr_number": pr_number,
        "feature_plan_pr": feature_plan_pr,
    }
    chain_status = _calculate_chain_status(initial_fields, gaps)

    # Insert new row
    conn.execute(
        """
        INSERT INTO provenance_registry
            (dispatch_id, receipt_id, commit_sha, pr_number,
             feature_plan_pr, trace_token, chain_status, gaps_json,
             registered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dispatch_id, receipt_id, commit_sha, pr_number,
            feature_plan_pr, trace_token, chain_status, gaps_json,
            now,
        ),
    )

    _append_event(
        conn,
        event_type="provenance_registered",
        entity_type="provenance",
        entity_id=dispatch_id,
        actor="receipt_provenance",
        reason="provenance link registered",
        metadata={
            "receipt_id": receipt_id,
            "commit_sha": commit_sha,
            "chain_status": chain_status,
        },
    )

    return ProvenanceLink(
        dispatch_id=dispatch_id,
        receipt_id=receipt_id,
        commit_sha=commit_sha,
        pr_number=pr_number,
        feature_plan_pr=feature_plan_pr,
        trace_token=trace_token,
        chain_status=chain_status,
        gaps=gaps or [],
    )


def get_provenance_link(
    conn: sqlite3.Connection,
    dispatch_id: str,
) -> Optional[ProvenanceLink]:
    """Retrieve a provenance link from the registry."""
    row = conn.execute(
        "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()

    if not row:
        return None

    gaps = []
    try:
        gaps = json.loads(row["gaps_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        pass

    return ProvenanceLink(
        dispatch_id=row["dispatch_id"],
        receipt_id=row["receipt_id"],
        commit_sha=row["commit_sha"],
        pr_number=row["pr_number"],
        feature_plan_pr=row["feature_plan_pr"],
        trace_token=row["trace_token"],
        chain_status=row["chain_status"],
        gaps=gaps,
    )


def _calculate_chain_status(
    merged: Dict[str, Any],
    gaps: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Calculate chain status from merged provenance fields."""
    has_receipt = bool(merged.get("receipt_id"))
    has_commit = bool(merged.get("commit_sha"))
    has_pr = merged.get("pr_number") is not None
    has_fp = bool(merged.get("feature_plan_pr"))

    # Check for broken chains (contradictions in gaps)
    if gaps:
        for gap in gaps:
            if isinstance(gap, dict) and gap.get("severity") == "error":
                return CHAIN_STATUS_BROKEN

    # All links present = complete
    if has_receipt and has_commit:
        return CHAIN_STATUS_COMPLETE

    return CHAIN_STATUS_INCOMPLETE


# ---------------------------------------------------------------------------
# Bidirectional mapping helpers
# ---------------------------------------------------------------------------

def find_receipts_by_dispatch(
    receipts_path: Path,
    dispatch_id: str,
) -> List[Dict[str, Any]]:
    """Find all receipts linked to a dispatch_id.

    Scans the NDJSON receipts file for matching dispatch_id or cmd_id.
    This is the Dispatch -> Receipt direction.
    """
    if not receipts_path.exists():
        return []

    matches: List[Dict[str, Any]] = []
    with receipts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry_did = str(entry.get("dispatch_id") or entry.get("cmd_id") or "")
            if entry_did == dispatch_id:
                matches.append(entry)
    return matches


def find_dispatch_by_receipt(receipt: Dict[str, Any]) -> Optional[str]:
    """Extract dispatch_id from a receipt.

    This is the Receipt -> Dispatch direction.
    Checks dispatch_id first, then cmd_id fallback.
    """
    return _resolve_dispatch_id(receipt)


def find_receipt_by_commit(
    receipts_path: Path,
    commit_sha: str,
) -> Optional[Dict[str, Any]]:
    """Find the receipt linked to a commit SHA.

    This is the Commit -> Receipt direction.
    Matches against receipt.provenance.git_ref.
    """
    if not receipts_path.exists():
        return None

    with receipts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            provenance = entry.get("provenance")
            if isinstance(provenance, dict):
                git_ref = str(provenance.get("git_ref") or "")
                if git_ref and git_ref == commit_sha:
                    return entry
    return None


def find_commits_by_dispatch(
    dispatch_id: str,
    repo_root: Optional[Path] = None,
) -> List[str]:
    """Find commit SHAs that carry a trace token for the given dispatch_id.

    This is the Dispatch -> Commit direction (via trace token in commit message).
    Searches git log for Dispatch-ID: lines matching the dispatch_id.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H %s%n%b", "--grep",
             f"Dispatch-ID: {dispatch_id}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    shas: List[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and re.match(r"^[0-9a-f]{40}\b", line):
            shas.append(line.split()[0])
    return shas


# ---------------------------------------------------------------------------
# Provenance gap event emission
# ---------------------------------------------------------------------------

def emit_provenance_gap_event(
    conn: sqlite3.Connection,
    gap: ProvenanceGap,
    actor: str = "receipt_provenance",
) -> str:
    """Emit a provenance_gap coordination event (Section 5.2).

    Returns the event_id.
    """
    return _append_event(
        conn,
        event_type="provenance_gap",
        entity_type=gap.entity_type,
        entity_id=gap.entity_id,
        actor=actor,
        reason=gap.description,
        metadata={
            "gap_type": gap.gap_type,
            "severity": gap.severity,
        },
    )


# ---------------------------------------------------------------------------
# Operator-readable provenance summaries
# ---------------------------------------------------------------------------

def provenance_summary_for_dispatch(
    dispatch_id: str,
    receipts_path: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Generate an operator-readable provenance summary for a dispatch.

    Combines data from receipts file and provenance registry to show
    the full provenance chain status.
    """
    receipts = find_receipts_by_dispatch(receipts_path, dispatch_id)

    summary: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "receipt_count": len(receipts),
        "receipts": [],
        "chain_status": CHAIN_STATUS_INCOMPLETE,
        "gaps": [],
        "registry": None,
    }

    for r in receipts:
        prov = r.get("provenance", {})
        summary["receipts"].append({
            "receipt_id": r.get("run_id") or r.get("task_id"),
            "event": r.get("event_type") or r.get("event"),
            "status": r.get("status"),
            "git_ref": prov.get("git_ref") if isinstance(prov, dict) else None,
            "trace_token": r.get("trace_token"),
            "timestamp": r.get("timestamp"),
        })

    # Check registry if connection provided
    if conn:
        link = get_provenance_link(conn, dispatch_id)
        if link:
            summary["registry"] = link.to_dict()
            summary["chain_status"] = link.chain_status
            summary["gaps"] = link.gaps

    # Validate each receipt's provenance
    all_gaps: List[Dict[str, Any]] = []
    for r in receipts:
        validation = validate_receipt_provenance(r)
        for gap in validation.gaps:
            all_gaps.append(gap.to_dict())

    if not summary["gaps"]:
        summary["gaps"] = all_gaps

    if not receipts:
        summary["chain_status"] = CHAIN_STATUS_INCOMPLETE
        summary["gaps"].append({
            "gap_type": GAP_MISSING_RECEIPT,
            "severity": "warning",
            "entity_type": "dispatch",
            "entity_id": dispatch_id,
            "description": f"No receipts found for dispatch {dispatch_id}",
        })

    return summary


def batch_provenance_summary(
    dispatch_ids: List[str],
    receipts_path: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Generate a batch provenance summary for multiple dispatches.

    Returns aggregate statistics and per-dispatch details.
    """
    summaries = []
    counts = {"complete": 0, "incomplete": 0, "broken": 0}

    for did in dispatch_ids:
        s = provenance_summary_for_dispatch(did, receipts_path, conn)
        summaries.append(s)
        status = s.get("chain_status", CHAIN_STATUS_INCOMPLETE)
        if status in counts:
            counts[status] += 1

    return {
        "total_dispatches": len(dispatch_ids),
        "chain_status_counts": counts,
        "dispatches": summaries,
    }
