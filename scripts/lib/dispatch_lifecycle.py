#!/usr/bin/env python3
"""ADR-045 (audit-spoor-kern K-3): the end finalizer of a v3 dispatch.

Steps 1-5 of "Einde en herstel", keyed on ``(project_id, dispatch_id, attempt)``
(plan-gate requirement 2): an exclusive intent holding the canonical end line and
its sha256 (temp file plus ``os.link``); a ``flock`` held through step 5 with the
intent re-read under it; the ledger line appended from the intent's byte offset
through the normal append layer; the projection onto ``dispatches.state`` along
the plan table "Eindtoestand naar projectie", one ``transition_dispatch`` per step;
and ``published_at`` via temp file plus rename. ``recover`` re-runs steps 2-5.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Mapping

from append_receipt_internals.common import AppendReceiptError
from append_receipt_internals.lifecycle import (
    EndIntentMalformed, build_end_intent, end_intent_path, load_end_intent,
)
# The one append entry point (Path 2 of scripts/append_receipt.py), imported from
# the internals module: a lib-only entry point has scripts/lib on sys.path, not scripts/.
from append_receipt_internals.payload import append_receipt_payload
from coordination_db import TERMINAL_DISPATCH_STATES, get_connection
from receipt_schema import (
    DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE,
    LIFECYCLE_END_STATES, LIFECYCLE_MODES, lifecycle_end_key,
)
from runtime_state_machine import transition_dispatch

LEDGER_FILENAME = "t0_receipts.ndjson"
PROJECTION_ACTOR = "lifecycle"
PUBLISHED, ALREADY_PUBLISHED, LOST_INTENT, NOT_V3 = "published", "already_published", "lost_intent", "not_v3"
PROJECTION_APPLIED, PROJECTION_ALREADY_AT_TARGET = "applied", "already_at_target"
PROJECTION_CONFLICT = "projection_conflict"
FinalizeResult = SimpleNamespace

# The plan table "Eindtoestand naar projectie": end_state -> row target and the
# only paths there. ``abandoned`` shares ``timed_out``'s row and path and adds
# metadata_json.end_state (ADR-045 §4: never a new DB state).
PROJECTION_TARGET = {"completed": "completed", "failed": "failed_delivery",
                     "timed_out": "timed_out", "abandoned": "timed_out"}
PROJECTION_PATHS = {
    "completed": {"running": ("completed",), "accepted": ("running", "completed"),
                  "delivering": ("accepted", "running", "completed")},
    "failed": {"delivering": ("failed_delivery",), "running": ("failed_delivery",),
               "claimed": ("delivering", "failed_delivery"),
               "accepted": ("running", "failed_delivery")},
    "timed_out": {"delivering": ("timed_out",), "accepted": ("timed_out",),
                  "running": ("timed_out",), "claimed": ("delivering", "timed_out")},
}
PROJECTION_PATHS["abandoned"] = PROJECTION_PATHS["timed_out"]


class DispatchLifecycleError(RuntimeError):
    """A lifecycle operation refused, with the reason in ``code``."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


def ledger_path_for(state_dir: str | Path) -> Path:
    """``<state_dir>/t0_receipts.ndjson`` — the ledger the append layer defaults to."""
    return Path(state_dir) / LEDGER_FILENAME


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_key(project_id: str, dispatch_id: str, attempt: int) -> None:
    for value, field in ((project_id, "project_id"), (dispatch_id, "dispatch_id")):
        if not str(value or "").strip():
            raise DispatchLifecycleError(f"missing_{field}", f"the key needs a {field}")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise DispatchLifecycleError("invalid_attempt", f"attempt is not a positive int: {attempt!r}")


