"""The append-only proposal ledger (decision 26, open question 9).

One file per project store, ``proposals.ndjson``, one JSON object per line,
never rewritten and never truncated. It holds ``proposed``, ``approved``,
``expired``, ``rejected``, ``deliverable_created``, ``applied`` and ``verdict`` events. The day files stay untouched by a verdict; they
only ever refer to proposal ids, so yesterday's measurement cannot change
because today's verdict landed.

The retry-safe limit lives here: :func:`append_proposals` reads the existing
``proposed`` events for one project and metric day under the ledger's lock,
takes ``3 - existing`` slots and skips every candidate whose id already
exists, whose key is still open or whose key is blocked by a recent
``undetermined`` verdict. A repeated or half-finished run can therefore never
exceed three proposals and never changes an existing event.

A damaged line is never read around: :class:`LedgerUnreadable` names it, and
proposing stops until a human repairs the file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
from argparse import Namespace
from collections.abc import Iterable, Mapping, Sequence
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from atomic_io import slot_lock
from schema_manifest import is_round_of

from .proposal_rules import Candidate
from .github_fetch import parse_timestamp

LEDGER_NAME = "proposals.ndjson"

#: Decision 26: at most three proposals per project and metric day.
SLOTS_PER_DAY = 3
#: Decision 26: an ``undetermined`` verdict silences its key for fourteen days.
BLOCK_DAYS = 14

PROPOSED = "proposed"
APPROVED = "approved"
EXPIRED = "expired"
VERDICT = "verdict"
UNDETERMINED = "undetermined"
DELIVERABLE_CREATED = "deliverable_created"
REJECTED = "rejected"
APPLIED = "applied"


class LedgerUnreadable(Exception):
    """A ledger line is not a JSON object; the ledger may not be read around."""


class ApprovalRefused(Exception):
    """An approval may not be written; ``reason`` names why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def ledger_path(store_root: Path) -> Path:
    """The ledger inside the per-project store directory (decision 13)."""
    return Path(store_root) / LEDGER_NAME


