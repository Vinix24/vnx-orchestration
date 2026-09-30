#!/usr/bin/env python3
"""open_outcomes.py — the dispatches a T0 still has to decide on (fabric-state-herstel D4b).

A dispatch is only finished with an outcome (operator decision 29-09-2026).
``receipt_outcome`` folds a dispatch's receipts into one outcome; a ``reject``
or ``investigate`` is not the end of it but a point a T0 has to look at. This
module lists those points and reads the decisions a T0 recorded about them.

It replaces the byte cursor of ``receipt_query.py pull``: a shared cursor let
the first T0 that pulled read a receipt away from every other T0, and a
receipt that was read was no longer visible anywhere. Here nothing is
consumed. An open outcome stays open for every reader until a T0 records a
decision about that dispatch in ``t0_decision_log.jsonl``.

Decisions
---------
One JSON line per decision, written only through ``t0_decision_log.
write_decision`` (append under an exclusive ``flock``, never a
read-modify-write): ``decision_type: "outcome_decision"``, ``dispatch_id``,
``project_id``, ``decision`` (``accept`` | ``reject``), ``timestamp``. Two
decisions about one dispatch may exist; the last one in file order counts.
An incomplete last line (a writer mid-append) and a malformed line are
skipped, never a crash. A decision of another project never closes an open
outcome of this one (ADR-007: the decision carries ``project_id`` and the
reader filters on it). Other records in the log (``dispatch_created``,
``pr_merge``, ...) are no outcome decision.

Scope
-----
Only dispatches with a receipt at or after ``OPEN_OUTCOMES_EPOCH`` are
listed. Before the per-dispatch reader (``OUTCOME_READER_EPOCH``) an
``investigate`` was a per-line stamp on bookkeeping, not a judgment, so the
backlog before it is not a list of open points. It stays in the ledger,
reachable through ``receipt_query.py by-dispatch``/``since``.

Each item carries ``kind``. Today there is one, ``receipt_outcome``; other
kinds of open point (a dispatch without any outcome, D4b2) are added as a
new ``kind`` next to it, counted in ``by_kind``.

No database is read and no table is added: the per-project ledger and
decision log plus the ``project_id`` filter are the whole scope.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from receipt_outcome import OUTCOME_READER_EPOCH, noise_reason, summarize

LEDGER_NAME = "t0_receipts.ndjson"
DECISION_LOG_NAME = "t0_decision_log.jsonl"

# Dispatches with a receipt at or after this moment are listed; the backlog
# before the per-dispatch reader is left out (see the module docstring).
OPEN_OUTCOMES_EPOCH = OUTCOME_READER_EPOCH

OUTCOME_DECISION_TYPE = "outcome_decision"
OUTCOME_DECISIONS = frozenset({"accept", "reject"})
OPEN_DECISIONS = ("reject", "investigate")
KIND_RECEIPT_OUTCOME = "receipt_outcome"

# The always-loaded t0_index.json carries at most this many items, the rest as a count.
INDEX_LIMIT = 10

_DECISION_ACTION = {"accept": "approve", "reject": "reject"}


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def iter_complete_lines(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield each JSON object on a complete (newline-terminated) line of ``path``.

    A last line without a newline is a writer mid-append and is skipped; a
    malformed or non-object line is skipped too. A missing file yields nothing.
    """
    if not path.exists():
        return
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.endswith(b"\n"):
                return
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict):
                yield obj


def read_outcome_decisions(log_path: Path, project_id: str) -> Dict[str, Dict[str, Any]]:
    """Map dispatch_id -> the last outcome decision of ``project_id`` in file order.

    An empty ``project_id`` reads nothing: a decision is only ever taken as
    one of a named project (ADR-007).
    """
    decisions: Dict[str, Dict[str, Any]] = {}
    if not project_id:
        return decisions
    for record in iter_complete_lines(Path(log_path)):
        if record.get("decision_type") != OUTCOME_DECISION_TYPE:
            continue
        if record.get("project_id") != project_id:
            continue
        did = record.get("dispatch_id")
        if not isinstance(did, str) or not did.strip():
            continue
        if record.get("decision") not in OUTCOME_DECISIONS:
            continue
        decisions[did.strip()] = record
    return decisions