def _write_atomic(target: Path, text: str, prefix: str, *, exclusive: bool) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(target.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if not exclusive:
            os.replace(tmp, target)
            return True
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _dump(document: Mapping[str, Any]) -> str:
    return json.dumps(dict(document), sort_keys=True, separators=(",", ":")) + "\n"


def create_intent(intent_file: Path, document: Mapping[str, Any]) -> bool:
    """Step 1: the exclusive intent; ``False`` when another caller won."""
    return _write_atomic(intent_file, _dump(document), ".intent-", exclusive=True)


def _publish_intent(intent_file: Path, intent: Mapping[str, Any]) -> Dict[str, Any]:
    published = dict(intent, published_at=_now_utc())
    _write_atomic(intent_file, _dump(published), ".publish-", exclusive=False)
    return published


def _read_intent(intent_file: Path) -> Dict[str, Any]:
    try:
        intent = load_end_intent(intent_file)
    except EndIntentMalformed as exc:
        raise DispatchLifecycleError("invalid_end_intent", str(exc)) from exc
    if intent is not None and intent["end_state"] in LIFECYCLE_END_STATES:
        return intent
    raise DispatchLifecycleError(
        "missing_end_intent" if intent is None else "invalid_end_intent",
        f"{intent_file}: no intent" if intent is None else f"{intent_file}: bad end_state")


def _iter_ledger(ledger_path: Path, offset: int = 0) -> Iterator[Dict[str, Any]]:
    blob = ledger_path.read_bytes()[offset:] if ledger_path.exists() else b""
    for raw in blob.split(b"\n"):
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(line, dict):
            yield line


def _append_end_line(intent: Mapping[str, Any], ledger_path: Path) -> bool:
    key = lifecycle_end_key(intent)
    if any(line.get("event_type") == DISPATCH_FINISHED_EVENT_TYPE
           and lifecycle_end_key(line) == key
           for line in _iter_ledger(ledger_path, int(intent["ledger_offset"]))):
        return False
    try:
        result = append_receipt_payload(
            json.loads(intent["line"]), receipts_file=str(ledger_path), skip_enrichment=True)
    except AppendReceiptError as exc:
        if exc.code != "duplicate_lifecycle_end":
            raise
        return False
    return result.status == "appended"


def _mark_abandoned(conn, intent: Mapping[str, Any]) -> None:
    key = (intent["dispatch_id"], intent["project_id"])
    row = conn.execute("SELECT metadata_json FROM dispatches WHERE dispatch_id = ? "
                       "AND project_id = ?", key).fetchone()
    try:
        metadata = json.loads((row["metadata_json"] if row else None) or "{}")
        metadata["end_state"] = "abandoned"
    except (ValueError, TypeError) as exc:
        raise DispatchLifecycleError("invalid_metadata_json", f"metadata_json for "
                                     f"{intent['dispatch_id']!r} is unusable: {exc}") from exc
    conn.execute("UPDATE dispatches SET metadata_json = ?, updated_at = ? "
                 "WHERE dispatch_id = ? AND project_id = ?",
                 (json.dumps(metadata, sort_keys=True), _now_utc()) + key)


def _project(intent: Mapping[str, Any], state_dir: Path) -> Dict[str, Any]:
    end_state = str(intent["end_state"])
    with get_connection(state_dir) as conn:
        row = conn.execute("SELECT state FROM dispatches WHERE dispatch_id = ? "
                           "AND project_id = ?",
                           (intent["dispatch_id"], intent["project_id"])).fetchone()
        from_state = row["state"] if row is not None else None
        path = PROJECTION_PATHS[end_state].get(from_state or "")
        out: Dict[str, Any] = {"from_state": from_state, "steps": [],
                               "target": PROJECTION_TARGET[end_state]}
        if path is None and from_state == out["target"]:
            return dict(out, status=PROJECTION_ALREADY_AT_TARGET)
        if path is None:
            reason = ("no_row" if from_state is None else
                      "terminal_state" if from_state in TERMINAL_DISPATCH_STATES else "no_path")
            return dict(out, status=PROJECTION_CONFLICT, reason=reason)
        for to_state in path:
            transition_dispatch(
                conn, dispatch_id=intent["dispatch_id"], project_id=intent["project_id"],
                to_state=to_state, actor=PROJECTION_ACTOR,
                reason=f"dispatch_lifecycle: end_state={end_state}")
        if end_state == "abandoned":
            _mark_abandoned(conn, intent)
        conn.commit()
        return dict(out, status=PROJECTION_APPLIED, steps=list(path))


def _result(status, intent, intent_file, ledger_written, projection) -> FinalizeResult:
    return FinalizeResult(status=status, end_state=intent["end_state"],
                          end_line_sha256=intent["sha256"], intent_path=intent_file,
                          ledger_written=ledger_written, projection=projection)


def _run_steps_under_lock(intent_file: Path, state_dir: Path) -> FinalizeResult:
    lock_file = intent_file.with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            intent = _read_intent(intent_file)
            if intent.get("published_at"):
                return _result(ALREADY_PUBLISHED, intent, intent_file, False, {})
            written = _append_end_line(intent, Path(intent["ledger_path"]))
            projection = _project(intent, state_dir)
            return _result(PUBLISHED, _publish_intent(intent_file, intent),
                           intent_file, written, projection)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lifecycle_mode_of(project_id: str, dispatch_id: str, attempt: int, *,
                      ledger_path: Path) -> str:
    """``lifecycle_mode`` from this attempt's ``dispatch_started`` line (ADR-045 §9)."""
    key = lifecycle_end_key({"project_id": project_id, "dispatch_id": dispatch_id,
                             "attempt": attempt})
    start = next((line for line in _iter_ledger(Path(ledger_path))
                  if line.get("event_type") == DISPATCH_STARTED_EVENT_TYPE
                  and lifecycle_end_key(line) == key), None)
    if start is None:
        raise DispatchLifecycleError(
            "missing_start_line",
            f"no dispatch_started for (project_id={project_id!r}, dispatch_id={dispatch_id!r}, "
            f"attempt={attempt!r}) in {ledger_path}")
    mode = str(start.get("lifecycle_mode") or "").strip()
    if mode not in LIFECYCLE_MODES:
        raise DispatchLifecycleError("invalid_lifecycle_mode", f"start line mode={mode!r}")
    return mode


def finalize(project_id: str, dispatch_id: str, attempt: int,
             end_line: Mapping[str, Any], *, state_dir: str | Path) -> FinalizeResult:
    """Steps 1 to 5 for one attempt of a ``v3`` dispatch.

    A caller with a different ``end_line`` loses the intent race.
    """
    _validate_key(project_id, dispatch_id, attempt)
    state_dir = Path(state_dir).resolve()
    ledger_path = ledger_path_for(state_dir)
    intent_file = end_intent_path(state_dir, project_id, dispatch_id, attempt)
    end_state = str(end_line.get("end_state") or "").strip()
    if lifecycle_mode_of(project_id, dispatch_id, attempt, ledger_path=ledger_path) != "v3":
        return _result(NOT_V3, {"end_state": end_state, "sha256": ""}, intent_file, False,
                       {"status": "skipped", "reason": "lifecycle_mode=legacy"})
    if end_state not in LIFECYCLE_END_STATES:
        raise DispatchLifecycleError("invalid_end_state",
                                     f"end_state must be one of {sorted(LIFECYCLE_END_STATES)}, "
                                     f"got {end_state!r}")
    try:
        document = build_end_intent(
            project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
            end_state=end_state, end_line=end_line, ledger_path=ledger_path,
            ledger_offset=ledger_path.stat().st_size if ledger_path.exists() else 0)
    except (TypeError, ValueError) as exc:
        raise DispatchLifecycleError("invalid_end_line", f"the end line is not JSON: {exc}") from exc
    if not create_intent(intent_file, document):
        return _result(LOST_INTENT, _read_intent(intent_file), intent_file, False, {})
    return _run_steps_under_lock(intent_file, state_dir)


def recover(project_id: str, dispatch_id: str, attempt: int, *,
            state_dir: str | Path) -> FinalizeResult:
    """Steps 2 to 5 for an intent without ``published_at``, under the same lock."""
    _validate_key(project_id, dispatch_id, attempt)
    state_dir = Path(state_dir).resolve()
    intent_file = end_intent_path(state_dir, project_id, dispatch_id, attempt)
    _read_intent(intent_file)
    return _run_steps_under_lock(intent_file, state_dir)
