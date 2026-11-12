#!/usr/bin/env python3
"""Failure class and per-dispatch merge for the learning loop (ADR-065 §1 and §2).

Two pure functions over receipt dicts, no I/O and no model:

* ``merge_dispatch_group`` folds every receipt of one dispatch into one record.
* ``classify_failure`` gives that group one class from one ordered table.

A missing, ``None``, unreadable or unknown field never excludes a receipt: it
falls through to a lower row of the table, and the answer is always a member of
``FAILURE_CLASSES``.

The learning loop calls this module: ``extract_failure_patterns`` merges the
receipts of one dispatch and ``generate_prevention_rules`` groups on the class.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence

from contract_invalid_window import is_contract_invalid

FAILURE_CLASSES = frozenset({
    "contract_invalid",
    "contract_violations_empty",
    "phantom",
    "timeout",
    "provider_quota",
    "review_failed",
    "budget",
    "failed",
    "unclassified",
})

# contract_violations_empty (legacy rows no proposal can fix) and unclassified
# (no content) are counted and never become a rule.
ELIGIBLE_CLASSES = frozenset({
    "contract_invalid",
    "phantom",
    "timeout",
    "provider_quota",
    "review_failed",
    "budget",
    "failed",
})

# Highest rank first: identity fields come from the first receipt of this order.
_EVENT_RANK = ("dispatch_finished", "subprocess_completion", "task_complete", "review_gate_result")
_LOWEST_RANK = len(_EVENT_RANK)

IDENTITY_FIELDS = ("task_id", "task_description", "terminal", "terminal_id", "provider", "model", "timestamp")
EVIDENCE_FIELDS = ("failure_reason", "error", "message", "detail", "terminal_response", "contract_violations")


def _event_type(receipt: Mapping) -> str:
    return str(receipt.get("event_type") or receipt.get("event") or "").strip().lower()


def _rank(receipt: Mapping) -> int:
    try:
        return _EVENT_RANK.index(_event_type(receipt))
    except ValueError:
        return _LOWEST_RANK


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _ordered(receipts: Iterable[Mapping]) -> List[Mapping]:
    """Receipts by rank; a stable sort keeps ledger order inside one rank."""
    return sorted((r for r in receipts if isinstance(r, Mapping)), key=_rank)


def merge_dispatch_group(receipts: Iterable[Mapping]) -> Dict[str, Any]:
    """One record per dispatch. Identity fields (and everything not named as
    evidence) come from the highest-ranked receipt; each evidence field is the
    first non-empty value in rank order, ``None`` when no receipt has one."""
    ordered = _ordered(receipts)
    if not ordered:
        return {}
    merged: Dict[str, Any] = dict(ordered[0])
    for name in EVIDENCE_FIELDS:
        merged[name] = next((r[name] for r in ordered if _non_empty(r.get(name))), None)
    return merged


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def classify_failure(group: Sequence[Mapping]) -> str:
    """The class of one dispatch group, first matching row of ADR-065 §1."""
    receipts = _ordered(group)
    if not receipts:
        return "unclassified"
    evidence = merge_dispatch_group(receipts)
    reason = _text(evidence.get("failure_reason")).strip()
    reason_lower = reason.lower()
    statuses = {str(r.get("status") or "").strip().lower() for r in receipts}

    invalid = [r for r in receipts if is_contract_invalid(r)]
    if invalid:
        if any(_non_empty(r.get("contract_violations")) for r in invalid):
            return "contract_invalid"
        return "contract_violations_empty"
    if reason.startswith("PHANTOM:"):
        return "phantom"
    if "timeout" in statuses or reason_lower.startswith("deadline exceeded"):
        return "timeout"
    if "[quota_or_auth]" in reason:
        return "provider_quota"
    if any(_event_type(r) == "review_gate_result" for r in receipts):
        return "review_failed"
    if "budget" in reason:
        return "budget"
    # Late import: learning_loop imports this module, so a top-level import
    # would be circular. The set stays defined in one place.
    from learning_loop import _FAILURE_STATUSES

    # A failure status with no evidence text at all is "no content": it is
    # counted as unclassified and never becomes the rule "Unknown error".
    if statuses & _FAILURE_STATUSES and any(_non_empty(evidence.get(f)) for f in EVIDENCE_FIELDS):
        return "failed"
    return "unclassified"
