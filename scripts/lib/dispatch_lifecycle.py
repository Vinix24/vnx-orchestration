#!/usr/bin/env python3
"""ADR-045 (audit-spoor-kern K-3c): the finalizer and the recovery of one end.

``finalize`` owns steps 1 to 5 of the plan's "Einde en herstel", ``recover``
steps 2 to 5 for an intent that never published, and ``sweep`` (K-6) runs both
unattended: every open intent, and ``abandoned`` for a ``v3`` attempt past its
deadline plus the grace without liveness (ADR-045 §5/§6). K-act turns it on.

What the line SAYS is not decided here. The K-3b contract in
``append_receipt_internals.lifecycle`` owns the canonical form, the intent
layout and the accept rule; this module owns the ORDER in which a line is
frozen, appended, read back from the ledger, projected and published (ADR-005,
ledger first), and the one
thing the ADR leaves to the finalizer: the attempt check under
``BEGIN IMMEDIATE`` (OI-2443). The intent lives beside the LEDGER, because the
accept rule resolves it from the ledger it appends to.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from append_receipt_internals.common import (
    EXIT_IO_ERROR,
    EXIT_LOCK_ERROR,
    EXIT_VALIDATION_ERROR,
    AppendReceiptError,
)
from append_receipt_internals.lifecycle import (
    END_INTENT_DIRNAME,
    EndIntentMalformed,
    build_end_intent,
    end_intent_path,
    end_line_matches_intent,
    lifecycle_enabled,
    load_end_intent,
    prepare_end_line,
    write_end_intent,
)
from atomic_io import atomic_write_json
from coordination_db import TERMINAL_DISPATCH_STATES, db_path_from_state_dir, get_connection
from dispatch_spec import DEADLINE_SECONDS_MAX, DEADLINE_SECONDS_MIN
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE
from runtime_state_machine import transition_dispatch_idempotent

logger = logging.getLogger(__name__)

#: The ledger of one store; the intent directory sits beside it (ADR-045 §3).
LEDGER_FILENAME = "t0_receipts.ndjson"

STATUS_FINALIZED = "finalized"
STATUS_RECOVERED = "recovered"
STATUS_ALREADY_PUBLISHED = "already_published"
STATUS_INTENT_LOST = "intent_lost"
STATUS_NO_INTENT = "no_intent"

LINE_ALREADY_IN_LEDGER = "already_in_ledger"

PROJECTION_APPLIED = "applied"
PROJECTION_NOOP = "noop"
PROJECTION_CONFLICT = "conflict"
PROJECTION_STALE_ATTEMPT = "stale_attempt"
PROJECTION_NO_ROW = "no_row"
PROJECTION_NO_STORE = "no_store"

#: ADR-045 §4 "Eindtoestand naar projectie": the DB target of each end_state.
#: ``abandoned`` is a ledger fact and a timed-out row plus a metadata marker.
END_STATE_TARGET: Dict[str, str] = {
    "completed": "completed",
    "failed": "failed_delivery",
    "timed_out": "timed_out",
    "abandoned": "timed_out",
}

#: The hop sequences of that same table, literally. A start state the table
#: does not list for a target has no path, and a row without a path is a
#: projection conflict — never an invented route.
PROJECTION_ROUTES: Dict[str, Dict[str, tuple]] = {
    "completed": {
        "running": ("completed",),
        "accepted": ("running", "completed"),
        "delivering": ("accepted", "running", "completed"),
    },
    "failed_delivery": {
        "delivering": ("failed_delivery",),
        "running": ("failed_delivery",),
        "claimed": ("delivering", "failed_delivery"),
        "accepted": ("running", "failed_delivery"),
    },
    "timed_out": {
        "delivering": ("timed_out",),
        "accepted": ("timed_out",),
        "running": ("timed_out",),
        "claimed": ("delivering", "timed_out"),
    },
}


@dataclass(frozen=True)
class LifecycleResult:
    """What one call did, in terms a caller (K-5, K-6) can log and count."""

    status: str
    intent_path: Path
    end_state: Optional[str] = None
    line_status: Optional[str] = None
    projection: Dict[str, Any] = field(default_factory=dict)
    published_at: Optional[str] = None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_for(state_dir: str | Path, ledger_path: Optional[str | Path]) -> Path:
    """The ledger this end belongs to — and the store the intent dir lives in.

    A ``ledger_path`` outside ``state_dir`` is refused: the accept rule looks
    the intent up beside the ledger it appends to, so freezing it under another
    store would freeze a decision the append can never find.
    """
    ledger = (Path(ledger_path) if ledger_path is not None
              else Path(state_dir) / LEDGER_FILENAME).expanduser().resolve()
    if ledger.parent != Path(state_dir).expanduser().resolve():
        raise AppendReceiptError(
            "ledger_outside_state", EXIT_VALIDATION_ERROR,
            f"ledger {ledger} is not in the state dir {state_dir}; the end intent is "
            "resolved from the ledger's directory (ADR-045 §3), so it must be one store.")
    return ledger


def _ledger_size(ledger: Path) -> int:
    """The intent's byte offset: what sits before it is not this attempt's business."""
    try:
        return ledger.stat().st_size
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise AppendReceiptError(
            "ledger_unreadable", EXIT_IO_ERROR, f"cannot stat the ledger {ledger}: {exc}") from exc


def _end_lines_from_offset(ledger: Path, document: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every parseable end record for this intent's key at or after its offset.

    A torn line (a crash mid-append) is not evidence that a complete end line
    exists, so it is skipped and the append layer decides (ADR-045 §3).
    """
    offset = int(document["ledger_offset"])
    try:
        if ledger.stat().st_size <= offset:
            return []
        with ledger.open("rb") as handle:
            handle.seek(offset)
            blob = handle.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AppendReceiptError(
            "ledger_unreadable", EXIT_IO_ERROR, f"cannot read the ledger {ledger}: {exc}") from exc
    key = (document["project_id"], document["dispatch_id"], document["attempt"])
    found: List[Dict[str, Any]] = []
    for raw in blob.split(b"\n"):
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (isinstance(line, dict)
                and str(line.get("event_type") or "").strip() == DISPATCH_FINISHED_EVENT_TYPE
                and (line.get("project_id"), line.get("dispatch_id"), line.get("attempt")) == key):
            found.append(line)
    return found


