#!/usr/bin/env python3
"""receipt_outcome.py — one outcome per dispatch, read from the receipt ledger.

Every ledger line carries a verdict stamp (``compute_verdict`` also runs on
bookkeeping), and a dispatch writes two outcome receipts: writer A (no
``report_file``) carries the lane status, writer B (report_parser, with
``report_file``) carries ``done``/``unknown`` plus verification. Counting
stamps per line counts bookkeeping as judgments and lets B's ``unknown``
overwrite A's ``failure``. This is the one place that folds a dispatch's
lines into one outcome (fabric-state-herstel D3):

1. Noise out: ``source=pytest``, temp-dir ``report_path``, ``MagicMock``,
   id missing/``unknown``/``?``, another ``project_id``. Never on lane.
2. ``BOOKKEEPING_EVENT_TYPES`` are neither outcome nor evidence.
3. Status: the last writer-A receipt in FILE order (ledger timestamps are not
   monotonic); a later contract_invalid event wins as reject. A retry under
   the same id is a new last A.
4. Verification: the last writer-B receipt; ``compute_verdict`` on the merge.
5. Evidence only: ``review_gate_result``, ``pr_merged`` and gate-runner
   dispatches (``kimi-gate-pr<N>-<ts>``), grouped on PR and linked to the work
   dispatch that carries that PR.
6. ``BLOCKING_EVENT_TYPES`` after the last outcome turn accept into
   investigate, unless resolved later: a refused merge by a ``pr_merged`` on
   that PR, a reopened obligation by a ``review_gate_result`` for that gate
   and PR. ``door_bookkeeping_failed`` is never resolved.
7. ``superseded`` only via an explicit link: a child carrying
   ``parent_dispatch`` or working on branch ``dispatch/<parent>``.

The ledger is append-only (ADR-005): this is a reader-side correction, dated
by ``OUTCOME_READER_EPOCH``. No database is read; the per-project ledger plus
the ``project_id`` filter is the whole scope (ADR-007).
"""

from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from receipt_verdict import compute_verdict

# When the per-dispatch reading replaced the per-line stamp count: the auditable
# reason for a delta between digests before and after. The ledger is untouched.
OUTCOME_READER_EPOCH = "2026-09-29T00:00:00+00:00"

BOOKKEEPING_EVENT_TYPES = frozenset({
    "state_mutation", "review_gate_request", "track_reconcile_advisory",
    "roadmap_dispatch_step", "context_rotation_continuation",
})
EVIDENCE_EVENT_TYPES = frozenset({"review_gate_result", "pr_merged"})
BLOCKING_EVENT_TYPES = frozenset({
    "pr_merge_refused", "door_bookkeeping_failed", "gate_obligation_reopened_stale_evidence",
})
CONTRACT_INVALID_EVENT_TYPES = frozenset({"contract_invalid", "report_contract_invalid"})
# subprocess_completion is the same writer family as A: phantom_guard and
# pr_enforcement use it to overturn a claimed success, and the synthesized
# plan-gate lane reports its status only through it.
OUTCOME_EVENT_TYPES = frozenset({"task_complete", "subprocess_completion"})

INVALID_DISPATCH_IDS = frozenset({"", "unknown", "?", "none", "null"})
GATE_DISPATCH_RE = re.compile(r"^(?P<gate>[a-z0-9]+)-gate-pr(?P<pr>\d+)-\d+$")

_TEMP_PREFIXES = tuple({"/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
                        str(Path(tempfile.gettempdir()).resolve()) + "/"})


def _dispatch_id(receipt: Dict[str, Any]) -> str:
    return str(receipt.get("dispatch_id") or "").strip()


def _pr_of(receipt: Dict[str, Any]) -> Optional[str]:
    for key in ("pr_number", "pr_id"):
        value = receipt.get(key)
        if value not in (None, "", "null"):
            return str(value).lstrip("#")
    return None


