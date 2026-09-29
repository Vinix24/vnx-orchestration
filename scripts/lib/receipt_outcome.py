#!/usr/bin/env python3
"""Per-dispatch outcome from the receipt ledger (fabric-state-herstel D3).

The ledger holds several receipts per dispatch: two outcome writers (A:
envelope/governance emit, no ``report_file``; B: report_parser, with
``report_file``), gate results, merge events and pure bookkeeping. A reader
that judges per line counts one dispatch four times and lets B's ``unknown``
overwrite A's ``failure``. This module reads the stream once and returns one
outcome per dispatch.

The ledger stays append-only (ADR-005): every correction lives here, in the
reader. ``OUTCOME_READER_EPOCH`` marks when the reader began interpreting the
ledger this way, so a reader comparing an old per-line count to a new
per-dispatch count has an auditable reason for the delta.

No I/O and no central DB access: the caller streams receipts in file order.
File order, not timestamp order, decides "last": 199 of 293 ids are not in
timestamp order (two formats, two integer timestamps).
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from receipt_verdict import compute_verdict

OUTCOME_READER_EPOCH = "2026-09-29T00:00:00+00:00"

# One place for what is bookkeeping: these events are neither an outcome nor
# evidence for one.
BOOKKEEPING_EVENT_TYPES = frozenset(
    {
        "state_mutation",
        "review_gate_request",
        "track_reconcile_advisory",
        "roadmap_dispatch_step",
        "context_rotation_continuation",
    }
)

# Not neutral: a merge or a gate obligation is still open after such an event.
BLOCKING_EVENT_TYPES = frozenset(
    {
        "pr_merge_refused",
        "door_bookkeeping_failed",
        "gate_obligation_reopened_stale_evidence",
    }
)

EVIDENCE_EVENT_TYPES = frozenset({"review_gate_result", "pr_merged"})

INVALID_EVENT_TYPE = "report_contract_invalid"
INVALID_STATUS = "contract_invalid"

_UNRESOLVED_IDS = frozenset({"", "unknown", "?"})
_TEMP_PREFIXES = ("/var/folders/", "/private/var/folders/", "/tmp/", "/private/tmp/")
_GATE_ID_RE = re.compile(r"^(?:[a-z0-9_]+-)*gate-pr-?(\d+)", re.IGNORECASE)

DECISIONS = ("accept", "investigate", "reject", "superseded")


def _temp_prefixes() -> tuple:
    return _TEMP_PREFIXES + (tempfile.gettempdir().rstrip("/") + "/",)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """ISO string (with or without ``Z``) or integer/float epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dispatch_id(receipt: Dict[str, Any]) -> str:
    return str(receipt.get("dispatch_id") or "").strip()


def _pr_number(receipt: Dict[str, Any]) -> Optional[str]:
    for key in ("pr_number", "pr_id"):
        value = str(receipt.get(key) or "").strip()
        if value.isdigit():
            return value
    return None


def noise_reason(receipt: Dict[str, Any], project_id: Optional[str]) -> Optional[str]:
    """Why a receipt is test noise or foreign, or None for a real receipt.

    Never filters on lane: real plan-gate and panel runs use the synthesized lane.
    """
    if str(receipt.get("source") or "").strip().lower() == "pytest":
        return "pytest"
    receipt_project = str(receipt.get("project_id") or "").strip()
    if project_id and receipt_project and receipt_project != project_id:
        return "foreign_project"
    prefixes = _temp_prefixes()
    for key in ("report_path", "report_file"):
        if str(receipt.get(key) or "").startswith(prefixes):
            return "temp_report_path"
    if "MagicMock" in json.dumps(receipt, default=str):
        return "magicmock"
    return None


