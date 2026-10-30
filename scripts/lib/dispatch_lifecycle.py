#!/usr/bin/env python3
"""ADR-045 (audit-spoor-kern K-3c): the finalizer and the recovery of one end.

``finalize`` owns steps 1 to 5 of the plan's "Einde en herstel", ``recover``
steps 2 to 5 for an intent that never published. Nothing calls either yet:
K-5 wires the lanes, K-6 the sweep (ADR-045 §9 — the writers land behind
``ledger_lifecycle``, which K-act turns on).

What the line SAYS is not decided here. The K-3b contract in
``append_receipt_internals.lifecycle`` owns the canonical form, the intent
layout and the accept rule; this module owns the ORDER in which a line is
frozen, appended, projected and published (ADR-005, ledger first), and the one
thing the ADR leaves to the finalizer: the attempt check under
``BEGIN IMMEDIATE`` (OI-2443). The intent lives beside the LEDGER, because the
accept rule resolves it from the ledger it appends to.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from append_receipt_internals.common import (
    EXIT_IO_ERROR,
    EXIT_LOCK_ERROR,
    EXIT_VALIDATION_ERROR,
    AppendReceiptError,
)
from append_receipt_internals.lifecycle import (
    EndIntentMalformed,
    build_end_intent,
    end_intent_path,
    load_end_intent,
    prepare_end_line,
    write_end_intent,
)
from coordination_db import TERMINAL_DISPATCH_STATES, db_path_from_state_dir, get_connection
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE
from runtime_state_machine import transition_dispatch_idempotent

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


def _find_end_line(ledger: Path, document: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The end line for this intent's key at or after its byte offset, if any.

    A torn line (a crash mid-append) is not evidence that a complete end line
    exists, so it is skipped and the append layer decides (ADR-045 §3).
    """
    offset = int(document["ledger_offset"])
    try:
        if ledger.stat().st_size <= offset:
            return None
        with ledger.open("rb") as handle:
            handle.seek(offset)
            blob = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AppendReceiptError(
            "ledger_unreadable", EXIT_IO_ERROR, f"cannot read the ledger {ledger}: {exc}") from exc
    key = (document["project_id"], document["dispatch_id"], document["attempt"])
    for raw in blob.split(b"\n"):
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (isinstance(line, dict)
                and str(line.get("event_type") or "").strip() == DISPATCH_FINISHED_EVENT_TYPE
                and (line.get("project_id"), line.get("dispatch_id"), line.get("attempt")) == key):
            return line
    return None


def _append_end_line(document: Mapping[str, Any]) -> str:
    """Step 3: append the frozen line unless the ledger already carries it."""
    from append_receipt_internals.payload import append_receipt_payload

    ledger = Path(document["ledger_path"])
    if _find_end_line(ledger, document) is not None:
        return LINE_ALREADY_IN_LEDGER
    try:
        return append_receipt_payload(
            json.loads(document["line"]), receipts_file=str(ledger), skip_enrichment=True,
        ).status
    except AppendReceiptError as exc:
        # Only another writer of this same attempt can have produced a second
        # end line for the key. Continue only when the ledger proves the line
        # landed; anything else stays refused.
        if exc.code != "duplicate_lifecycle_end" or _find_end_line(ledger, document) is None:
            raise
        return LINE_ALREADY_IN_LEDGER


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
def _publication_lock(lock_path: Path, lock_timeout: Optional[float]):
    """An exclusive flock held through step 5 (ADR-045 §3).

    Without ``lock_timeout`` the wait is unbounded, as a lane's finalizer may be;
    a bounded caller gets ``lock_timeout`` and nothing published, so the next
    tick retries.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if lock_timeout is None else time.monotonic() + max(0.0, lock_timeout)
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            flags = fcntl.LOCK_EX if deadline is None else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AppendReceiptError(
                        "lock_timeout", EXIT_LOCK_ERROR,
                        f"publication lock {lock_path} was still held after {lock_timeout}s; "
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
    with _publication_lock(intent_path.with_suffix(".lock"), lock_timeout):
        document = _reload_intent(intent_path)  # step 2, re-read under the lock
        if document is None:
            return LifecycleResult(status=STATUS_NO_INTENT, intent_path=intent_path)
        published_at = document.get("published_at")
        if published_at:
            return LifecycleResult(status=STATUS_ALREADY_PUBLISHED, intent_path=intent_path,
                                   end_state=document["end_state"], published_at=published_at)
        line_status = _append_end_line(document)                               # step 3
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