def _find_end_line(ledger: Path, document: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The first parseable end line for this intent's key after its offset, if any."""
    found = _end_lines_from_offset(ledger, document)
    return found[0] if found else None


def _append_end_line(document: Mapping[str, Any], *,
                     lock_deadline: Optional[float] = None) -> str:
    """Step 3: append the frozen line unless the ledger already carries it.

    ``lock_deadline`` (a ``time.monotonic()`` value) is the caller's bound for
    the whole operation: the append wait gets what is left of it, so a caller
    that bounds ``recover`` never waits longer on the append lock than its own
    ``lock_timeout``.
    """
    from append_receipt_internals.payload import append_receipt_payload

    ledger = Path(document["ledger_path"])
    if _find_end_line(ledger, document) is not None:
        return LINE_ALREADY_IN_LEDGER
    remaining = (None if lock_deadline is None
                 else max(0.0, lock_deadline - time.monotonic()))
    try:
        return append_receipt_payload(
            json.loads(document["line"]), receipts_file=str(ledger), skip_enrichment=True,
            lock_timeout=remaining,
        ).status
    except AppendReceiptError as exc:
        # Only another writer of this same attempt can have produced a second
        # end line for the key. Continue only when the ledger proves the line
        # landed; anything else stays refused.
        if exc.code != "duplicate_lifecycle_end" or _find_end_line(ledger, document) is None:
            raise
        return LINE_ALREADY_IN_LEDGER


def _verify_the_end_line(ledger: Path, document: Mapping[str, Any]) -> None:
    """Step 3b: the ledger, not the append's return, proves the record landed.

    The append's ``appended`` status only says what the append layer believes
    it wrote. A torn tail used to swallow the new line, so ``appended`` was
    reported for a record the ledger does not carry; the row was then completed
    and the intent published, and recovery skips a published intent — the loss
    became permanent. One parseable record for this ``(project_id,
    dispatch_id, attempt)`` that equals the frozen intent is required before
    anything is projected or published; anything else is a named refusal.
    """
    found = _end_lines_from_offset(ledger, document)
    key = (document["project_id"], document["dispatch_id"], document["attempt"])
    if len(found) != 1:
        raise AppendReceiptError(
            "end_line_not_verified", EXIT_VALIDATION_ERROR,
            f"the ledger {ledger} carries {len(found)} parseable "
            f"{DISPATCH_FINISHED_EVENT_TYPE} records for (project_id={key[0]!r}, "
            f"dispatch_id={key[1]!r}, attempt={key[2]!r}) after the append, not exactly "
            "one; nothing was projected or published")
    if not end_line_matches_intent(found[0], document):
        raise AppendReceiptError(
            "end_line_not_verified", EXIT_VALIDATION_ERROR,
            f"the ledger record for (project_id={key[0]!r}, dispatch_id={key[1]!r}, "
            f"attempt={key[2]!r}) does not equal the frozen intent; nothing was "
            "projected or published")


def _stamp_abandoned(conn: Any, row: Any, document: Mapping[str, Any]) -> str:
    """Mark ``metadata_json.end_state`` on the row an ``abandoned`` end projected.

    ADR-045 §4: no new DB state — the terminal set stays one set (ADR-049) and
    this marker tells ``abandoned`` apart from ``timed_out``. Merged into the
    existing metadata, never replacing another writer's keys; a ``metadata_json``
    this cannot parse is refused rather than overwritten.
    """
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise AppendReceiptError(
            "projection_metadata_unreadable", EXIT_VALIDATION_ERROR,
            f"cannot merge end_state into dispatch {document['dispatch_id']!r} of "
            f"{document['project_id']!r}: metadata_json is not JSON ({exc})") from exc
    if not isinstance(metadata, dict):
        raise AppendReceiptError(
            "projection_metadata_unreadable", EXIT_VALIDATION_ERROR,
            f"metadata_json of dispatch {document['dispatch_id']!r} is not an object")
    if metadata.get("end_state") == "abandoned":
        return "already_marked"
    metadata["end_state"] = "abandoned"
    conn.execute(
        "UPDATE dispatches SET metadata_json = ?, updated_at = ? "
        "WHERE dispatch_id = ? AND project_id = ?",
        (json.dumps(metadata, sort_keys=True), _now_utc(),
         document["dispatch_id"], document["project_id"]),
    )
    return "marked"


def _apply_end_state(conn: Any, row: Any, document: Mapping[str, Any], *,
                     actor: str, reason: Optional[str]) -> Dict[str, Any]:
    """Step 4 inside the transaction: one end_state onto one row."""
    end_state = str(document["end_state"])
    target = END_STATE_TARGET[end_state]
    from_state = str(row["state"])
    result: Dict[str, Any] = {"action": PROJECTION_NOOP, "end_state": end_state,
                              "from_state": from_state, "to_state": target}
    if from_state != target:
        route = PROJECTION_ROUTES[target].get(from_state)
        if route is None:
            result["action"] = PROJECTION_CONFLICT
            result["reason"] = ("other_terminal_state" if from_state in TERMINAL_DISPATCH_STATES
                                else "no_path")
            return result
        for hop in route:
            transition_dispatch_idempotent(
                conn, dispatch_id=str(document["dispatch_id"]), to_state=hop, actor=actor,
                reason=reason or f"ADR-045 end projection of {end_state}",
                metadata={"end_state": end_state, "attempt": document["attempt"],
                          "project_id": document["project_id"]},
                project_id=str(document["project_id"]),
            )
        result["action"] = PROJECTION_APPLIED
    if end_state == "abandoned":
        result["metadata"] = _stamp_abandoned(conn, row, document)
    return result


def _project_in_transaction(conn: Any, document: Mapping[str, Any], *,
                            actor: str, reason: Optional[str]) -> Dict[str, Any]:
    """Step 4's read-and-compare: the row, its attempt, and then the table."""
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
        (document["dispatch_id"], document["project_id"]),
    ).fetchone()
    if row is None:
        return {"action": PROJECTION_NO_ROW, "project_id": document["project_id"],
                "dispatch_id": document["dispatch_id"]}
    row_attempt = row["attempt_count"]
    if isinstance(row_attempt, bool) or row_attempt != document["attempt"]:
        # Fail closed: an unknown or different attempt is never "this attempt",
        # so a late attempt 1 cannot end attempt 2 (OI-2443).
        return {"action": PROJECTION_STALE_ATTEMPT, "row_attempt_count": row_attempt,
                "attempt": document["attempt"]}
    return _apply_end_state(conn, row, document, actor=actor, reason=reason)