class _DispatchState:
    __slots__ = (
        "a", "a_idx", "b", "b_idx", "invalid_idx", "last_outcome_idx",
        "blocking", "evidence", "parents", "branches", "latest_ts", "other_events",
    )

    def __init__(self) -> None:
        self.a: Optional[Dict[str, Any]] = None
        self.a_idx = -1
        self.b: Optional[Dict[str, Any]] = None
        self.b_idx = -1
        self.invalid_idx = -1
        self.last_outcome_idx = -1
        self.blocking: List[tuple] = []
        self.evidence: List[Dict[str, Any]] = []
        self.parents: set = set()
        self.branches: set = set()
        self.latest_ts: Optional[datetime] = None
        self.other_events = 0

    def touch(self, receipt: Dict[str, Any]) -> None:
        ts = parse_timestamp(receipt.get("timestamp"))
        if ts is not None and (self.latest_ts is None or ts > self.latest_ts):
            self.latest_ts = ts


def _evidence_row(receipt: Dict[str, Any]) -> Dict[str, Any]:
    if receipt.get("event_type") == "pr_merged":
        return {"kind": "pr_merged", "pr": _pr_number(receipt), "result": receipt.get("conclusion") or receipt.get("status")}
    return {
        "kind": "review_gate_result",
        "gate": receipt.get("gate"),
        "pr": _pr_number(receipt),
        "result": receipt.get("gate_status") or receipt.get("status"),
        "blocking_count": receipt.get("blocking_count"),
    }


def _gate_run_row(did: str, receipt: Dict[str, Any]) -> Dict[str, Any]:
    """A gate's own dispatch (`kimi-gate-pr1890-...`) is evidence for the PR, not a dispatch outcome."""
    return {"kind": "gate_run", "gate": did, "pr": _pr_number(receipt), "result": receipt.get("status")}


def _merged_receipt(state: _DispatchState) -> Dict[str, Any]:
    """Status from A (else B); verification from B (else A); warnings of both."""
    base = dict(state.a if state.a is not None else state.b)
    if state.a is not None and state.b is not None:
        base["verification"] = state.b.get("verification")
        base["warnings"] = list(state.a.get("warnings") or []) + list(state.b.get("warnings") or [])
    return base


def _decide(state: _DispatchState) -> Dict[str, Any]:
    if state.a is None and state.b is None and state.invalid_idx < 0:
        if state.blocking:
            return {"decision": "investigate", "reason": f"{state.blocking[-1][1]} and no outcome receipt", "status": None}
        return {"decision": "unknown", "reason": "no task_complete receipt", "status": None}
    if state.invalid_idx > state.a_idx:
        return {"decision": "reject", "reason": "contract_invalid after the last outcome receipt", "status": INVALID_STATUS}
    merged = _merged_receipt(state)
    verdict = compute_verdict(merged)
    decision, reason = verdict["decision"], verdict["reason"]
    late = [event for idx, event in state.blocking if idx > state.last_outcome_idx]
    if late and decision != "reject":
        decision = "investigate"
        reason = f"{late[-1]} after the last outcome receipt ({reason})"
    return {"decision": decision, "reason": reason, "status": merged.get("status")}


def _link_superseded(outcomes: Dict[str, Dict[str, Any]], states: Dict[str, _DispatchState]) -> None:
    """A reject is superseded only by an explicit link from an accepted child:
    ``parent_dispatch`` on the child, or the child working on ``dispatch/<parent>``."""
    for child_id, child in outcomes.items():
        if child["decision"] != "accept":
            continue
        state = states[child_id]
        parents = set(state.parents)
        parents.update(b[len("dispatch/"):] for b in state.branches if b.startswith("dispatch/"))
        for parent_id in sorted(parents):
            parent = outcomes.get(parent_id)
            if parent_id != child_id and parent is not None and parent["decision"] == "reject":
                parent["decision"] = "superseded"
                parent["superseded_by"] = child_id