def noise_reason(receipt: Dict[str, Any], project_id: str) -> Optional[str]:
    """Why a receipt is test noise or out of scope, or None when it is real."""
    if str(receipt.get("source") or "").strip().lower() == "pytest":
        return "pytest"
    foreign = receipt.get("project_id")
    if foreign not in (None, "") and str(foreign) != project_id:
        return "foreign_project"
    if str(receipt.get("report_path") or "").startswith(_TEMP_PREFIXES):
        return "temp_report_path"
    if "MagicMock" in json.dumps(receipt, default=str):
        return "magicmock"
    return None


def _is_writer_a(receipt: Dict[str, Any]) -> bool:
    return (receipt.get("event_type") in OUTCOME_EVENT_TYPES
            and receipt.get("receipt_kind") in ("dispatch", None)
            and not receipt.get("report_file"))


def _is_writer_b(receipt: Dict[str, Any]) -> bool:
    return (receipt.get("event_type") == "task_complete"
            and receipt.get("receipt_kind") in ("dispatch", None)
            and bool(receipt.get("report_file")))


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _new_record(dispatch_id: str) -> Dict[str, Any]:
    return {"dispatch_id": dispatch_id, "a": None, "b": None, "a_pos": -1, "b_pos": -1,
            "contract_invalid_pos": -1, "contract_invalid": None, "evidence": [],
            "blocking": [], "children_of": set(), "in_window": False}


def _evidence_entry(receipt: Dict[str, Any], pos: int) -> Dict[str, Any]:
    gate_match = GATE_DISPATCH_RE.match(_dispatch_id(receipt))
    return {
        "event_type": receipt.get("event_type"),
        "gate": receipt.get("gate") or (gate_match.group("gate") + "_gate" if gate_match else None),
        "pr": _pr_of(receipt) or (gate_match.group("pr") if gate_match else None),
        "status": receipt.get("status"),
        "dispatch_id": _dispatch_id(receipt),
        "pos": pos,
    }


def _blocking_resolved(block: Dict[str, Any], evidence: List[Dict[str, Any]]) -> bool:
    later = [e for e in evidence if e["pos"] > block["pos"] and e["pr"] == block["pr"]]
    if block["event_type"] == "pr_merge_refused":
        return any(e["event_type"] == "pr_merged" for e in later)
    if block["event_type"] == "gate_obligation_reopened_stale_evidence":
        return any(e["event_type"] == "review_gate_result" and e["gate"] == block["gate"]
                   for e in later)
    return False