def record_outcome_decision(
    log_path: Path,
    *,
    dispatch_id: str,
    project_id: str,
    decision: str,
    reason: str = "",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one outcome decision through ``t0_decision_log.write_decision``.

    Raises ValueError on an empty dispatch_id or project_id or a decision
    outside ``accept``/``reject``. Returns the record written.
    """
    did = (dispatch_id or "").strip()
    pid = (project_id or "").strip()
    if not did:
        raise ValueError("dispatch_id is required")
    if not pid:
        raise ValueError("project_id is required (ADR-007): a decision is always one project's")
    if decision not in OUTCOME_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(OUTCOME_DECISIONS)}, got {decision!r}")

    # imported here: t0_decision_log resolves its default paths at import time
    from t0_decision_log import build_record, write_decision

    record = build_record(
        action=_DECISION_ACTION[decision],
        reasoning=reason,
        dispatch_id=did,
        timestamp=timestamp,
    )
    record["decision_type"] = OUTCOME_DECISION_TYPE
    record["decision"] = decision
    record["project_id"] = pid
    # settled at write time: nothing for t0_decision_reconcile to resolve
    record["outcome_pending"] = False
    write_decision(record, Path(log_path))
    return record


def _last_seen(receipts: Iterable[Dict[str, Any]], project_id: str) -> Dict[str, datetime]:
    """Latest receipt timestamp per dispatch_id over the lines ``summarize``
    keeps for ``project_id``: ``noise_reason`` is the one project test, so a
    receipt of another project or a noise line never makes an outcome of this
    one look fresh (ADR-007)."""
    seen: Dict[str, datetime] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or noise_reason(receipt, project_id) is not None:
            continue
        did = str(receipt.get("dispatch_id") or "").strip()
        ts = _parse_ts(receipt.get("timestamp"))
        if did and ts is not None:
            ts = ts.astimezone(timezone.utc)
            if did not in seen or ts > seen[did]:
                seen[did] = ts
    return seen


def open_outcome_items(
    receipts: List[Dict[str, Any]],
    decisions: Dict[str, Dict[str, Any]],
    *,
    project_id: str,
    since: str = OPEN_OUTCOMES_EPOCH,
) -> List[Dict[str, Any]]:
    """Every ``reject``/``investigate`` dispatch of ``project_id`` without a
    decision, newest first (by its latest receipt timestamp)."""
    summary = summarize(receipts, project_id=project_id, cutoff=_parse_ts(since))
    last_seen = _last_seen(receipts, project_id)
    items: List[Dict[str, Any]] = []
    for outcome in summary["outcomes"]:
        did = outcome["dispatch_id"]
        if outcome["decision"] not in OPEN_DECISIONS or did in decisions:
            continue
        seen = last_seen.get(did)
        items.append({
            "kind": KIND_RECEIPT_OUTCOME,
            "dispatch_id": did,
            "outcome": outcome["decision"],
            "status": outcome.get("status"),
            "reason": outcome.get("reason"),
            "last_seen": seen.isoformat() if seen else None,
        })
    items.sort(key=lambda i: i["dispatch_id"])
    items.sort(key=lambda i: i["last_seen"] or "", reverse=True)
    return items


def build_open_outcomes(
    state_dir: Path,
    *,
    project_id: str,
    ledger_path: Optional[Path] = None,
    decision_log_path: Optional[Path] = None,
    limit: Optional[int] = INDEX_LIMIT,
    since: str = OPEN_OUTCOMES_EPOCH,
) -> Dict[str, Any]:
    """The open-outcomes section: counts over all open points, at most
    ``limit`` items (None = all), and ``more`` for the rest.

    Stateless: two readers at the same moment get the same answer, and
    reading consumes nothing. Without a project id, or when a file cannot be
    read, the section is ``available: false`` with a reason — never an
    empty list that reads as "nothing open".
    """
    state_dir = Path(state_dir)
    if not project_id:
        return {"available": False, "reason": "no project_id: open outcomes are read per project (ADR-007)"}
    ledger = Path(ledger_path) if ledger_path else state_dir / LEDGER_NAME
    log = Path(decision_log_path) if decision_log_path else state_dir / DECISION_LOG_NAME
    try:
        receipts = list(iter_complete_lines(ledger))
        decisions = read_outcome_decisions(log, project_id)
    except OSError as exc:
        return {"available": False, "reason": f"could not read {ledger.name} or {log.name}: {exc}"}

    items = open_outcome_items(receipts, decisions, project_id=project_id, since=since)
    shown = items if limit is None else items[:max(0, limit)]
    by_outcome = {d: 0 for d in OPEN_DECISIONS}
    by_kind: Dict[str, int] = {}
    for item in items:
        by_outcome[item["outcome"]] += 1
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {
        "available": True,
        "project_id": project_id,
        "since": since,
        "total": len(items),
        "by_outcome": by_outcome,
        "by_kind": by_kind,
        "items": shown,
        "more": len(items) - len(shown),
    }
