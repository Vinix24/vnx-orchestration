#!/usr/bin/env python3
"""Chain state projection layer for multi-feature chain execution.

Implements the chain state model from docs/MULTI_FEATURE_CHAIN_CONTRACT.md (PR-0).

This module provides the single queryable surface for:
  - Current chain state (INITIALIZED, FEATURE_ACTIVE, FEATURE_ADVANCING, etc.)
  - Active feature and its PR progress
  - Next feature in sequence
  - Advancement truth: whether the chain is safe to advance (requires merged PR state
    AND gate certification — not implicit operator memory)
  - Carry-forward findings and unresolved chain items

State files (all located under $VNX_STATE_DIR, typically $VNX_DATA_DIR/vnx-state):
  chain_state.json            current chain state record
  chain_carry_forward.json    cumulative carry-forward ledger
  chain_audit.jsonl           append-only state transition audit trail
  pr_queue_state.json         PR completion status (existing VNX file)
  open_items.json             open items (existing VNX file)
  review_gates/results/       per-PR gate certification records (existing VNX dir)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical chain state constants (from contract Section 2.2)
# ---------------------------------------------------------------------------

CHAIN_STATES = frozenset({
    "INITIALIZED",
    "FEATURE_ACTIVE",
    "FEATURE_FAILED",
    "RECOVERY_PENDING",
    "FEATURE_ADVANCING",
    "ADVANCEMENT_BLOCKED",
    "CHAIN_HALTED",
    "CHAIN_COMPLETE",
    "NOT_INITIALIZED",  # sentinel: chain_state.json absent
})

# States where the chain cannot advance without intervention
BLOCKED_STATES = frozenset({"ADVANCEMENT_BLOCKED", "CHAIN_HALTED", "FEATURE_FAILED", "RECOVERY_PENDING"})

# States where operator action is required
RECOVERY_NEEDED_STATES = frozenset({"RECOVERY_PENDING", "CHAIN_HALTED"})

# Maximum requeue attempts before escalation (contract rule R-2/R-3)
MAX_REQUEUE_ATTEMPTS = 3

# gemini_review was removed from the mandatory set by the 05-09 operator
# decision (gemini-cli stays available but is never required) but stays a
# LEGAL signer whenever it actually runs and produces evidence -- dropping
# it from consideration entirely would silently discard a real PASS a
# gemini run had already earned. Appended unconditionally by
# required_signer_gates(); never part of the takeover-chain config itself.
_OPTIONAL_LEGACY_SIGNER = "gemini_review"


def required_signer_gates() -> Tuple[str, ...]:
    """Gate names eligible to certify chain-advancement (F2-4, 06-09).

    Replaces the old ``REQUIRED_GATES = ("gemini_review", "codex_gate")``
    literal -- a hardcoded ALL-of-two requirement that went dark the moment
    both named providers were unavailable on the same evening (gemini-cli
    not installed, codex on a weekly quota cap) even though a THIRD gate
    (glm_gate) had produced a full, evidenced PASS on that very PR (#1777,
    measured 2026-09-05/06). One provider outage should never be able to
    stall every merge on a fleet with four configured review-gate
    providers.

    Sourced from the SAME operator-configured review-gate takeover chain
    ``gate_request_handler`` already resolves
    (``VNX_REVIEW_GATE_TAKEOVER_CHAIN``, registered in
    ``config_registry.py``) -- never a second, independently maintained
    gate list that could silently drift from it. Resolved FRESH on every
    call, the same discipline
    ``gate_request_handler._build_review_gate_takeover_chain`` already
    applies, so an operator's config edit takes effect on the very next
    check instead of needing a process restart.

    ``gemini_review`` is ALWAYS appended (see ``_OPTIONAL_LEGACY_SIGNER``)
    even though it is absent from the takeover chain's default string and
    from the mandatory set: the 05-09 operator decision keeps it a legal,
    optional signer.

    Malformed operator config (an unknown gate name, or a name repeated --
    a cycle) raises ``gate_request_handler.ReviewGateTakeoverConfigError``,
    the SAME fail-loud behaviour the takeover chain itself has; never a
    silent fallback to a stale default.

    An explicit ``VNX_REVIEW_GATE_TAKEOVER_CHAIN=""`` (operator disabled
    automated takeover entirely) leaves exactly one eligible signer:
    ``gemini_review``. Known, narrow edge -- left as an Open Item rather
    than papered over with a second "gates eligible to sign" knob separate
    from "gates eligible for takeover", which is exactly the second
    configuration layer this deliverable was told not to invent.

    When ``config_runtime``/``gate_request_handler`` cannot be imported at
    all (e.g. a bare script invocation whose sys.path lacks scripts/lib's
    sibling modules), degrades to the same single-signer fallback -- logged
    as a WARNING, never silent, mirroring ``config_runtime``'s own
    fail-soft-but-loud philosophy. It deliberately does NOT fall back to a
    hardcoded copy of the chain's default string: duplicating that literal
    here would recreate the exact two-lists-that-can-drift defect this
    function exists to remove.
    """
    try:
        import config_runtime
        from gate_request_handler import (
            _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN,
            _parse_review_gate_takeover_chain,
        )
    except Exception as exc:  # vnx-silent-except: import-time unavailability of the wider config stack must degrade this gate list, never crash a caller merely checking advancement truth -- logged loudly so the degradation is never silent
        logger.warning(
            "chain_state_projection: kon config_runtime/gate_request_handler niet "
            "importeren (%s) -- geen enkele geconfigureerde ondertekenaar "
            "beschikbaar; alleen %s blijft geldig",
            exc, _OPTIONAL_LEGACY_SIGNER,
        )
        names: List[str] = []
    else:
        raw = config_runtime.get("VNX_REVIEW_GATE_TAKEOVER_CHAIN")
        if raw is None:
            raw = _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
        _parse_review_gate_takeover_chain(raw)  # fail-loud: unknown name / cycle
        names = [item.strip() for item in raw.split(",") if item.strip()]

    if _OPTIONAL_LEGACY_SIGNER not in names:
        names.append(_OPTIONAL_LEGACY_SIGNER)
    return tuple(names)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load a JSON file safely; return None on any error."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _load_pr_queue_state(state_dir: Path) -> Dict[str, Any]:
    """Load pr_queue_state.json; return empty dict if absent."""
    path = state_dir / "pr_queue_state.json"
    return _safe_load_json(path) or {}


def _load_open_items(state_dir: Path) -> List[Dict[str, Any]]:
    """Return list of open item records from open_items.json."""
    path = state_dir / "open_items.json"
    data = _safe_load_json(path) or {}
    items = data.get("items")
    return items if isinstance(items, list) else []


def _load_chain_state(state_dir: Path) -> Optional[Dict[str, Any]]:
    """Load chain_state.json; return None if absent."""
    return _safe_load_json(state_dir / "chain_state.json")


def _load_carry_forward(state_dir: Path) -> Dict[str, Any]:
    """Load chain_carry_forward.json; return empty structure if absent."""
    path = state_dir / "chain_carry_forward.json"
    data = _safe_load_json(path) or {}
    return {
        "chain_id": data.get("chain_id", ""),
        "findings": data.get("findings") if isinstance(data.get("findings"), list) else [],
        "open_items": data.get("open_items") if isinstance(data.get("open_items"), list) else [],
        "deferred_items": data.get("deferred_items") if isinstance(data.get("deferred_items"), list) else [],
        "residual_risks": data.get("residual_risks") if isinstance(data.get("residual_risks"), list) else [],
        "feature_summaries": data.get("feature_summaries") if isinstance(data.get("feature_summaries"), list) else [],
    }


def _load_gate_results(state_dir: Path, pr_id: str, gates: Iterable[str]) -> Dict[str, Any]:
    """Return gate certification results keyed by gate name for a given PR."""
    results_dir = state_dir / "review_gates" / "results"
    if not results_dir.is_dir():
        return {}
    try:
        pr_num = str(int(pr_id.split("-")[-1]))
    except (ValueError, IndexError):
        pr_num = pr_id.lower().replace("pr-", "").replace("pr", "")
    gate_results: Dict[str, Any] = {}
    for gate in gates:
        data = _safe_load_json(results_dir / f"pr-{pr_num}-{gate}.json")
        if data is not None:
            gate_results[gate] = {
                "status": data.get("status", "unknown"),
                "contract_hash": data.get("contract_hash", ""),
                "report_path": data.get("report_path", ""),
                "blocking_count": int(data.get("blocking_count") or 0),
                "blocking_findings": data.get("blocking_findings") or [],
                "commit_sha": data.get("commit_sha", ""),
                "recorded_at": data.get("recorded_at", ""),
            }
    return gate_results


_DECIDED_ABSENT_STATES = frozenset({"unavailable", "not_executable"})
_PASS_STATUSES = frozenset({"approve", "pass", "passed"})


def _classify_gate_signer(
    result: Dict[str, Any], pr_head_sha: Optional[str]
) -> Tuple[str, str]:
    """Classify one gate result record for chain-advancement signing.

    Three outcomes (OI-1624's open design question, decided HERE for this
    surface):

      - ``"absent"``        no usable evidence either way. Covers
                             ``status in {unavailable, not_executable}`` --
                             a provider outage or a gate whose runner does
                             not exist yet (deepseek_gate pre-E2). Neither a
                             vote FOR nor AGAINST advancement: an
                             unavailable provider must never silently pass,
                             but it must also never be the one thing
                             standing between a genuinely evidenced PASS
                             elsewhere and a GO.
      - ``"certified"``     every invariant holds: a pass-like status, zero
                             blocking findings/count, a populated
                             ``contract_hash``, a ``report_path`` that
                             exists ON DISK, and (when ``pr_head_sha`` is
                             known) a ``commit_sha`` that matches it.
      - ``"not_certified"`` a DECIDED record (the gate actually ran and
                             produced a terminal outcome) that fails at
                             least one invariant -- a real fail/reject
                             verdict, blocking findings, incomplete
                             evidence, or evidence for the wrong commit.
                             This is a genuine blocker: it stands even when
                             another gate in the same set certifies clean
                             -- a dissenting vote never loses to a passing
                             one (F2-4 requirement 3).

    Returns ``(label, detail)`` -- ``detail`` is "" for ``absent``/
    ``certified``, and a human-readable reason for ``not_certified``.
    """
    status = str(result.get("status", "")).lower()
    if status in _DECIDED_ABSENT_STATES:
        return "absent", ""

    blocking_findings = result.get("blocking_findings") or []
    blocking_len = len(blocking_findings) if isinstance(blocking_findings, list) else 0
    blocking_count = result.get("blocking_count")
    blocking_count = blocking_count if isinstance(blocking_count, int) else 0
    is_blocking = blocking_len > 0 or blocking_count > 0

    contract_hash = str(result.get("contract_hash", "")).strip()
    report_path = str(result.get("report_path", "")).strip()
    report_exists = bool(report_path) and Path(report_path).is_file()
    commit_sha = str(result.get("commit_sha", "")).strip()
    sha_matches = pr_head_sha is None or (bool(commit_sha) and commit_sha == pr_head_sha)

    if (
        status in _PASS_STATUSES
        and not is_blocking
        and contract_hash
        and report_exists
        and sha_matches
    ):
        return "certified", ""

    reasons: List[str] = []
    if status not in _PASS_STATUSES:
        reasons.append(f"status={status or 'unknown'}")
    if is_blocking:
        reasons.append(f"blocking={blocking_count or blocking_len}")
    if not contract_hash:
        reasons.append("contract_hash ontbreekt")
    if not report_path:
        reasons.append("report_path ontbreekt")
    elif not report_exists:
        reasons.append(f"report_path bestaat niet op schijf: {report_path}")
    if pr_head_sha is not None and not sha_matches:
        reasons.append(f"commit_sha={commit_sha or 'leeg'} komt niet overeen met head {pr_head_sha}")
    return "not_certified", "; ".join(reasons) if reasons else "onbekende reden"


def _pr_sequence_from_queue(pr_queue: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the ordered PR list from pr_queue_state.json."""
    prs = pr_queue.get("prs")
    return prs if isinstance(prs, list) else []


