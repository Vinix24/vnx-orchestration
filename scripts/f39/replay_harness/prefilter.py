from __future__ import annotations

from typing import Any

_REASON_KEYWORDS: dict[str, list[str]] = {
    "DISPATCH": ["dispatch", "next task", "next work", "assign", "send to", "proceed"],
    "COMPLETE": ["complete", "merge", "done", "finished", "closure", "close pr"],
    "REJECT":   ["reject", "missing", "incomplete", "not found", "invalid", "unverifi"],
    "WAIT":     ["wait", "busy", "no action", "hold", "not yet", "ghost", "duplicate", "unknown"],
    "ESCALATE": ["escalate", "blocker", "human", "intervention", "chain-breaking", "architectural"],
}


def _reason_aligns(decision: str, reason_text: str) -> bool:
    """Check whether the reason text semantically fits the decision."""
    keywords = _REASON_KEYWORDS.get(decision.upper(), [])
    lower = reason_text.lower()
    return any(kw in lower for kw in keywords)


def _has_pending_required_gates(state: dict[str, Any]) -> bool:
    """Return True if any required gate has not passed."""
    flat_gates = state.get("review_gates", {})
    return any(
        isinstance(gate_data, dict)
        and gate_data.get("required", False)
        and gate_data.get("status", "") not in ("completed", "passed", "pass")
        for gate_data in flat_gates.values()
    )


def _has_pending_gate_requests(state: dict[str, Any]) -> bool:
    """Return True if any gate is null, requested, or queued (feature complete check)."""
    review_gates = state.get("review_gates", {})
    for pr_gates in review_gates.values():
        if not isinstance(pr_gates, dict):
            continue
        for gate_val in pr_gates.values():
            if gate_val is None:
                return True
            if isinstance(gate_val, dict) and gate_val.get("status") in ("requested", "queued"):
                return True
    return False


def _code_prefilter(receipt: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Deterministic pre-filter. Returns decision string or None (needs LLM)."""
    dispatch_id = receipt.get("dispatch_id", "")
    if not dispatch_id or dispatch_id.startswith("unknown-"):
        return "WAIT"

    recent = [r.get("dispatch_id") for r in state.get("recent_receipts", [])]
    if dispatch_id in recent:
        return "WAIT"

    terminals = state.get("terminals", {})
    if terminals and all(not t.get("ready", False) for t in terminals.values()):
        return "WAIT"

    if _has_pending_required_gates(state):
        return "WAIT"

    pr = state.get("pr_progress", {})
    oi = state.get("open_items", {})
    if (
        pr.get("completion_pct", 0) >= 100
        and oi.get("blocker_count", 0) == 0
        and state.get("queues", {}).get("pending_count", 0) == 0
    ):
        if _has_pending_gate_requests(state):
            return None
        return "COMPLETE"

    if pr.get("blocked", []):
        return "WAIT"

    if receipt.get("status") == "failure" and receipt.get("retry_count", 0) < 3:
        return "DISPATCH"

    if (
        receipt.get("status") == "success"
        and receipt.get("ci_status") == "failure"
        and receipt.get("ci_failure_check")
    ):
        return "DISPATCH"

    files_claimed = receipt.get("files_modified") or (
        receipt.get("provenance", {}).get("diff_summary", {}).get("files_changed", 0)
    )
    git_evidence = receipt.get("provenance", {}).get("git_ref")
    if files_claimed and not git_evidence:
        return None

    return None