def _project(state_dir: Path, document: Mapping[str, Any], *,
             actor: str, reason: Optional[str]) -> Dict[str, Any]:
    """Step 4: the projection, in one ``BEGIN IMMEDIATE`` transaction.

    The write lock goes on before the row is read, so the ``attempt_count``
    compare and the transitions it guards cannot be split by another writer.
    Nothing is committed for a conflict, a foreign attempt or a missing row; a
    refused transition rolls the step back and leaves the intent unpublished
    for ``recover`` to retry.
    """
    if not Path(db_path_from_state_dir(state_dir)).is_file():
        return {"action": PROJECTION_NO_STORE}
    with get_connection(state_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _project_in_transaction(conn, document, actor=actor, reason=reason)
        except BaseException:
            conn.rollback()
            raise
        if result["action"] in (PROJECTION_APPLIED, PROJECTION_NOOP):
            conn.commit()
        else:
            conn.rollback()
    return result


@contextmanager
def _publication_lock(lock_path: Path, lock_deadline: Optional[float]):
    """An exclusive flock held through step 5 (ADR-045 §3).

    ``lock_deadline`` is a ``time.monotonic()`` value or None. Without it the
    wait is unbounded, as a lane's finalizer may be; at the deadline the wait
    ends with ``lock_timeout`` and nothing published, so the next tick retries.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            flags = fcntl.LOCK_EX if lock_deadline is None else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
                break
            except BlockingIOError:
                if time.monotonic() >= lock_deadline:
                    raise AppendReceiptError(
                        "lock_timeout", EXIT_LOCK_ERROR,
                        f"publication lock {lock_path} was still held at the deadline; "
                        "nothing was published") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publish_intent(intent_path: Path, document: Mapping[str, Any]) -> str:
    """Step 5: ``published_at`` into the intent, temp file plus ``rename``."""
    published_at = _now_utc()
    updated = dict(document, published_at=published_at)
    text = json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n"
    fd, name = tempfile.mkstemp(prefix=".publish-", suffix=".tmp", dir=str(intent_path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, intent_path)
    except OSError as exc:
        raise AppendReceiptError(
            "intent_publish_failed", EXIT_IO_ERROR,
            f"cannot publish {intent_path}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return published_at


def _reload_intent(intent_path: Path) -> Optional[Dict[str, Any]]:
    """Read an intent, turning a malformed one into a named refusal.

    ``load_end_intent`` already refuses an unreadable file with
    ``invalid_end_intent``; a file that reads but is not an intent arrives as a
    bare ``EndIntentMalformed`` (a ``ValueError``). Both must reach the caller as
    the same named refusal, from ``finalize`` and from ``recover`` alike.
    """
    try:
        return load_end_intent(intent_path)
    except EndIntentMalformed as exc:
        raise AppendReceiptError(
            "invalid_end_intent", EXIT_VALIDATION_ERROR,
            f"the intent at {intent_path} is malformed: {exc}") from exc


def _run_steps_2_to_5(*, intent_path: Path, state_dir: Path, actor: str,
                      reason: Optional[str], lock_timeout: Optional[float],
                      status: str) -> LifecycleResult:
    """The locked middle: re-read the intent, ledger, projection, publication."""
    lock_deadline = None if lock_timeout is None else time.monotonic() + max(0.0, lock_timeout)
    with _publication_lock(intent_path.with_suffix(".lock"), lock_deadline):
        document = _reload_intent(intent_path)  # step 2, re-read under the lock
        if document is None:
            return LifecycleResult(status=STATUS_NO_INTENT, intent_path=intent_path)
        published_at = document.get("published_at")
        if published_at:
            return LifecycleResult(status=STATUS_ALREADY_PUBLISHED, intent_path=intent_path,
                                   end_state=document["end_state"], published_at=published_at)
        line_status = _append_end_line(document, lock_deadline=lock_deadline)   # step 3
        _verify_the_end_line(Path(document["ledger_path"]), document)          # step 3b
        projection = _project(state_dir, document, actor=actor, reason=reason)  # step 4
        published_at = _publish_intent(intent_path, document)                  # step 5
        return LifecycleResult(status=status, intent_path=intent_path,
                               end_state=document["end_state"], line_status=line_status,
                               projection=projection, published_at=published_at)


def finalize(
    *,
    state_dir: str | Path,
    project_id: str,
    dispatch_id: str,
    attempt: int,
    end_state: str,
    end_line: Mapping[str, Any],
    ledger_path: Optional[str | Path] = None,
    actor: str = "lifecycle",
    reason: Optional[str] = None,
    lock_timeout: Optional[float] = None,
) -> LifecycleResult:
    """End one attempt of one dispatch: freeze, append, project, publish.

    ``attempt`` is the attempt the caller took at hand-over
    (``dispatches.attempt_count``, ADR-045 §3); it is part of the key, so a late
    finalize of attempt 1 can neither write attempt 2's line nor project onto
    its row. The end line is ``dispatch_finished`` with ``end_state`` from
    {``completed``, ``failed``, ``timed_out``, ``abandoned``}.

    The key is validated before any path is built and the line is validated and
    canonicalised before the intent exists (K-3 #2328 r1): a refused call
    leaves nothing on disk. The loser of the exclusive freeze writes nothing and
    reports ``intent_lost`` (``already_published`` when the winner published).
    """
    ledger = _ledger_for(state_dir, ledger_path)
    intent_path = end_intent_path(Path(state_dir), project_id, dispatch_id, attempt)
    prepared = prepare_end_line(end_line, ledger_path=ledger)
    document = build_end_intent(
        project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
        end_state=end_state, end_line=prepared, ledger_path=ledger,
        ledger_offset=_ledger_size(ledger),
    )
    if not write_end_intent(intent_path, document):
        winner = _reload_intent(intent_path)
        if winner is None:
            raise AppendReceiptError(
                "missing_end_intent", EXIT_IO_ERROR,
                f"{intent_path} exists but cannot be read as an intent")
        published_at = winner.get("published_at")
        return LifecycleResult(
            status=STATUS_ALREADY_PUBLISHED if published_at else STATUS_INTENT_LOST,
            intent_path=intent_path, end_state=winner["end_state"], published_at=published_at,
        )
    return _run_steps_2_to_5(
        intent_path=intent_path, state_dir=Path(state_dir), actor=actor,
        reason=reason, lock_timeout=lock_timeout, status=STATUS_FINALIZED,
    )


def recover(
    *,
    state_dir: str | Path,
    project_id: str,
    dispatch_id: str,
    attempt: int,
    actor: str = "sweep",
    reason: Optional[str] = None,
    lock_timeout: Optional[float] = None,
) -> LifecycleResult:
    """Finish an intent that never published: steps 2 to 5 under the same lock.

    The end line may already be in the ledger (the append happens before the
    projection): nothing is appended then, but step 4 is applied again
    regardless — that is what completes a crash between them and what re-stamps
    the ``abandoned`` marker. ``no_intent`` means there was nothing to recover.
    """
    intent_path = end_intent_path(Path(state_dir), project_id, dispatch_id, attempt)
    return _run_steps_2_to_5(
        intent_path=intent_path, state_dir=Path(state_dir), actor=actor,
        reason=reason, lock_timeout=lock_timeout, status=STATUS_RECOVERED,
    )



# K-6: the sweep — recover open intents, abandon a dead attempt, heartbeat

#: ADR-045 §6: deadline plus this grace, no margin added and none subtracted (15-10).
ABANDON_GRACE_SECONDS = 30 * 60
#: No readable deadline: the largest legal one, so an unknown deadline never abandons
#: early; named in the heartbeat and the report rather than left implicit.
FALLBACK_DEADLINE_SECONDS = DEADLINE_SECONDS_MAX
SWEEP_HEARTBEAT_FILENAME = "sweep_heartbeat.json"
SWEEP_LOCK_TIMEOUT_SECONDS = 5.0  # a taken publication lock must not stall the tick
REASON_SWEEP_ABANDONED = "deadline_passed_no_liveness"
NO_PUSH_ABANDONED = f"no_push:{REASON_SWEEP_ABANDONED}"
MODE_V3 = "v3"
SPEC_DIRS = ("pending", "completed", "failed")  # where a staged bundle may sit
LIVENESS_LIVE, LIVENESS_DEAD, LIVENESS_UNKNOWN = "live", "dead", "unknown"
_LEDGER_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_THE_COUNTS = ("recovered", "abandoned", "live", "unknown_liveness", "lifecycle_off_in_flight", "in_flight")


class InFlightAttempt(NamedTuple):
    """One ``v3`` start line without its end line."""
    project_id: str
    dispatch_id: str
    attempt: int
    start_line: Dict[str, Any]


@dataclass(frozen=True)
class SweepResult:
    """One run, in the counts the heartbeat and the count command read back."""
    recovered: int = 0
    abandoned: int = 0
    live: int = 0
    unknown_liveness: int = 0
    lifecycle_off: bool = False
    lifecycle_off_in_flight: int = 0
    in_flight: int = 0
    errors: Tuple[str, ...] = ()
    heartbeat_path: str = ""
    # K-6b: a bounded run reports whether it stopped early and how much it left.
    budget_exhausted: bool = False
    budget_left: int = 0


def v3_in_flight(state_dir: str | Path, project_id: Optional[str] = None
                 ) -> Tuple[Tuple[InFlightAttempt, ...], Tuple[str, ...]]:
    """The ``v3`` attempts with a start line and no end line, plus unkeyable starts.

    An attempt ends only on its own lifecycle end: a ``dispatch_finished`` whose
    ``project_id``, ``dispatch_id`` and ``attempt`` are exactly its key. A legacy
    ``task_complete`` (also one refusing delivery), another completion type or a
    line without an ``attempt`` is no end for it (ADR-045 §2).
    """
    ledger = Path(state_dir) / LEDGER_FILENAME
    starts: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    finished: set = set()
    errors: List[str] = []
    try:
        handle = ledger.open("rb")
    except FileNotFoundError:
        return (), ()
    except OSError as exc:
        raise AppendReceiptError(
            "ledger_unreadable", EXIT_IO_ERROR, f"cannot read {ledger}: {exc}") from exc
    with handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError): continue  # a torn line is no record
            if not isinstance(line, dict):
                continue
            key = (line.get("project_id"), line.get("dispatch_id"))
            attempt = line.get("attempt")
            keyed = (all(isinstance(part, str) and part.strip() for part in key)
                     and isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1)
            kind = str(line.get("event_type") or "").strip()
            if kind == DISPATCH_FINISHED_EVENT_TYPE:
                if keyed: finished.add((key[0], key[1], attempt))
            elif kind == DISPATCH_STARTED_EVENT_TYPE and \
                    str(line.get("lifecycle_mode") or "").strip() == MODE_V3:
                if not keyed:
                    errors.append(f"unkeyable dispatch_started: {key!r} attempt={attempt!r}")
                elif (key[0], key[1], attempt) not in starts:
                    starts[(key[0], key[1], attempt)] = line
    return tuple(
        InFlightAttempt(pid, did, attempt, start)
        for (pid, did, attempt), start in starts.items()
        if (project_id is None or pid == project_id) and (pid, did, attempt) not in finished
    ), tuple(errors)


def open_intents(state_dir: str | Path, project_id: Optional[str] = None
                 ) -> Tuple[Tuple[Tuple[str, str, int, Path], ...], Tuple[str, ...]]:
    """The readable intents without ``published_at``, plus the unreadable ones."""
    root = Path(state_dir) / END_INTENT_DIRNAME
    places = [root / project_id] if project_id is not None else sorted(root.glob("*/"))
    intents: List[Tuple[str, str, int, Path]] = []
    errors: List[str] = []
    for path in (found for place in places for found in sorted(place.glob("*.json"))
                 if not found.name.endswith(".spawn.json")):
        try:
            document = load_end_intent(path)
        except (AppendReceiptError, EndIntentMalformed) as exc:
            errors.append(f"unreadable intent {path}: {exc}")
            continue
        if document is not None and not document.get("published_at"):
            intents.append((document["project_id"], document["dispatch_id"],
                            document["attempt"], path))
    return tuple(intents), tuple(errors)


def in_flight_counts(state_dir: str | Path) -> Dict[str, Any]:
    """The rollback counts of one store, read-only (the plan's "Code terug" gate)."""
    intents, intent_errors = open_intents(state_dir)
    flights, flight_errors = v3_in_flight(state_dir)
    off = not lifecycle_enabled()
    unreadable = list(intent_errors) + list(flight_errors)
    try: _dispatch_row(Path(state_dir), "", "")  # no ids: no row wanted, only whether the store answers
    except AppendReceiptError as exc: unreadable.append(str(exc))
    return {"v3_in_flight": len(flights), "open_intents": len(intents), "lifecycle_off": off,
            "v3_in_flight_lifecycle_off": len(flights) if off else 0, "unreadable": unreadable}


def _dispatch_row(state_dir: Path, project_id: str, dispatch_id: str) -> Optional[Dict[str, Any]]:
    """The attempt's row: ``None`` for no store and no row, raised when the store cannot be read."""
    if not Path(db_path_from_state_dir(state_dir)).is_file(): return None
    try:
        with get_connection(state_dir) as conn:
            row = conn.execute("SELECT bundle_path, metadata_json FROM dispatches "
                               "WHERE dispatch_id = ? AND project_id = ?",
                               (dispatch_id, project_id)).fetchone()
    except Exception as exc:  # vnx-silent-except: raised to the caller as the store's refusal, never "no row"
        raise AppendReceiptError("store_unreadable", EXIT_IO_ERROR,
                                 f"cannot read {db_path_from_state_dir(state_dir)}: {exc}") from exc
    return dict(row) if row is not None else None


def _valid_deadline(value: Any) -> bool:
    """A legal ``deadline_seconds``: a plain int inside the spec's own bounds."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and DEADLINE_SECONDS_MIN <= value <= DEADLINE_SECONDS_MAX)


def deadline_for(state_dir: str | Path, start_line: Mapping[str, Any],
                 row: Optional[Mapping[str, Any]]) -> Tuple[int, str]:
    """``(deadline_seconds, source)``: start line, row bundle, staged spec, else the constant.

    The start line's own field is read first (K-6b, OI-2522): the door stages
    under ``pending/<staging_id>/``, which the staged-spec candidates cannot see
    when the staging id differs from the dispatch id, so a valid 300-second spec
    fell back to 14400. A staged spec only counts when its bytes are the start
    line's ``spec_sha256``.
    """
    own = start_line.get("deadline_seconds")
    if _valid_deadline(own):
        return own, "start_line"
    sha = start_line.get("spec_sha256")
    sha = sha.strip() if isinstance(sha, str) else None
    did = str(start_line.get("dispatch_id") or "")
    bundle = row.get("bundle_path") if row is not None else None
    candidates = ([(Path(bundle.strip()) / "dispatch-spec.json", "row:bundle_path")]
                  if isinstance(bundle, str) and bundle.strip() else [])
    candidates += [(Path(state_dir).parent / "dispatches" / name / did / "dispatch-spec.json",
                    "spec") for name in SPEC_DIRS]
    for path, kind in candidates:
        try:
            blob = path.read_bytes()
            value = json.loads(blob).get("deadline_seconds")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
        if (not sha or hashlib.sha256(blob).hexdigest() != sha
                or not _valid_deadline(value)):
            continue
        return value, f"{kind}:{path}"
    return FALLBACK_DEADLINE_SECONDS, "fallback:FALLBACK_DEADLINE_SECONDS"


def _pid_state(pid: Any, started_at: Any) -> str:
    """Live only when *pid* is alive with the recorded kernel start time."""
    from dispatch_start import process_started_at  # noqa: PLC0415

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0: return LIVENESS_DEAD
    if not isinstance(started_at, str) or not started_at.strip(): return LIVENESS_UNKNOWN
    try:
        actual = process_started_at(pid)
    except Exception:  # vnx-silent-except: an unmeasurable pid is unknown, and unknown never abandons
        return LIVENESS_UNKNOWN
    if actual is not None:
        return LIVENESS_LIVE if actual == started_at else LIVENESS_DEAD
    try:  # no start time, but the pid answers: alive and not ours to read
        os.kill(pid, 0)
    except ProcessLookupError:
        return LIVENESS_DEAD
    except OSError:
        pass
    return LIVENESS_UNKNOWN


def liveness(state_dir: str | Path, attempt: InFlightAttempt) -> Tuple[str, Optional[str]]:
    """ADR-045 §5: the flock, the door pid or the child pid; unknown beats dead."""
    from dispatch_start import spawn_file_path  # noqa: PLC0415
    from dispatch_worktree_isolation import claims_dir_for_state_dir, probe_occupancy  # noqa: PLC0415

    verdicts: List[str] = []
    try:
        held = probe_occupancy(claims_dir_for_state_dir(Path(state_dir)), attempt.dispatch_id)
        verdicts.append({"held": LIVENESS_LIVE, "released": LIVENESS_DEAD,
                         "absent": LIVENESS_DEAD}.get(held, LIVENESS_UNKNOWN))
    except Exception:  # vnx-silent-except: an unmeasurable lock is unknown, and unknown never abandons
        verdicts.append(LIVENESS_UNKNOWN)
    verdicts.append(_pid_state(attempt.start_line.get("door_pid"),
                               attempt.start_line.get("door_started_at")))
    try:
        spawn = json.loads(spawn_file_path(
            Path(state_dir), attempt.project_id, attempt.dispatch_id, attempt.attempt
        ).read_text(encoding="utf-8"))
        verdicts.append(_pid_state(spawn.get("child_pid"), spawn.get("child_started_at")))
    except FileNotFoundError:
        verdicts.append(LIVENESS_DEAD)  # no spawn file: this attempt recorded no child
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AppendReceiptError):
        verdicts.append(LIVENESS_UNKNOWN)
    if LIVENESS_LIVE in verdicts:
        return LIVENESS_LIVE, ("occupancy_flock", "door_pid", "child_pid")[
            verdicts.index(LIVENESS_LIVE)]
    return (LIVENESS_UNKNOWN if LIVENESS_UNKNOWN in verdicts else LIVENESS_DEAD), None


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None: return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.strptime(str(value).strip(), _LEDGER_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"now={value!r} is not a datetime or {_LEDGER_TS_FORMAT}") from exc


def _stamp_lifecycle_mode(state_dir: Path, row: Optional[Mapping[str, Any]],
                          attempt: InFlightAttempt) -> None:
    """ADR-045 §6: the start line is the source; the sweep writes the row's mode.

    One statement: SQLite merges the field under whatever ``metadata_json`` holds
    when it runs, so a key committed after this tick read the row survives; a row
    whose metadata is not an object, or is already ``v3``, is not matched at all.
    """
    if row is None:
        return
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE dispatches SET metadata_json = "
            "json_set(COALESCE(metadata_json, '{}'), '$.lifecycle_mode', ?), updated_at = ? "
            "WHERE dispatch_id = ? AND project_id = ? "
            "AND json_valid(COALESCE(metadata_json, '{}')) "
            "AND json_type(COALESCE(metadata_json, '{}')) = 'object' "
            "AND json_extract(COALESCE(metadata_json, '{}'), '$.lifecycle_mode') IS NOT ?",
            (MODE_V3, _now_utc(), attempt.dispatch_id, attempt.project_id, MODE_V3))
        conn.commit()


def sweep_attempt(state_dir: Path, attempt: InFlightAttempt, now: datetime) -> Tuple[str, Optional[str], Optional[str]]:
    """Decide and book one in-flight attempt as ``(counter, source, error)``."""
    from dispatch_exit import build_end_line  # noqa: PLC0415

    row = _dispatch_row(state_dir, attempt.project_id, attempt.dispatch_id)
    _stamp_lifecycle_mode(state_dir, row, attempt)
    verdict, _signal = liveness(state_dir, attempt)
    if verdict != LIVENESS_DEAD:
        return ("unknown_liveness" if verdict == LIVENESS_UNKNOWN else "live"), None, None
    deadline, source = deadline_for(state_dir, attempt.start_line, row)
    started = _as_utc(attempt.start_line.get("timestamp"))
    if started is None:
        return "open", source, f"{attempt.dispatch_id}#{attempt.attempt}: start line has no timestamp"
    if now <= started + timedelta(seconds=deadline + ABANDON_GRACE_SECONDS):
        return "open", source, None
    # K-3c's intent is exclusive: a sweep that loses the race books nothing.
    booked = finalize(state_dir=state_dir, project_id=attempt.project_id, dispatch_id=attempt.dispatch_id,
             attempt=attempt.attempt, end_state="abandoned", actor="sweep",
             end_line=build_end_line(attempt, end_state="abandoned",
                                     reason=REASON_SWEEP_ABANDONED,
                                     no_push=NO_PUSH_ABANDONED),
             reason=f"{REASON_SWEEP_ABANDONED} (deadline {deadline}s from {source})",
             lock_timeout=SWEEP_LOCK_TIMEOUT_SECONDS)
    return ("abandoned" if booked.status == STATUS_FINALIZED else "open"), source, None


def _monotonic() -> float:
    """The budget's clock, in one place so a test can pin it without touching ``time``."""
    return time.monotonic()


def _budget_spent(budget_deadline: Optional[float]) -> bool:
    """Between items only: a spent budget stops the sweep from taking a new one."""
    return budget_deadline is not None and _monotonic() >= budget_deadline


def _collect(state_dir: Path, project_id: Optional[str], now: datetime,
             budget_seconds: Optional[float] = None) -> Dict[str, Any]:
    """The sweep's work: recover the open intents, then decide every attempt.

    A budget is read between items only: an item already started always finishes.
    """
    budget_deadline = (None if budget_seconds is None
                       else _monotonic() + max(0.0, budget_seconds))
    intents, intent_errors = open_intents(state_dir, project_id)
    flights, flight_errors = v3_in_flight(state_dir, project_id)
    collected: Dict[str, Any] = {name: 0 for name in _THE_COUNTS}
    collected.update(errors=list(intent_errors) + list(flight_errors), sources=[],
                     in_flight=len(flights), open_intents=len(intents), enabled=lifecycle_enabled(),
                     budget_seconds=budget_seconds, budget_exhausted=False, budget_left=0)
    errors: List[str] = collected["errors"]
    if not collected["enabled"]:
        # ADR-045 §9: the brake refuses every append; write nothing, report them.
        collected["lifecycle_off_in_flight"] = len(flights)
        return collected
    for index, (project, dispatch, attempt_no, _path) in enumerate(intents):
        if _budget_spent(budget_deadline):
            collected.update(budget_exhausted=True,
                             budget_left=len(intents) - index + len(flights))
            return collected
        try:
            if recover(state_dir=state_dir, project_id=project, dispatch_id=dispatch,
                       attempt=attempt_no, actor="sweep",
                       reason="recover an unpublished end intent",
                       lock_timeout=SWEEP_LOCK_TIMEOUT_SECONDS).status == STATUS_RECOVERED:
                collected["recovered"] += 1
        except Exception as exc:  # vnx-silent-except: one unreadable intent must not stop the other recoveries (it is counted)
            errors.append(f"recover {dispatch}#{attempt_no}: {type(exc).__name__}: {exc}")
    for index, attempt in enumerate(flights):
        if _budget_spent(budget_deadline):
            collected.update(budget_exhausted=True, budget_left=len(flights) - index)
            return collected
        try:
            counter, source, error = sweep_attempt(state_dir, attempt, now)
        except Exception as exc:  # vnx-silent-except: one refused attempt is counted and reported, never silently dropped
            errors.append(f"sweep {attempt.dispatch_id}#{attempt.attempt}: {type(exc).__name__}: {exc}")
            continue
        if counter in _THE_COUNTS:
            collected[counter] += 1
        if source:
            collected["sources"].append(f"{attempt.dispatch_id}#{attempt.attempt}:{source}")
        if error:
            errors.append(error)
    return collected


def _heartbeat(state_dir: Path, collected: Optional[Dict[str, Any]], *, project_id: Optional[str],
               started_at: str, finished_at: str, evaluated: datetime, error: Optional[str],
               budget_seconds: Optional[float] = None) -> str:
    """Write the one heartbeat of a run; a run that raised still calls this."""
    data = collected or {}
    errors = list(data.get("errors") or []) + ([error] if error else [])
    path = Path(state_dir) / END_INTENT_DIRNAME / SWEEP_HEARTBEAT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {
        "project_id": project_id, "started_at": started_at, "finished_at": finished_at,
        "evaluated_at": evaluated.strftime(_LEDGER_TS_FORMAT),
        "lifecycle_off": None if collected is None else not collected["enabled"],
        "counts": dict({name: data.get(name, 0) for name in _THE_COUNTS},
                       open_intents=data.get("open_intents", 0), errors=len(errors)),
        "deadline": {"grace_seconds": ABANDON_GRACE_SECONDS,
                     "fallback_seconds": FALLBACK_DEADLINE_SECONDS,
                     "sources": list(data.get("sources") or [])},
        "budget": {"seconds": budget_seconds,
                   "exhausted": bool(data.get("budget_exhausted", False)),
                   "items_left": int(data.get("budget_left", 0))},
        "last_error": errors[-1] if errors else None,
        "error": error,
    })
    return str(path)


def sweep(*, state_dir: str | Path, project_id: Optional[str] = None,
          now: Any = None, budget_seconds: Optional[float] = None) -> SweepResult:
    """One sweep run (ADR-045 §6): recover the open intents, abandon the dead.

    ``now`` is the evaluation instant (``datetime`` or ``...Z``), absent is the real
    clock. ``budget_seconds`` bounds the run between items (K-6b): what is left is
    reported as ``budget_exhausted``/``budget_left`` and picked up by the next
    sweep; without it the run is unbounded, as the runner's tick wants. Every run
    writes one heartbeat; a raised run writes it before raising.
    """
    state_path, started_at = Path(state_dir), _now_utc()
    evaluated = _as_utc(now) or datetime.now(timezone.utc)
    try:
        collected = _collect(state_path, project_id, evaluated, budget_seconds)
    except Exception as exc:
        try:
            _heartbeat(state_path, None, project_id=project_id, started_at=started_at,
                       finished_at=_now_utc(), evaluated=evaluated,
                       error=f"{type(exc).__name__}: {exc}", budget_seconds=budget_seconds)
        except OSError as write_exc:  # vnx-silent-except: the original failure is the one to raise; the heartbeat's own failure is only logged
            logger.warning("[dispatch_lifecycle] sweep heartbeat unwritable: %s", write_exc)
        raise
    heartbeat_path = _heartbeat(state_path, collected, project_id=project_id,
                                started_at=started_at, finished_at=_now_utc(),
                                evaluated=evaluated, error=None,
                                budget_seconds=budget_seconds)
    return SweepResult(errors=tuple(collected["errors"]), heartbeat_path=heartbeat_path,
                       lifecycle_off=not collected["enabled"],
                       budget_exhausted=collected["budget_exhausted"],
                       budget_left=collected["budget_left"],
                       **{name: collected[name] for name in _THE_COUNTS})