def _decide(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    a, b = record["a"], record["b"]
    if a is None and b is None and record["contract_invalid"] is None:
        return None
    last_outcome_pos = max(record["a_pos"], record["b_pos"], record["contract_invalid_pos"])
    if record["contract_invalid_pos"] > record["a_pos"]:
        ci = record["contract_invalid"]
        return {"decision": "reject", "status": ci.get("status") or "contract_invalid",
                "reason": f"{ci.get('event_type')} after the last lane status"}
    merged = dict(a if a is not None else b)
    if b is not None:
        merged["verification"] = b.get("verification") or {}
        merged["warnings"] = list((a or {}).get("warnings") or []) + list(b.get("warnings") or [])
        if not merged.get("provenance"):
            merged["provenance"] = b.get("provenance")
    verdict = compute_verdict(merged)
    decision, reason = verdict["decision"], verdict["reason"]
    if decision == "accept":
        open_blocks = [blk for blk in record["blocking"]
                       if blk["pos"] > last_outcome_pos
                       and not _blocking_resolved(blk, record["evidence"])]
        if open_blocks:
            decision = "investigate"
            reason = f"{open_blocks[-1]['event_type']} after the last accept"
    return {"decision": decision, "status": merged.get("status"), "reason": reason}


def summarize(
    receipts: Iterable[Dict[str, Any]],
    *,
    project_id: str,
    cutoff: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fold ledger lines (in file order) into one outcome per dispatch.

    ``cutoff`` scopes which dispatches are reported (any attributed receipt at
    or after it) and which lines are counted as bookkeeping/noise. A reported
    dispatch is always decided on its full history.
    """
    def _in_window(receipt: Dict[str, Any]) -> bool:
        ts = _parse_ts(receipt.get("timestamp"))
        return cutoff is None or (ts is not None and ts >= cutoff)

    noise: Counter = Counter()
    bookkeeping: Counter = Counter()
    kept: List[Tuple[int, Dict[str, Any]]] = []
    pr_owner: Dict[str, str] = {}
    for pos, receipt in enumerate(r for r in receipts if isinstance(r, dict)):
        reason = noise_reason(receipt, project_id)
        if reason is None and receipt.get("event_type") in BOOKKEEPING_EVENT_TYPES:
            bookkeeping[str(receipt["event_type"])] += _in_window(receipt)
        elif reason is not None:
            noise[reason] += _in_window(receipt)
        else:
            kept.append((pos, receipt))
            did, pr = _dispatch_id(receipt), _pr_of(receipt)
            if pr and did.lower() not in INVALID_DISPATCH_IDS and not GATE_DISPATCH_RE.match(did):
                pr_owner[pr] = did

    records: Dict[str, Dict[str, Any]] = {}
    unlinked_gate_evidence = 0
    for pos, receipt in kept:
        did, event_type = _dispatch_id(receipt), receipt.get("event_type")
        gate_match = GATE_DISPATCH_RE.match(did)
        is_evidence = bool(gate_match) or event_type in EVIDENCE_EVENT_TYPES
        is_blocking = event_type in BLOCKING_EVENT_TYPES
        missing_id = did.lower() in INVALID_DISPATCH_IDS
        if gate_match or (missing_id and (is_evidence or is_blocking)):
            owner = pr_owner.get(_pr_of(receipt) or (gate_match.group("pr") if gate_match else ""))
            if owner is None:
                if missing_id:
                    noise["missing_dispatch_id"] += _in_window(receipt)
                else:
                    unlinked_gate_evidence += _in_window(receipt)
                continue
            did = owner
        elif missing_id:
            noise["missing_dispatch_id"] += _in_window(receipt)
            continue
        record = records.setdefault(did, _new_record(did))
        record["in_window"] = record["in_window"] or _in_window(receipt)
        if is_evidence or is_blocking:
            record["evidence" if is_evidence else "blocking"].append(_evidence_entry(receipt, pos))
        elif event_type in CONTRACT_INVALID_EVENT_TYPES:
            record["contract_invalid"], record["contract_invalid_pos"] = receipt, pos
        elif _is_writer_b(receipt):
            record["b"], record["b_pos"] = receipt, pos
        elif _is_writer_a(receipt):
            record["a"], record["a_pos"] = receipt, pos
        if not gate_match:
            branch = str(receipt.get("branch") or "")
            for parent in (str(receipt.get("parent_dispatch") or "").strip(),
                           branch[len("dispatch/"):] if branch.startswith("dispatch/") else ""):
                if parent and parent != did:
                    record["children_of"].add(parent)

    superseded_by: Dict[str, str] = {}
    for did, record in records.items():
        for parent in record["children_of"]:
            superseded_by.setdefault(parent, did)

    outcomes: List[Dict[str, Any]] = []
    counts = {"accept": 0, "investigate": 0, "reject": 0, "superseded": 0, "unknown": 0}
    for did, record in records.items():
        if not record["in_window"]:
            continue
        decided = _decide(record) or {"decision": "unknown", "status": None,
                                      "reason": "no outcome receipt, only evidence"}
        if decided["decision"] in ("reject", "investigate") and did in superseded_by:
            decided = {**decided, "decision": "superseded",
                       "reason": f"{decided['decision']} superseded by {superseded_by[did]}"}
        counts[decided["decision"]] += 1
        outcomes.append({
            "dispatch_id": did, **decided,
            "evidence": [{k: v for k, v in e.items() if k != "pos"} for e in record["evidence"]],
            "blocking": [{k: v for k, v in e.items() if k != "pos"} for e in record["blocking"]],
        })
    return {
        "verdict_counts": counts,
        "outcomes": outcomes,
        "bookkeeping_counts": {k: v for k, v in bookkeeping.items() if v},
        "noise_counts": {k: v for k, v in noise.items() if v},
        "unlinked_gate_evidence": unlinked_gate_evidence,
    }
