"""The append-only proposal ledger (decision 26, open question 9).

One file per project store, ``proposals.ndjson``, one JSON object per line,
never rewritten and never truncated. It holds three events: ``proposed``,
``approved`` and ``verdict``. The day files stay untouched by a verdict; they
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
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from atomic_io import slot_lock

from .proposal_rules import Candidate

LEDGER_NAME = "proposals.ndjson"

#: Decision 26: at most three proposals per project and metric day.
SLOTS_PER_DAY = 3
#: Decision 26: an ``undetermined`` verdict silences its key for fourteen days.
BLOCK_DAYS = 14

PROPOSED = "proposed"
VERDICT = "verdict"
UNDETERMINED = "undetermined"


class LedgerUnreadable(Exception):
    """A ledger line is not a JSON object; the ledger may not be read around."""


def ledger_path(store_root: Path) -> Path:
    """The ledger inside the per-project store directory (decision 13)."""
    return Path(store_root) / LEDGER_NAME


def proposal_id(project_id: str, key: str, metric_day: date, rule_version: str) -> str:
    """Decision 26: sha256 of the four inputs joined by ``|``, first 12 hex chars."""
    payload = "|".join([project_id, key, metric_day.isoformat(), rule_version])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every event in file order; a missing file is empty, a bad line raises."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerUnreadable(f"{path}: line {number} is not JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise LedgerUnreadable(f"{path}: line {number} is not a JSON object")
        events.append(event)
    return events


def open_keys(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Keys with a ``proposed`` event that no ``verdict`` event answers."""
    answered = {event.get("proposal_id") for event in events if event.get("event") == VERDICT}
    return {event["key"] for event in events
            if event.get("event") == PROPOSED
            and isinstance(event.get("key"), str)
            and event.get("proposal_id") not in answered}


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
    }


def _write(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    """Append the events as whole lines and fsync before the caller drops the lock."""
    payload = "".join(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
                      for event in events)
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