def compute_outcomes(
    receipts: Iterable[Dict[str, Any]],
    *,
    project_id: Optional[str] = None,
    since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fold a receipt stream (file order) into one outcome per dispatch.

    Returns ``{"outcomes": {dispatch_id: {...}}, "counts": {...}, "bookkeeping": n,
    "noise": {reason: n}, "evidence_unlinked": n}``. ``since`` keeps only dispatches
    whose outcome-bearing receipts reach into the window; the counts follow.
    """
    states: Dict[str, _DispatchState] = {}
    noise: Dict[str, int] = {}
    bookkeeping = 0
    pr_to_dispatch: Dict[str, str] = {}
    pending_evidence: List[tuple] = []

    for idx, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            continue
        reason = noise_reason(receipt, project_id)
        if reason:
            noise[reason] = noise.get(reason, 0) + 1
            continue
        event = str(receipt.get("event_type") or "")
        if event in BOOKKEEPING_EVENT_TYPES:
            bookkeeping += 1
            continue
        did = _dispatch_id(receipt)
        if did.lower() in _UNRESOLVED_IDS:
            noise["unresolved_id"] = noise.get("unresolved_id", 0) + 1
            continue
        gate_match = _GATE_ID_RE.match(did)
        pr = _pr_number(receipt) or (gate_match.group(1) if gate_match else None)

        if event in EVIDENCE_EVENT_TYPES:
            pending_evidence.append((did, gate_match is not None, pr, _evidence_row(receipt)))
            if gate_match is None and pr:
                pr_to_dispatch[pr] = did
            continue
        if gate_match is not None:
            if event == "task_complete":
                pending_evidence.append((did, True, pr, _gate_run_row(did, receipt)))
            else:
                noise["gate_dispatch_other"] = noise.get("gate_dispatch_other", 0) + 1
            continue

        state = states.setdefault(did, _DispatchState())
        if pr and event == "task_complete":
            pr_to_dispatch[pr] = did
        parent = str(receipt.get("parent_dispatch") or "").strip()
        if parent:
            state.parents.add(parent)
        branch = str(receipt.get("branch") or "").strip()
        if branch:
            state.branches.add(branch)
        state.touch(receipt)

        status = str(receipt.get("status") or "")
        if event in BLOCKING_EVENT_TYPES:
            state.blocking.append((idx, event))
        elif event == "task_complete" and receipt.get("receipt_kind") == "dispatch":
            if receipt.get("report_file"):
                state.b, state.b_idx = receipt, idx
            else:
                state.a, state.a_idx = receipt, idx
            state.last_outcome_idx = idx
            if status == INVALID_STATUS:
                state.invalid_idx = idx
        elif event == INVALID_EVENT_TYPE or status == INVALID_STATUS:
            state.invalid_idx = idx
            state.last_outcome_idx = idx
        else:
            state.other_events += 1

    outcomes: Dict[str, Dict[str, Any]] = {}
    for did, state in states.items():
        outcome = _decide(state)
        outcome.update(dispatch_id=did, evidence=[], latest_timestamp=state.latest_ts)
        verification = _merged_receipt(state).get("verification") if (state.a or state.b) else None
        outcome["verification_method"] = (verification or {}).get("method")
        outcomes[did] = outcome

    evidence_unlinked = evidence_only = 0
    for did, is_gate_id, pr, row in pending_evidence:
        target = pr_to_dispatch.get(pr) if (is_gate_id and pr) else did
        if target is None:
            evidence_unlinked += 1
        elif target in outcomes:
            outcomes[target]["evidence"].append(row)
        else:
            evidence_only += 1

    _link_superseded(outcomes, states)

    if since is not None:
        outcomes = {
            did: o for did, o in outcomes.items()
            if o["latest_timestamp"] is not None and o["latest_timestamp"] >= since
        }
    counts = {name: 0 for name in DECISIONS}
    counts["unknown"] = 0
    for outcome in outcomes.values():
        counts[outcome["decision"]] += 1
    return {
        "outcomes": outcomes,
        "counts": counts,
        "bookkeeping": bookkeeping,
        "noise": noise,
        "evidence_unlinked": evidence_unlinked,
        "evidence_only": evidence_only,
    }