def _completed_pr_ids(pr_queue: Dict[str, Any]) -> List[str]:
    prs = _pr_sequence_from_queue(pr_queue)
    return [pr["id"] for pr in prs if pr.get("status") == "completed"]


def _find_current_feature(pr_queue: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first non-completed PR whose dependencies are all satisfied."""
    prs = _pr_sequence_from_queue(pr_queue)
    completed_ids = set(_completed_pr_ids(pr_queue))
    for pr in prs:
        if pr.get("status") == "completed":
            continue
        if all(dep in completed_ids for dep in (pr.get("dependencies") or [])):
            return pr
    return None


def _find_next_feature(pr_queue: Dict[str, Any], current_pr_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the PR that comes after current_pr_id in dependency order."""
    if current_pr_id is None:
        return None
    prs = _pr_sequence_from_queue(pr_queue)
    completed_ids = set(_completed_pr_ids(pr_queue)) | {current_pr_id}
    for pr in prs:
        if pr.get("id") == current_pr_id or pr.get("status") == "completed":
            continue
        if all(dep in completed_ids for dep in (pr.get("dependencies") or [])):
            return pr
    return None


# ---------------------------------------------------------------------------
# Advancement truth helpers
# ---------------------------------------------------------------------------

def _compute_gate_certification(
    state_dir: Path,
    feature_id: str,
    blockers: List[str],
    pr_head_sha: Optional[str] = None,
) -> Dict[str, str]:
    """Populate blockers and return the per-gate certification_status dict.

    F2-4 (06-09): advancement requires AT LEAST ONE certified signer from
    ``required_signer_gates()`` -- not all-of-N. A decided-but-invalid
    record (``not_certified``) still blocks on its own even when another
    gate in the same set certifies clean; an absent/unavailable record
    blocks nothing by itself. Zero certified signers is recorded as its OWN
    explicit blocker, so ``len(blockers) == 0`` never accidentally reads as
    advanceable just because no SINGLE gate individually failed (e.g. every
    configured gate is merely absent).
    """
    candidate_gates = required_signer_gates()
    gate_results = _load_gate_results(state_dir, feature_id, candidate_gates)
    certification_status: Dict[str, str] = {}
    certified_count = 0
    for gate in candidate_gates:
        result = gate_results.get(gate)
        if result is None:
            certification_status[gate] = "absent"
            continue
        label, detail = _classify_gate_signer(result, pr_head_sha)
        if label == "certified":
            certification_status[gate] = "certified"
            certified_count += 1
        elif label == "absent":
            certification_status[gate] = "absent"
        else:
            certification_status[gate] = f"not_certified:{detail}"
            blockers.append(f"{gate} not certified for {feature_id}: {detail}")
    if certified_count == 0:
        blockers.append(
            f"no valid signer for {feature_id} among configured gates "
            f"({', '.join(candidate_gates)}): at least one certified review-gate "
            "result is required"
        )
    return certification_status


def compute_advancement_truth(
    pr_queue: Dict[str, Any],
    open_items: List[Dict[str, Any]],
    state_dir: Path,
    current_feature_id: Optional[str],
    pr_head_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Derive whether the chain can advance to the next feature.

    Advancement requires (contract Section 3.2, gate rule updated by F2-4):
    1. Current feature PR has status 'completed' in pr_queue_state.json
    2. No open items with severity 'blocker' and status 'open'
    3. At least one gate from ``required_signer_gates()`` has terminal
       success with complete, on-disk evidence (see
       ``_classify_gate_signer``) -- not all configured gates at once.
    """
    if current_feature_id is None:
        return {"can_advance": False, "blockers": ["no active feature identified"], "certification_status": {}}

    blockers: List[str] = []

    # Check 1: PR completion
    prs = _pr_sequence_from_queue(pr_queue)
    pr_record = next((p for p in prs if p.get("id") == current_feature_id), None)
    if pr_record is None:
        blockers.append(f"{current_feature_id} not found in PR queue")
    elif pr_record.get("status") != "completed":
        blockers.append(f"{current_feature_id} not yet merged (status={pr_record.get('status', 'unknown')})")

    # Check 2: No blocker open items
    blocker_open = [
        item for item in open_items
        if str(item.get("severity", "")).lower() == "blocker"
        and str(item.get("status", "")).lower() == "open"
    ]
    if blocker_open:
        ids = ", ".join(item.get("id", "?") for item in blocker_open)
        blockers.append(f"{len(blocker_open)} open blocker item(s) unresolved: {ids}")

    # Check 2b: No unresolved blocker findings in carry-forward ledger (F-2)
    cf = _load_carry_forward(state_dir)
    blocker_findings = [
        f for f in cf.get("findings") or []
        if str(f.get("severity", "")).lower() == "blocker"
        and str(f.get("resolution_status", "")).lower() != "resolved"
    ]
    if blocker_findings:
        blockers.append(f"{len(blocker_findings)} unresolved blocker finding(s) in carry-forward")

    # Check 3: Gate certification
    certification_status = _compute_gate_certification(
        state_dir, current_feature_id, blockers, pr_head_sha
    )

    return {"can_advance": len(blockers) == 0, "blockers": blockers, "certification_status": certification_status}


# ---------------------------------------------------------------------------
# Carry-forward summary
# ---------------------------------------------------------------------------

def _summarize_findings(findings: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    """Return (total, open_count, blocker_count) for a findings list."""
    open_findings = [f for f in findings if str(f.get("resolution_status", "")).lower() != "resolved"]
    blocker_findings = [f for f in open_findings if str(f.get("severity", "")).lower() == "blocker"]
    return len(findings), len(open_findings), len(blocker_findings)


def build_carry_forward_summary(
    carry_forward: Dict[str, Any],
    open_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Summarize the carry-forward ledger and live open items for the projection surface."""
    total_f, open_f, blocker_f = _summarize_findings(carry_forward.get("findings") or [])

    cf_open_items = carry_forward.get("open_items") or []
    cf_blockers = [
        i for i in cf_open_items
        if str(i.get("severity", "")).lower() == "blocker" and str(i.get("status", "")).lower() == "open"
    ]
    live_unresolved = [
        item for item in open_items
        if str(item.get("status", "")).lower() not in {"done", "closed", "resolved", "wontfix"}
    ]
    live_blockers = [i for i in live_unresolved if str(i.get("severity", "")).lower() == "blocker"]

    return {
        "total_findings": total_f,
        "open_findings": open_f,
        "blocker_findings": blocker_f,
        "carry_forward_open_items": len(cf_open_items),
        "carry_forward_blocker_items": len(cf_blockers),
        "live_unresolved_items": len(live_unresolved),
        "live_blocker_items": len(live_blockers),
        "residual_risks": len(carry_forward.get("residual_risks") or []),
        "feature_summaries_count": len(carry_forward.get("feature_summaries") or []),
    }


def _build_unresolved_chain_items(
    carry_forward: Dict[str, Any],
    open_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return all carry-forward and live open items that are not yet resolved."""
    resolved = {"done", "closed", "resolved", "wontfix"}
    unresolved: List[Dict[str, Any]] = []
    for item in carry_forward.get("open_items") or []:
        if str(item.get("status", "")).lower() not in resolved:
            unresolved.append({
                "source": "carry_forward",
                "id": item.get("id"),
                "severity": item.get("severity"),
                "status": item.get("status"),
                "title": item.get("title"),
                "origin_feature": item.get("origin_feature"),
            })
    for item in open_items:
        if str(item.get("status", "")).lower() not in resolved:
            unresolved.append({
                "source": "open_items.json",
                "id": item.get("id"),
                "severity": item.get("severity"),
                "status": item.get("status"),
                "title": item.get("title"),
                "origin_feature": item.get("pr_id"),
            })
    return unresolved


# ---------------------------------------------------------------------------
# Requeue history helpers
# ---------------------------------------------------------------------------

def _load_requeue_history(chain_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if chain_state is None:
        return {}
    history = chain_state.get("requeue_history")
    return history if isinstance(history, dict) else {}


def _requeue_count_for(chain_state: Optional[Dict[str, Any]], feature_id: str) -> int:
    entry = _load_requeue_history(chain_state).get(feature_id) or {}
    return int(entry.get("total_attempts", 0))


# ---------------------------------------------------------------------------
# Projection sub-builders
# ---------------------------------------------------------------------------

def _resolve_chain_identity(
    chain_state: Optional[Dict[str, Any]], pr_queue: Dict[str, Any]
) -> Tuple[str, str, Optional[str], List[str]]:
    """Return (current_state, chain_id, current_feature_id, feature_sequence)."""
    if chain_state is not None:
        return (
            str(chain_state.get("current_state", "INITIALIZED")),
            str(chain_state.get("chain_id", "")),
            chain_state.get("current_feature_id"),
            chain_state.get("feature_sequence") or [],
        )
    current_feature = _find_current_feature(pr_queue)
    return (
        "NOT_INITIALIZED",
        "",
        current_feature.get("id") if current_feature else None,
        [pr.get("id") for pr in _pr_sequence_from_queue(pr_queue)],
    )


def _infer_not_initialized_state(
    prs: List[Dict[str, Any]], cf_summary: Dict[str, Any], current_feature_id: Optional[str]
) -> str:
    """Derive chain state from queue when chain_state.json is absent."""
    if prs and all(pr.get("status") == "completed" for pr in prs):
        return "CHAIN_COMPLETE"
    if cf_summary["live_blocker_items"] > 0:
        return "ADVANCEMENT_BLOCKED"
    if current_feature_id is not None:
        return "FEATURE_ACTIVE"
    return "NOT_INITIALIZED"


def _build_active_feature_info(
    current_pr_record: Optional[Dict[str, Any]],
    prs: List[Dict[str, Any]],
    chain_state: Optional[Dict[str, Any]],
    current_feature_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Build active feature dict from PR record, or None if no active PR."""
    if current_pr_record is None:
        return None
    completed_ids = {p["id"] for p in prs if p.get("status") == "completed"}
    return {
        "id": current_pr_record.get("id"),
        "title": current_pr_record.get("title"),
        "status": current_pr_record.get("status"),
        "track": current_pr_record.get("track"),
        "gate": current_pr_record.get("gate"),
        "requeue_count": _requeue_count_for(chain_state, current_feature_id or ""),
        "prs_completed_before": [
            p.get("id") for p in prs
            if p.get("id") in completed_ids and p.get("id") != current_feature_id
        ],
    }


def _build_next_feature_info(next_pr_record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build next feature dict from PR record, or None."""
    if next_pr_record is None:
        return None
    return {
        "id": next_pr_record.get("id"),
        "title": next_pr_record.get("title"),
        "dependencies": next_pr_record.get("dependencies") or [],
    }


# ---------------------------------------------------------------------------
# Main projection builder
# ---------------------------------------------------------------------------

def build_chain_projection(state_dir: str | Path) -> Dict[str, Any]:
    """Build the full chain state projection.

    Single stable surface for querying chain progression truth.
    Works without chain_state.json by deriving state from pr_queue_state.json.
    """
    state_root = Path(state_dir)
    chain_state = _load_chain_state(state_root)
    pr_queue = _load_pr_queue_state(state_root)
    open_items = _load_open_items(state_root)
    carry_forward = _load_carry_forward(state_root)

    current_state, chain_id, current_feature_id, feature_sequence = _resolve_chain_identity(
        chain_state, pr_queue
    )

    prs = _pr_sequence_from_queue(pr_queue)
    current_pr_record = next((p for p in prs if p.get("id") == current_feature_id), None) if current_feature_id else None
    next_pr_record = _find_next_feature(pr_queue, current_feature_id)

    advancement = compute_advancement_truth(
        pr_queue=pr_queue, open_items=open_items, state_dir=state_root, current_feature_id=current_feature_id,
        pr_head_sha=(current_pr_record or {}).get("head_sha"),
    )
    cf_summary = build_carry_forward_summary(carry_forward, open_items)
    unresolved = _build_unresolved_chain_items(carry_forward, open_items)

    if current_state == "NOT_INITIALIZED":
        current_state = _infer_not_initialized_state(prs, cf_summary, current_feature_id)

    return {
        "chain_id": chain_id,
        "chain_state": current_state,
        "is_blocked": current_state in BLOCKED_STATES,
        "is_recovery_needed": current_state in RECOVERY_NEEDED_STATES,
        "active_feature": _build_active_feature_info(current_pr_record, prs, chain_state, current_feature_id),
        "next_feature": _build_next_feature_info(next_pr_record),
        "feature_sequence": feature_sequence,
        "completed_features": _completed_pr_ids(pr_queue),
        "advancement_truth": advancement,
        "carry_forward_summary": cf_summary,
        "unresolved_chain_items": unresolved,
        "requeue_history": _load_requeue_history(chain_state),
        "generated_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Chain state write helpers (for T0 and operators)
# ---------------------------------------------------------------------------

def init_chain_state(
    state_dir: str | Path,
    *,
    chain_id: str,
    feature_plan: str,
    feature_sequence: List[str],
    chain_origin_sha: str,
    initiated_by: str = "T0",
) -> Dict[str, Any]:
    """Create or overwrite chain_state.json with INITIALIZED state."""
    state_root = Path(state_dir)
    now = _now_iso()
    record: Dict[str, Any] = {
        "chain_id": chain_id,
        "feature_plan": feature_plan,
        "feature_sequence": feature_sequence,
        "chain_origin_sha": chain_origin_sha,
        "initiated_by": initiated_by,
        "initiated_at": now,
        "current_state": "INITIALIZED",
        "current_feature_id": None,
        "requeue_history": {},
        "completed_features": [],
        "updated_at": now,
    }
    (state_root / "chain_state.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    _append_audit(state_root, chain_id=chain_id, from_state=None, to_state="INITIALIZED",
                  feature_id=None, actor=initiated_by, reason="chain initialized")
    return record


def _update_requeue_history(
    chain_state: Dict[str, Any], to_state: str, from_state: str, feature_id: Optional[str]
) -> None:
    """Increment requeue counter when transitioning back to FEATURE_ACTIVE from recovery."""
    if to_state == "FEATURE_ACTIVE" and feature_id and from_state in {"RECOVERY_PENDING", "FEATURE_FAILED"}:
        history = chain_state.setdefault("requeue_history", {})
        entry = history.setdefault(feature_id, {"total_attempts": 0, "failure_classes": {}})
        entry["total_attempts"] = int(entry.get("total_attempts", 0)) + 1


def _update_completed_features(
    chain_state: Dict[str, Any], to_state: str, feature_id: Optional[str]
) -> None:
    """Append feature_id to completed_features when advancing or completing chain."""
    if to_state in {"FEATURE_ADVANCING", "CHAIN_COMPLETE"} and feature_id:
        completed = chain_state.setdefault("completed_features", [])
        if feature_id not in completed:
            completed.append(feature_id)


def record_state_transition(
    state_dir: str | Path,
    *,
    to_state: str,
    feature_id: Optional[str] = None,
    actor: str = "T0",
    reason: str = "",
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Transition the chain to a new state, recording audit trail.

    Updates chain_state.json and appends to chain_audit.jsonl.
    Returns the updated chain state record.
    """
    if to_state not in CHAIN_STATES:
        raise ValueError(f"Invalid chain state: {to_state!r}. Valid: {sorted(CHAIN_STATES)}")

    state_root = Path(state_dir)
    chain_state = _load_chain_state(state_root) or {}
    from_state = chain_state.get("current_state", "NOT_INITIALIZED")

    chain_state["current_state"] = to_state
    chain_state["updated_at"] = _now_iso()
    if feature_id is not None:
        chain_state["current_feature_id"] = feature_id

    _update_requeue_history(chain_state, to_state, from_state, feature_id)
    _update_completed_features(chain_state, to_state, feature_id)

    (state_root / "chain_state.json").write_text(json.dumps(chain_state, indent=2), encoding="utf-8")
    _append_audit(state_root, chain_id=chain_state.get("chain_id", ""), from_state=from_state,
                  to_state=to_state, feature_id=feature_id, actor=actor, reason=reason, evidence=evidence)
    return chain_state


def _append_audit(
    state_root: Path,
    *,
    chain_id: str,
    from_state: Optional[str],
    to_state: str,
    feature_id: Optional[str],
    actor: str,
    reason: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one record to chain_audit.jsonl."""
    record = {
        "chain_id": chain_id,
        "timestamp": _now_iso(),
        "from_state": from_state,
        "to_state": to_state,
        "feature_id": feature_id,
        "actor": actor,
        "reason": reason,
        "evidence": evidence or {},
    }
    audit_path = state_root / "chain_audit.jsonl"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chain state projection for VNX multi-feature chains")
    parser.add_argument("--state-dir", default=os.environ.get("VNX_STATE_DIR", ""),
                        help="Path to state directory (defaults to VNX_STATE_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("projection", help="Full chain state projection")
    sub.add_parser("advancement-truth", help="Advancement truth only")
    sub.add_parser("carry-forward", help="Carry-forward summary and unresolved items")
    sub.add_parser("chain-state", help="Current chain state name and blocked/recovery flags")

    init_p = sub.add_parser("init", help="Initialize chain state")
    init_p.add_argument("--chain-id", required=True)
    init_p.add_argument("--feature-plan", default="FEATURE_PLAN.md")
    init_p.add_argument("--feature-sequence", nargs="+", required=True)
    init_p.add_argument("--origin-sha", default="")
    init_p.add_argument("--initiated-by", default="T0")

    trans_p = sub.add_parser("transition", help="Record a state transition")
    trans_p.add_argument("--to-state", required=True)
    trans_p.add_argument("--feature-id")
    trans_p.add_argument("--actor", default="T0")
    trans_p.add_argument("--reason", default="")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    state_dir = (args.state_dir or "").strip() or os.environ.get("VNX_STATE_DIR", "").strip()
    if not state_dir:
        print("{}")
        return 0

    state_root = Path(state_dir)

    if args.command == "init":
        record = init_chain_state(state_root, chain_id=args.chain_id, feature_plan=args.feature_plan,
                                   feature_sequence=args.feature_sequence, chain_origin_sha=args.origin_sha,
                                   initiated_by=args.initiated_by)
        print(json.dumps(record, separators=(",", ":")))
    elif args.command == "transition":
        record = record_state_transition(state_root, to_state=args.to_state, feature_id=args.feature_id,
                                          actor=args.actor, reason=args.reason)
        print(json.dumps(record, separators=(",", ":")))
    else:
        projection = build_chain_projection(state_root)
        if args.command == "projection":
            print(json.dumps(projection, separators=(",", ":")))
        elif args.command == "advancement-truth":
            print(json.dumps(projection["advancement_truth"], separators=(",", ":")))
        elif args.command == "carry-forward":
            print(json.dumps({"summary": projection["carry_forward_summary"],
                              "unresolved_chain_items": projection["unresolved_chain_items"]},
                             separators=(",", ":")))
        elif args.command == "chain-state":
            print(json.dumps({"chain_state": projection["chain_state"],
                              "is_blocked": projection["is_blocked"],
                              "is_recovery_needed": projection["is_recovery_needed"],
                              "active_feature": (projection.get("active_feature") or {}).get("id"),
                              "next_feature": (projection.get("next_feature") or {}).get("id")},
                             separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
