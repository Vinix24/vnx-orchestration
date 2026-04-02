#!/usr/bin/env python3
"""Chain recovery, requeue enforcement, and branch/worktree transition guard.

Implements PR-2 of Feature 14: chain-safe resume, requeue, and feature-transition
enforcement per docs/MULTI_FEATURE_CHAIN_CONTRACT.md Sections 4, 5, and 6.

Provides:
  - Failure classification (recoverable_transient / recoverable_fixable / non_recoverable)
  - Recovery decision: requeue vs block vs escalate with retry-limit enforcement
  - Branch baseline guard: reject dispatches on branches not derived from merged main
  - Carry-forward snapshot: persist feature boundary state into the chain ledger
  - Next feature context: carry-forward summary injected into next dispatch

State files (all under $VNX_STATE_DIR):
  chain_state.json            read/written via chain_state_projection
  chain_carry_forward.json    carry-forward ledger (append on feature boundary)
  chain_audit.jsonl           audit trail (written via chain_state_projection)
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Failure classification (contract Section 4.1)
# ---------------------------------------------------------------------------

FAILURE_CLASS_TRANSIENT = "recoverable_transient"
FAILURE_CLASS_FIXABLE = "recoverable_fixable"
FAILURE_CLASS_NON_RECOVERABLE = "non_recoverable"

FAILURE_CLASSES = frozenset({
    FAILURE_CLASS_TRANSIENT,
    FAILURE_CLASS_FIXABLE,
    FAILURE_CLASS_NON_RECOVERABLE,
})

# Keywords that indicate a transient (infrastructure) failure
_TRANSIENT_SIGNALS = frozenset({
    "timeout", "rate_limit", "rate limit", "network", "outage", "flake",
    "ci_flake", "provider_unavailable", "connection", "temporary",
})

# Keywords that indicate a fixable failure
_FIXABLE_SIGNALS = frozenset({
    "lint", "test_failure", "test failure", "gate_finding", "gate finding",
    "review_finding", "syntax", "import_error", "import error", "assertion",
})


@dataclass
class FailureClassification:
    failure_class: str          # one of FAILURE_CLASSES
    is_recoverable: bool        # True for transient or fixable
    recovery_hint: str          # human-readable guidance


def classify_failure(reason: str, hint: Optional[str] = None) -> FailureClassification:
    """Classify a feature failure into one of three contract categories.

    Uses keyword matching on reason string; hint overrides classification when
    the caller provides an explicit failure_class keyword.

    Returns FailureClassification with failure_class, is_recoverable, recovery_hint.
    """
    # Explicit override via hint (allows callers to set class directly)
    if hint and hint.strip() in FAILURE_CLASSES:
        fc = hint.strip()
        recoverable = fc != FAILURE_CLASS_NON_RECOVERABLE
        return FailureClassification(
            failure_class=fc,
            is_recoverable=recoverable,
            recovery_hint=f"explicit hint: {fc}",
        )

    lower = reason.lower()

    if any(sig in lower for sig in _TRANSIENT_SIGNALS):
        return FailureClassification(
            failure_class=FAILURE_CLASS_TRANSIENT,
            is_recoverable=True,
            recovery_hint="transient failure — retry same work from current main",
        )

    if any(sig in lower for sig in _FIXABLE_SIGNALS):
        return FailureClassification(
            failure_class=FAILURE_CLASS_FIXABLE,
            is_recoverable=True,
            recovery_hint="fixable failure — requeue with fix applied",
        )

    return FailureClassification(
        failure_class=FAILURE_CLASS_NON_RECOVERABLE,
        is_recoverable=False,
        recovery_hint="non-recoverable — requires human intervention or scope change",
    )


# ---------------------------------------------------------------------------
# Recovery decision (contract Section 4.2 and 4.3)
# ---------------------------------------------------------------------------

RECOVERY_ACTION_REQUEUE = "requeue"
RECOVERY_ACTION_BLOCK = "block"
RECOVERY_ACTION_ESCALATE = "escalate"

MAX_ATTEMPTS_PER_CLASS = 2   # R-2: max 2 retries per failure class
MAX_TOTAL_ATTEMPTS = 3       # R-3: max 3 total retries across all classes


@dataclass
class RecoveryDecision:
    action: str                 # requeue / block / escalate
    failure_class: str
    requeue_count: int          # total attempts so far for this feature
    reason: str                 # human-readable explanation
    must_start_from_main: bool = True  # R-4: always True for requeue


def _count_attempts_for_class(history_entry: Dict[str, Any], failure_class: str) -> int:
    by_class = history_entry.get("failure_classes") or {}
    return int(by_class.get(failure_class, 0))


def evaluate_recovery(
    feature_id: str,
    failure_reason: str,
    requeue_history: Dict[str, Any],
    failure_class_hint: Optional[str] = None,
) -> RecoveryDecision:
    """Apply the contract recovery decision tree to determine the correct action.

    Args:
        feature_id: PR id being evaluated (e.g. "PR-1")
        failure_reason: raw failure reason string
        requeue_history: dict from chain_state["requeue_history"] keyed by feature_id
        failure_class_hint: optional explicit failure class override

    Returns RecoveryDecision with action, failure_class, requeue_count, and reason.
    """
    classification = classify_failure(failure_reason, hint=failure_class_hint)
    history_entry = requeue_history.get(feature_id) or {}
    total_attempts = int(history_entry.get("total_attempts", 0))

    # R-3: hard cap on total attempts
    if total_attempts >= MAX_TOTAL_ATTEMPTS:
        return RecoveryDecision(
            action=RECOVERY_ACTION_ESCALATE,
            failure_class=classification.failure_class,
            requeue_count=total_attempts,
            reason=f"max total attempts ({MAX_TOTAL_ATTEMPTS}) exceeded for {feature_id}",
        )

    # Non-recoverable failures always escalate
    if not classification.is_recoverable:
        return RecoveryDecision(
            action=RECOVERY_ACTION_ESCALATE,
            failure_class=classification.failure_class,
            requeue_count=total_attempts,
            reason=f"non-recoverable failure for {feature_id}: {failure_reason[:120]}",
        )

    # R-2: per-class retry cap
    class_attempts = _count_attempts_for_class(history_entry, classification.failure_class)
    if class_attempts >= MAX_ATTEMPTS_PER_CLASS:
        return RecoveryDecision(
            action=RECOVERY_ACTION_ESCALATE,
            failure_class=classification.failure_class,
            requeue_count=total_attempts,
            reason=(
                f"max per-class attempts ({MAX_ATTEMPTS_PER_CLASS}) exceeded for "
                f"{feature_id} class={classification.failure_class}"
            ),
        )

    return RecoveryDecision(
        action=RECOVERY_ACTION_REQUEUE,
        failure_class=classification.failure_class,
        requeue_count=total_attempts,
        reason=classification.recovery_hint,
        must_start_from_main=True,
    )


def record_failure_attempt(
    requeue_history: Dict[str, Any],
    feature_id: str,
    failure_class: str,
) -> Dict[str, Any]:
    """Update requeue_history in-place with the recorded failure attempt.

    Returns the updated history dict.
    """
    entry = requeue_history.setdefault(feature_id, {"total_attempts": 0, "failure_classes": {}})
    entry["total_attempts"] = int(entry.get("total_attempts", 0)) + 1
    by_class = entry.setdefault("failure_classes", {})
    by_class[failure_class] = int(by_class.get(failure_class, 0)) + 1
    return requeue_history


# ---------------------------------------------------------------------------
# Resume safety (contract Section 2.3)
# ---------------------------------------------------------------------------

# States from which the chain can safely resume without re-evaluation
RESUME_SAFE_STATES = frozenset({
    "INITIALIZED",
    "FEATURE_ACTIVE",
    "FEATURE_ADVANCING",
    "CHAIN_COMPLETE",
})

# States that require explicit recovery before resuming
RESUME_UNSAFE_STATES = frozenset({
    "FEATURE_FAILED",
    "RECOVERY_PENDING",
    "ADVANCEMENT_BLOCKED",
    "CHAIN_HALTED",
    "NOT_INITIALIZED",
})


def is_resume_safe(chain_state: Optional[Dict[str, Any]]) -> bool:
    """Return True if the chain can be resumed from its current state.

    Resume is safe when the chain state is in RESUME_SAFE_STATES.
    A None chain_state (no chain_state.json) is treated as NOT_INITIALIZED — not safe.
    """
    if chain_state is None:
        return False
    current = str(chain_state.get("current_state", "NOT_INITIALIZED"))
    return current in RESUME_SAFE_STATES


# ---------------------------------------------------------------------------
# Branch baseline guard (contract Section 5.4)
# ---------------------------------------------------------------------------

GitRunner = Callable[[List[str]], str]  # callable(args) -> stdout


def _default_git_runner(args: List[str]) -> str:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[1:]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass
class BaselineCheckResult:
    is_valid: bool
    feature_branch: str
    expected_main_sha: str
    actual_merge_base: str
    reason: str


def _baseline_result(
    valid: bool, branch: str, expected: str, actual: str, reason: str
) -> BaselineCheckResult:
    return BaselineCheckResult(
        is_valid=valid, feature_branch=branch,
        expected_main_sha=expected, actual_merge_base=actual, reason=reason,
    )


def _sha_prefix_matches(expected: str, actual: str) -> bool:
    """Compare SHA strings by shortest common prefix (up to 40 chars)."""
    e, a = expected.strip().lower(), actual.strip().lower()
    n = min(len(e), len(a), 40)
    return n > 0 and a[:n] == e[:n]


def _is_ancestor(run: GitRunner, repo_root: str, ancestor: str, descendant: str) -> bool:
    """Return True if ancestor is an ancestor of descendant."""
    try:
        run(["git", "-C", repo_root, "merge-base", "--is-ancestor", ancestor, descendant])
        return True
    except RuntimeError:
        return False


def check_branch_baseline(
    feature_branch: str,
    expected_main_sha: str,
    repo_root: str = ".",
    git_runner: Optional[GitRunner] = None,
) -> BaselineCheckResult:
    """Verify the feature branch is derived from the expected main SHA.

    Implements contract rules S-1 through S-3: blocks advancement when a
    feature branch's merge-base with main is older than the last recorded
    merge SHA.
    """
    run = git_runner or _default_git_runner

    try:
        actual = run(["git", "-C", repo_root, "merge-base", feature_branch, "main"])
    except RuntimeError as exc:
        return _baseline_result(False, feature_branch, expected_main_sha, "", f"git merge-base failed: {exc}")

    if not expected_main_sha:
        return _baseline_result(True, feature_branch, expected_main_sha, actual, "no expected SHA recorded — baseline accepted")

    if _sha_prefix_matches(expected_main_sha, actual):
        return _baseline_result(True, feature_branch, expected_main_sha, actual, "branch baseline matches expected main SHA")

    if _is_ancestor(run, repo_root, expected_main_sha, actual):
        return _baseline_result(True, feature_branch, expected_main_sha, actual,
                                "merge-base is a descendant of expected main SHA — baseline valid")

    return _baseline_result(
        False, feature_branch, expected_main_sha, actual,
        f"stale branch: merge-base {actual[:12]} does not match "
        f"expected {expected_main_sha[:12]} — recreate worktree from current main",
    )


# ---------------------------------------------------------------------------
# Carry-forward snapshot (contract Section 6.1, 6.3, 6.5)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _load_carry_forward(state_dir: Path) -> Dict[str, Any]:
    data = _safe_load_json(state_dir / "chain_carry_forward.json") or {}
    return {
        "chain_id": data.get("chain_id", ""),
        "findings": data.get("findings") if isinstance(data.get("findings"), list) else [],
        "open_items": data.get("open_items") if isinstance(data.get("open_items"), list) else [],
        "deferred_items": data.get("deferred_items") if isinstance(data.get("deferred_items"), list) else [],
        "residual_risks": data.get("residual_risks") if isinstance(data.get("residual_risks"), list) else [],
        "feature_summaries": data.get("feature_summaries") if isinstance(data.get("feature_summaries"), list) else [],
    }


def _write_carry_forward(state_dir: Path, ledger: Dict[str, Any]) -> None:
    (state_dir / "chain_carry_forward.json").write_text(
        json.dumps(ledger, indent=2), encoding="utf-8"
    )


def _accumulate_open_items(
    ledger: Dict[str, Any], open_items: List[Dict[str, Any]], feature_id: str, now: str
) -> None:
    """Merge open items into carry-forward ledger (O-1/O-5: snapshot + never drop)."""
    for item in open_items:
        existing_ids = {i.get("id") for i in ledger["open_items"]}
        item_copy = dict(item)
        item_copy.setdefault("origin_feature", feature_id)
        item_copy.setdefault("snapshotted_at", now)
        if item_copy.get("id") not in existing_ids:
            ledger["open_items"].append(item_copy)
        else:
            for i, existing in enumerate(ledger["open_items"]):
                if existing.get("id") == item_copy.get("id"):
                    merged = {**existing, **item_copy}
                    # Preserve original origin_feature — provenance must not drift
                    if "origin_feature" in existing:
                        merged["origin_feature"] = existing["origin_feature"]
                    ledger["open_items"][i] = merged
                    break


def _build_feature_summary(
    feature_id: str, feature_name: str, status: str, prs_merged: List[str],
    merge_shas: List[str], gate_results: Dict[str, str],
    findings: List[Dict[str, Any]], open_items: List[Dict[str, Any]],
    residual_risks: List[Dict[str, Any]], requeue_count: int, now: str,
) -> Dict[str, Any]:
    """Build a feature summary record (contract Section 6.5)."""
    return {
        "feature_id": feature_id,
        "feature_name": feature_name,
        "status": status,
        "completed_at": now,
        "prs_merged": prs_merged,
        "merge_shas": merge_shas,
        "gate_results": gate_results,
        "findings_created": len(findings),
        "findings_resolved": len([f for f in findings if str(f.get("resolution_status", "")).lower() == "resolved"]),
        "open_items_created": len([i for i in open_items if i.get("status") != "done"]),
        "open_items_resolved": len([i for i in open_items if i.get("status") == "done"]),
        "open_items_deferred": len([i for i in open_items if i.get("status") == "deferred"]),
        "residual_risks": len(residual_risks),
        "requeue_count": requeue_count,
    }


def snapshot_feature_boundary(
    state_dir: str | Path,
    *,
    feature_id: str,
    feature_name: str,
    status: str,
    prs_merged: List[str],
    merge_shas: Optional[List[str]] = None,
    gate_results: Optional[Dict[str, str]] = None,
    findings: Optional[List[Dict[str, Any]]] = None,
    open_items: Optional[List[Dict[str, Any]]] = None,
    deferred_items: Optional[List[Dict[str, Any]]] = None,
    residual_risks: Optional[List[Dict[str, Any]]] = None,
    requeue_count: int = 0,
) -> Dict[str, Any]:
    """Snapshot a feature's completion state into the carry-forward ledger.

    Returns the updated ledger.
    """
    state_root = Path(state_dir)
    ledger = _load_carry_forward(state_root)
    now = _now_iso()
    safe_findings = findings or []
    safe_items = open_items or []
    safe_deferred = deferred_items or []
    safe_risks = residual_risks or []

    for f in safe_findings:
        entry = dict(f)
        entry.setdefault("source_feature", feature_id)
        entry.setdefault("recorded_at", now)
        ledger["findings"].append(entry)

    _accumulate_open_items(ledger, safe_items, feature_id, now)

    for item in safe_deferred:
        entry = dict(item)
        entry.setdefault("origin_feature", feature_id)
        entry.setdefault("deferred_at", now)
        ledger["deferred_items"].append(entry)

    for risk in safe_risks:
        entry = dict(risk)
        entry.setdefault("accepting_feature", feature_id)
        entry.setdefault("recorded_at", now)
        ledger["residual_risks"].append(entry)

    ledger["feature_summaries"].append(_build_feature_summary(
        feature_id, feature_name, status, prs_merged, merge_shas or [],
        gate_results or {}, safe_findings, safe_items, safe_risks, requeue_count, now,
    ))

    _write_carry_forward(state_root, ledger)
    return ledger


def build_next_feature_context(
    state_dir: str | Path,
) -> Dict[str, Any]:
    """Build the carry-forward context to inject into the next feature's dispatch.

    Returns a dict that T0 can include in the next feature's dispatch context
    so workers are aware of accumulated debt (contract O-4).
    """
    state_root = Path(state_dir)
    ledger = _load_carry_forward(state_root)

    unresolved_items = [
        i for i in ledger.get("open_items") or []
        if str(i.get("status", "")).lower() not in {"done", "closed", "resolved", "wontfix"}
    ]
    blocker_items = [i for i in unresolved_items if str(i.get("severity", "")).lower() == "blocker"]
    warn_items = [i for i in unresolved_items if str(i.get("severity", "")).lower() == "warn"]

    open_findings = [
        f for f in ledger.get("findings") or []
        if str(f.get("resolution_status", "")).lower() != "resolved"
    ]

    last_summary = (ledger.get("feature_summaries") or [None])[-1]

    return {
        "carry_forward_chain_id": ledger.get("chain_id", ""),
        "unresolved_item_count": len(unresolved_items),
        "blocker_item_count": len(blocker_items),
        "warn_item_count": len(warn_items),
        "open_finding_count": len(open_findings),
        "residual_risk_count": len(ledger.get("residual_risks") or []),
        "features_completed": len(ledger.get("feature_summaries") or []),
        "last_feature_summary": last_summary,
        "blocker_items": blocker_items,
        "warn_items": warn_items,
        "generated_at": _now_iso(),
    }