def proposal_id(project_id: str, key: str, metric_day: date, rule_version: str) -> str:
    """Decision 26: sha256 of the four inputs joined by ``|``, first 12 hex chars."""
    payload = "|".join([project_id, key, metric_day.isoformat(), rule_version])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _validate_deliverable(event: Mapping[str, Any]) -> None:
    """Optional means absent; a present but malformed specification is refused."""
    if event.get("event") == APPLIED:
        stamp = parse_timestamp(event.get("applied_at"))
        if (stamp is None or event.get("application_day") != stamp.date().isoformat()
                or type(event.get("pr_number")) is not int or event["pr_number"] <= 0
                or not isinstance(event.get("merge_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", event["merge_sha"])):
            raise LedgerUnreadable(f"{event.get('proposal_id')}: invalid applied merge evidence")
    if event.get("event") in (DELIVERABLE_CREATED, APPLIED):
        if not isinstance(event.get("deliverable_id"), str) or not re.fullmatch(r"dlv-[0-9a-f]{12}", event["deliverable_id"]):
            raise LedgerUnreadable(f"{event.get('proposal_id')}: invalid deliverable_created id")
    if event.get("event") != PROPOSED or "deliverable" not in event:
        return
    from smart_router import TASK_CLASSES
    spec = event["deliverable"]
    fields = ("track", "title", "output_kind", "task_class")
    if (not isinstance(spec, dict) or any(not isinstance(spec.get(k), str) or not spec[k].strip() for k in fields)
            or spec["output_kind"] not in ("pr", "doc") or spec["task_class"] not in TASK_CLASSES):
        raise LedgerUnreadable(f"{event.get('proposal_id')}: invalid deliverable: require track, title, output_kind (pr/doc), task_class")


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every event in file order; a missing or empty file is empty, a bad line raises.

    A non-empty file whose last record has no line end is a torn write: a later
    append would glue two objects onto one line, so it is refused here and the
    file is left byte-identical.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    if not text:
        return []
    if not text.endswith("\n"):
        last_line = text.count("\n") + 1
        raise LedgerUnreadable(f"{path}: line {last_line} has no line end (torn write)")
    # Split on "\n" only: str.splitlines() also splits on U+0085, U+2028 and
    # U+2029, which _write emits raw inside a string (ensure_ascii=False).
    events: list[dict[str, Any]] = []
    for number, line in enumerate(text.split("\n")[:-1], start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerUnreadable(f"{path}: line {number} is not JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise LedgerUnreadable(f"{path}: line {number} is not a JSON object")
        _validate_deliverable(event)
        events.append(event)
    return events


def open_keys(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Keys with a proposal that has neither a verdict nor an expiry."""
    answered = {(e.get("project_id"), e.get("proposal_id")) for e in events if e.get("event") in (VERDICT, EXPIRED, REJECTED)}
    return {event["key"] for event in events
            if event.get("event") == PROPOSED
            and isinstance(event.get("key"), str)
            and (event.get("project_id"), event.get("proposal_id")) not in answered}


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def blocked_keys(events: Sequence[Mapping[str, Any]], metric_day: date) -> set[str]:
    """Keys silenced by an ``undetermined`` verdict in the fourteen days before ``metric_day``."""
    blocked: set[str] = set()
    for event in events:
        if event.get("event") != VERDICT or event.get("verdict") != UNDETERMINED:
            continue
        key = event.get("key")
        if not isinstance(key, str):
            continue
        verdict_day = _parse_day(event.get("metric_day"))
        if verdict_day is None:
            # No date is no proof the silence expired: keep the key blocked.
            blocked.add(key)
            continue
        if 0 <= (metric_day - verdict_day).days <= BLOCK_DAYS:
            blocked.add(key)
    return blocked


def _event(candidate: Candidate, *, proposal_id_value: str, project_id: str,
           metric_day: date, rule_version: str, recorded_at: str) -> dict[str, Any]:
    return {
        "event": PROPOSED,
        "proposal_id": proposal_id_value,
        "project_id": project_id,
        "key": candidate.key,
        "metric_id": candidate.metric_id,
        "seat": candidate.seat,
        "metric_day": metric_day.isoformat(),
        "rule_version": rule_version,
        "value": candidate.value,
        "baseline": candidate.baseline,
        "baseline_n": candidate.baseline_n,
        "day_n": candidate.day_n,
        "exceedance": candidate.exceedance,
        "recorded_at": recorded_at,
        **({"deliverable": candidate.deliverable} if candidate.deliverable is not None else {}),
    }


def _write(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    """Append the events as whole lines and fsync before the caller drops the lock."""
    for event in events:
        _validate_deliverable(event)
    try:
        payload = "".join(json.dumps(event, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
                          for event in events)
    except ValueError as exc:
        raise LedgerUnreadable(f"{path}: invalid proposal JSON: {exc}") from exc
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _new_events(*, project_id: str, metric_day: date, candidates: Iterable[Candidate],
                rule_version: str, recorded_at: str, slots: int,
                events: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    known_ids = {event.get("proposal_id") for event in events}
    still_open = open_keys(events)
    blocked = blocked_keys(events, metric_day)
    fresh: list[dict[str, Any]] = []
    written: list[str] = []
    for candidate in candidates:
        if len(written) >= slots:
            break
        candidate_id = proposal_id(project_id, candidate.key, metric_day, rule_version)
        if candidate_id in known_ids or candidate.key in still_open or candidate.key in blocked:
            continue
        fresh.append(_event(candidate, proposal_id_value=candidate_id, project_id=project_id,
                            metric_day=metric_day, rule_version=rule_version,
                            recorded_at=recorded_at))
        known_ids.add(candidate_id)
        # One open proposal per key: a duplicate later in this same walk must not pass.
        still_open.add(candidate.key)
        written.append(candidate_id)
    return fresh, written


def append_proposals(path: Path, *, project_id: str, metric_day: date,
                     candidates: Iterable[Candidate], rule_version: str,
                     recorded_at: str) -> list[str]:
    """Write up to ``3 - existing`` proposals; return the ids written, in order.

    The read, the count and the append are one critical section on the ledger's
    lock, and the file is only ever opened for append.
    """
    path = Path(path)
    with slot_lock(path):
        events = read_events(path)
        same_day = sum(1 for event in events
                       if event.get("event") == PROPOSED
                       and event.get("project_id") == project_id
                       and event.get("metric_day") == metric_day.isoformat())
        slots = SLOTS_PER_DAY - same_day
        if slots <= 0:
            return []
        fresh, written = _new_events(project_id=project_id, metric_day=metric_day,
                                     candidates=candidates, rule_version=rule_version,
                                     recorded_at=recorded_at, slots=slots, events=events)
        if fresh:
            _write(path, fresh)
        return written


def _has_event(events: Sequence[Mapping[str, Any]], kind: str, proposal_id_value: Any) -> bool:
    return any(event.get("event") == kind and event.get("proposal_id") == proposal_id_value
               for event in events)


def append_expiries(path: Path, *, now: datetime) -> None:
    """Expire unanswered proposals after seven full days, checking and appending under one lock."""
    if now.utcoffset() is None:
        raise LedgerUnreadable("expiry now must have a timezone")
    with slot_lock(path):
        events = read_events(path)
        for event in events:
            if any(not isinstance(event.get(k), str) or not event[k] for k in ("project_id", "proposal_id")):
                raise LedgerUnreadable(f"{path}: invalid proposal identity")
        closed = {(e.get("project_id"), e.get("proposal_id")) for e in events
                  if e.get("event") in (APPROVED, EXPIRED, VERDICT, REJECTED)}
        fresh = []
        for event in events:
            if event.get("event") not in (PROPOSED, APPROVED, EXPIRED, VERDICT, REJECTED, DELIVERABLE_CREATED, APPLIED):
                raise LedgerUnreadable(f"{path}: unknown proposal event")
            identity = (event.get("project_id"), event.get("proposal_id"))
            if event.get("event") != PROPOSED or identity in closed:
                continue
            stamp = parse_timestamp(event.get("recorded_at"))
            if stamp is None:
                raise LedgerUnreadable(f"{path}: {identity}: invalid recorded_at")
            if now - stamp >= timedelta(days=7):
                fresh.append({"event": EXPIRED, "project_id": identity[0],
                              "proposal_id": identity[1], "recorded_at": now.isoformat()})
                closed.add(identity)
        if fresh:
            _write(path, fresh)


def create_deliverables(path: Path, *, project_id: str, state_dir: Path | None, now: datetime) -> dict[str, str]:
    """Hold the proposal lock through planning and append; deterministic IDs recover a failed append."""
    outcomes = {}
    with slot_lock(path):
        events = [e for e in read_events(path) if e.get("project_id") == project_id]
        approved = {e["proposal_id"] for e in events if e.get("event") == APPROVED}
        closed = {e["proposal_id"] for e in events if e.get("event") in (EXPIRED, REJECTED, DELIVERABLE_CREATED)}
        for event in events:
            pid = event["proposal_id"]
            if event.get("event") != PROPOSED or pid not in approved or pid in closed:
                continue
            if "deliverable" not in event:
                outcomes[pid] = "no deliverable named"
                continue
            scripts = str(Path(__file__).resolve().parents[2])
            if scripts not in sys.path:
                sys.path.append(scripts)
            import planning_cli
            from .store import default_store_dir
            spec = event["deliverable"]
            did = "dlv-" + hashlib.sha256(json.dumps([project_id, pid]).encode()).hexdigest()[:12]
            args = Namespace(state_dir=state_dir or default_store_dir(project_id).parent.parent,
                project_id=project_id, objective=spec["track"], title=spec["title"],
                output_kind=spec["output_kind"], task_class=spec["task_class"],
                proposal_id=pid, dispatch_id=did)
            with redirect_stdout(io.StringIO()):
                result = planning_cli.cmd_deliverable_add(args)
            if result != 0:
                raise LedgerUnreadable(f"{pid}: deliverable creation refused for track {spec['track']!r}")
            _write(path, [{"event": DELIVERABLE_CREATED, "project_id": project_id,
                "proposal_id": pid, "deliverable_id": did, "recorded_at": now.isoformat()}])
            closed.add(pid)
            outcomes[pid] = did
    return outcomes


def append_applications(path: Path, *, project_id: str, state_dir: Path | None, now: datetime) -> None:
    """Link native merge receipts once, holding the proposal lock from read through append."""
    from .store import default_store_dir
    evidence = Path(state_dir or default_store_dir(project_id).parent.parent) / "t0_receipts.ndjson"
    with slot_lock(path):
        events = [e for e in read_events(path) if e.get("project_id") == project_id]
        pending = [e for e in events if e.get("event") == DELIVERABLE_CREATED
                   and not _has_event(events, APPLIED, e["proposal_id"])]
        if not pending:
            return
        try:
            with evidence.open(encoding="utf-8") as handle:
                receipts = []
                for line in handle:
                    if not line.endswith("\n"):
                        raise ValueError("torn receipt")
                    receipts.append(json.loads(line))
            if any(not isinstance(r, dict) for r in receipts):
                raise ValueError("receipt is not an object")
            for event in pending:
                pid, did = event["proposal_id"], event["deliverable_id"]
                if _has_event(events, APPLIED, pid):
                    continue
                if not _has_event(events, APPROVED, pid):
                    raise ValueError(f"{pid}: not approved")
                matches = [r for r in receipts if r.get("project_id") == project_id
                           and (r.get("dispatch_id") == did or is_round_of(did, r.get("dispatch_id")))
                           and r.get("event_type") == "pr_merged"]
                applications = []
                for receipt in matches:
                    stamp = parse_timestamp(receipt.get("timestamp"))
                    if receipt.get("conclusion") != "merged" or stamp is None or stamp > now:
                        raise ValueError(f"{did}: invalid merge conclusion/timestamp")
                    applied = dict(event, event=APPLIED, pr_number=receipt.get("pr_number"),
                        merge_sha=receipt.get("merge_sha"), applied_at=stamp.isoformat(),
                        application_day=stamp.date().isoformat(), recorded_at=now.isoformat())
                    _validate_deliverable(applied)
                    applications.append(applied)
                if len({(a["pr_number"], a["merge_sha"], a["application_day"]) for a in applications}) > 1:
                    raise ValueError(f"{did}: conflicting merge receipts")
                if applications:
                    _write(path, applications[:1])
                    events.extend(applications[:1])
        except (OSError, ValueError) as exc:
            raise LedgerUnreadable(f"merge evidence {evidence}: {exc}") from exc


def approval_refusal(events: Sequence[Mapping[str, Any]], proposal_id_value: Any) -> str | None:
    """The reason an approval is refused, or ``None`` when the proposal is open.

    Anything but a string id is unknown: a missing id never matches a proposal.
    """
    if not isinstance(proposal_id_value, str) or not _has_event(events, PROPOSED, proposal_id_value):
        return "unknown_proposal"
    project = next(e.get("project_id") for e in events
                   if e.get("event") == PROPOSED and e.get("proposal_id") == proposal_id_value)
    if _has_event([e for e in events if e.get("project_id") == project], EXPIRED, proposal_id_value):
        return "expired"
    if _has_event([e for e in events if e.get("project_id") == project], REJECTED, proposal_id_value):
        return "rejected"
    if _has_event(events, VERDICT, proposal_id_value):
        return "already_judged"
    if _has_event(events, APPROVED, proposal_id_value):
        return "already_approved"
    return None


def append_approval(path: Path, event: Mapping[str, Any]) -> None:
    """Append one ``approved`` event, or raise :class:`ApprovalRefused` and write nothing.

    The proposal checks are read again under the ledger's lock, so two approvals
    racing for the same id cannot both land.
    """
    path = Path(path)
    with slot_lock(path):
        reason = approval_refusal(read_events(path), event.get("proposal_id"))
        if reason is not None:
            raise ApprovalRefused(reason)
        _write(path, [event])


def append_verdict(path: Path, event: Mapping[str, Any]) -> None:
    """Append one ``verdict`` event, or raise :class:`ApprovalRefused` and write nothing.

    Only an approved, applied proposal is judged, and only once. The approval is read
    again under the ledger's lock; anything but a string id never matches one.
    """
    path = Path(path)
    with slot_lock(path):
        events = [e for e in read_events(path) if e.get("project_id") == event.get("project_id")]
        proposal_id_value = event.get("proposal_id")
        if not isinstance(proposal_id_value, str) or not _has_event(
                events, APPROVED, proposal_id_value):
            raise ApprovalRefused("not_approved")
        if _has_event(events, VERDICT, proposal_id_value):
            raise ApprovalRefused("already_judged")
        if not _has_event(events, APPLIED, proposal_id_value):
            raise ApprovalRefused("not_applied")
        _write(path, [event])
