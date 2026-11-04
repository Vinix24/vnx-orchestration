"""ADR-045 (audit-spoor-kern K-4a): the start line at the door's hand-over.

The hand-over is the one place a ``dispatch_started`` line can honestly be written —
before the occupancy slot and the spawn (§1/§5): ledger first, then the
``lifecycle_mode`` projection, then slot and spawn. Nothing calls this module yet (K-4b
wires the door, K-4c the spawn file); ``begin`` takes the base as an already resolved
sha (OI-2510 p. 5), and a failed attempt read is never "attempt 1" (p. 6/7).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import time
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from append_receipt_internals.common import AppendReceiptError
from append_receipt_internals.lifecycle import end_intent_path, lifecycle_enabled
from atomic_io import atomic_write_json
from coordination_db import db_path_from_state_dir, get_connection
from ndjson_io import NdjsonCorruptionError, read_ndjson_strict
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE

LEDGER_FILENAME = "t0_receipts.ndjson"
MODE_V3 = "v3"
MODE_LEGACY = "legacy"
REFUSAL_CODE = "start-line-refused"
MODE_WRITE_OK = "ok"
MODE_WRITE_FAILED = "write_failed"
# A hand-over waits at most this long for the per-dispatch lock before refusing.
HANDOVER_LOCK_TIMEOUT_SECONDS = 5.0
_HANDOVER_LOCK_POLL_SECONDS = 0.05
# The three results of reading ``dispatches.attempt_count`` (OI-2510 point 6).
ATTEMPT_STORED = "stored"
ATTEMPT_NO_ROW = "no_row"
ATTEMPT_UNREADABLE = "unreadable"

logger = logging.getLogger(__name__)

AttemptState = namedtuple("AttemptState", ("status", "count", "detail"), defaults=(0, ""))


class StartRefused(Exception):
    """A hand-over the door must refuse; ``code`` is the named reason."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Handover:
    """What the door fixed; ``attempt`` 0 means "count unreadable", never written (OI-2510 p. 7)."""

    project_id: str
    dispatch_id: str
    attempt: int
    lifecycle_mode: str
    door_pid: int
    door_started_at: str
    started_at: str
    mode_write: str = MODE_WRITE_OK

    @property
    def is_v3(self) -> bool:
        return self.lifecycle_mode == MODE_V3


def _now() -> str: return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_started_at(pid: object) -> Optional[str]:
    """The kernel start time for *pid*, else None. Uncached: a reused pid is no evidence."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0: return None
    try:
        probe = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                               text=True, timeout=10, env=dict(os.environ, LC_ALL="C"), check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return probe.stdout.strip() if probe.returncode == 0 and probe.stdout.strip() else None


def read_attempt_state(state_dir: str | Path, project_id: str, dispatch_id: str) -> AttemptState:
    """Stored count, no row yet, or a read failure; a malformed count is not stored.

    Only a missing path is "no row"; an existing non-file path is unreadable (K-9c rule 3).
    """
    db_path = Path(db_path_from_state_dir(state_dir))
    if not db_path.exists(): return AttemptState(ATTEMPT_NO_ROW)
    if not db_path.is_file():
        return AttemptState(ATTEMPT_UNREADABLE, detail=f"{db_path} exists but is not a regular file")
    try:
        with get_connection(state_dir) as conn:
            row = conn.execute(
                "SELECT attempt_count FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                (dispatch_id, project_id)).fetchone()
    except Exception as exc:  # vnx-silent-except: the caller decides; v3 refuses, legacy never guesses
        return AttemptState(ATTEMPT_UNREADABLE, detail=str(exc))
    if row is None: return AttemptState(ATTEMPT_NO_ROW)
    count = row["attempt_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return AttemptState(ATTEMPT_UNREADABLE, detail=f"attempt_count={count!r} is not positive")
    return AttemptState(ATTEMPT_STORED, count=count)


def _lifecycle_lines(state_dir, project_id: str, dispatch_id: str) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    """This exact key's start lines (file order) and its end lines, by attempt.

    The ledger is read strictly with the refusal scoped to lines carrying this dispatch
    id (OI-2528): another dispatch's break is skipped, this dispatch's own refuses
    ``ledger_corrupt``; a torn final fragment stays tolerated.
    """
    ledger = Path(state_dir) / LEDGER_FILENAME
    try:
        records = read_ndjson_strict(ledger, lambda raw: dispatch_id in raw)
    except NdjsonCorruptionError as exc:
        raise StartRefused("ledger_corrupt", f"{exc}") from exc
    except OSError as exc:
        raise StartRefused("ledger_unreadable", f"cannot read {ledger}: {exc}") from exc
    starts, ends = [], {}
    for line in records:
        # The prefilter admits ids that merely CONTAIN this one (``job-extra`` for
        # ``job``); only the exact key counts (ADR-045 §3).
        attempt = line.get("attempt") if isinstance(line, dict) else None
        if (not isinstance(line, dict) or line.get("project_id") != project_id
                or line.get("dispatch_id") != dispatch_id
                or isinstance(attempt, bool) or not isinstance(attempt, int)): continue
        kind = str(line.get("event_type") or "").strip()
        if kind == DISPATCH_STARTED_EVENT_TYPE: starts.append(line)
        elif kind == DISPATCH_FINISHED_EVENT_TYPE: ends.setdefault(attempt, line)
    return starts, ends


def spawn_file_path(state_dir, project_id: str, dispatch_id: str, attempt: int) -> Path:
    """The §5 spawn file, derived from the intent path's component rules."""
    try:
        return end_intent_path(state_dir, project_id, dispatch_id, attempt).with_suffix(".spawn.json")
    except AppendReceiptError as exc:
        raise StartRefused(exc.code, str(exc)) from exc


def handover_lock_path(state_dir: str | Path, project_id: str, dispatch_id: str) -> Path:
    """``<state>/dispatch_end/<project_id>/<dispatch_id>.handover.lock``, beside the intents."""
    try:
        return end_intent_path(state_dir, project_id, dispatch_id, 1).parent / f"{dispatch_id}.handover.lock"
    except AppendReceiptError as exc:
        raise StartRefused(exc.code, str(exc)) from exc


@contextmanager
def _handover_lock(state_dir: Path, project_id: str, dispatch_id: str) -> Iterator[None]:
    """Hold the per-dispatch lock across attempt choice, liveness and append: two hand-overs
    must not both pick an attempt; a lock still taken at the bound is ``handover_lock_timeout``."""
    path = handover_lock_path(state_dir, project_id, dispatch_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise StartRefused("handover_lock_unavailable", f"cannot open {path}: {exc}") from exc
    try:
        deadline = time.monotonic() + HANDOVER_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StartRefused("handover_lock_timeout", f"{path} stayed taken for "
                        f"{HANDOVER_LOCK_TIMEOUT_SECONDS:g}s; refusing rather than sharing an attempt")
                time.sleep(_HANDOVER_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _live(pid: object, started_at: object) -> bool:
    """True only when *pid* is alive AND carries the recorded start time."""
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and isinstance(started_at, str) and bool(started_at.strip())
            and process_started_at(pid) == started_at)


def _liveness(start_line: Dict[str, Any], *, state_dir: Path, dispatch_id: str) -> Optional[str]:
    """The first of §5's three liveness signals that holds, else None."""
    if _live(start_line.get("door_pid"), start_line.get("door_started_at")): return "door_pid"
    try:
        spawn = json.loads(spawn_file_path(state_dir, start_line["project_id"], dispatch_id,
                                           int(start_line["attempt"])).read_text(encoding="utf-8"))
    except (KeyError, TypeError, ValueError, OSError, StartRefused, json.JSONDecodeError):
        spawn = None
    if isinstance(spawn, dict) and _live(spawn.get("child_pid"), spawn.get("child_started_at")): return "child_pid"
    from dispatch_worktree_isolation import claims_dir_for_state_dir, probe_occupancy  # noqa: PLC0415
    try:
        held = probe_occupancy(claims_dir_for_state_dir(state_dir), dispatch_id) == "held"
    except OSError: held = True  # unmeasurable counts as held (fail closed)
    return "occupancy_flock" if held else None


def _plan_attempt(*, state_dir: Path, project_id: str, dispatch_id: str) -> int:
    """The row's count unless the ledger already carries that attempt's start, and refused
    while the previous attempt is unfinished and live, or the count is unreadable (§3)."""
    state = read_attempt_state(state_dir, project_id, dispatch_id)
    if state.status == ATTEMPT_UNREADABLE:
        raise StartRefused("start_attempt_unreadable", f"dispatches.attempt_count is unreadable "
            f"for (project_id={project_id!r}, dispatch_id={dispatch_id!r}): {state.detail}")
    row_attempt = state.count if state.status == ATTEMPT_STORED else 1
    starts, ends = _lifecycle_lines(state_dir, project_id, dispatch_id)
    if not starts: return row_attempt
    last = max(starts, key=lambda line: line["attempt"])
    if last["attempt"] not in ends:
        alive = _liveness(last, state_dir=state_dir, dispatch_id=dispatch_id)
        if alive is not None:
            raise StartRefused("refire_attempt_live", f"attempt {last['attempt']} has a "
                f"dispatch_started without a dispatch_finished and is live ({alive}); refusing the refire")
    return max(row_attempt, int(last["attempt"]) + 1)


def _record_lifecycle_mode(handover: Handover, *, state_dir: Path, write_attempt: bool) -> str:
    """Step 2: mode (and the attempt, when known) into ``dispatches`` after the line is on
    disk. A failure is logged with key, attempt and exception and is loud (OI-2510 p. 2/7)."""
    if not Path(db_path_from_state_dir(state_dir)).is_file(): return MODE_WRITE_OK
    try:
        with get_connection(state_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT metadata_json FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                (handover.dispatch_id, handover.project_id)).fetchone()
            if row is None:
                conn.rollback()
                return MODE_WRITE_OK
            metadata = json.loads(row["metadata_json"] or "{}")
            if not isinstance(metadata, dict):
                raise ValueError(f"metadata_json is {type(metadata).__name__}, not an object")
            if metadata.get("lifecycle_mode") != MODE_V3: metadata["lifecycle_mode"] = handover.lifecycle_mode
            conn.execute(
                "UPDATE dispatches SET metadata_json = ?, updated_at = ?, "
                "attempt_count = COALESCE(?, attempt_count) "
                "WHERE dispatch_id = ? AND project_id = ?",
                (json.dumps(metadata, sort_keys=True), _now(),
                 handover.attempt if write_attempt else None,
                 handover.dispatch_id, handover.project_id))
            conn.commit()
            return MODE_WRITE_OK
    except Exception as exc:  # vnx-silent-except: the ledger is the source; this projection is loud below
        logger.warning("[dispatch_start] lifecycle_mode write failed dispatch_id=%s attempt=%s: %s",
                       handover.dispatch_id, handover.attempt, exc)
        return MODE_WRITE_FAILED


def _spec_deadline_seconds(blob: bytes) -> Optional[int]:
    """The spec's own legal integer ``deadline_seconds``, else None (K-6b/OI-2522)."""
    from dispatch_lifecycle import _valid_deadline  # noqa: PLC0415
    try:
        value = json.loads(blob).get("deadline_seconds")
        return value if _valid_deadline(value) else None
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return None


def _append_start_line(handover: Handover, *, role: str, base_sha: str, spec_file: str | Path,
                       target_slot: Optional[str], state_dir: Path) -> None:
    """Write the v3 start line; a ``duplicate`` verdict means another hand-over owns the key."""
    from append_receipt_internals.payload import append_receipt_payload  # noqa: PLC0415
    slot = (target_slot or "").strip()
    try:
        spec_bytes = Path(spec_file).read_bytes()
        spec_sha = hashlib.sha256(spec_bytes).hexdigest()
    except OSError as exc:
        raise StartRefused("spec_unreadable", f"cannot read the staged spec {spec_file}: {exc}") from exc
    receipt = {
        "timestamp": handover.started_at, "event_type": DISPATCH_STARTED_EVENT_TYPE,
        "project_id": handover.project_id, "dispatch_id": handover.dispatch_id,
        "attempt": handover.attempt, "role": role, "base_sha": str(base_sha or "").strip(),
        "spec_sha256": spec_sha, "lifecycle_mode": handover.lifecycle_mode,
        "door_pid": handover.door_pid, "door_started_at": handover.door_started_at,
        "requested_by": (os.environ.get("VNX_REQUESTED_BY") or "").strip() or (
            f"terminal:{slot}" if slot else f"door:{handover.door_pid}"),
    }
    if (deadline_seconds := _spec_deadline_seconds(spec_bytes)) is not None:
        receipt["deadline_seconds"] = deadline_seconds
    try:
        result = append_receipt_payload(receipt, receipts_file=str(state_dir / LEDGER_FILENAME))
    except AppendReceiptError as exc:
        raise StartRefused(exc.code, str(exc)) from exc
    status = getattr(result, "status", None)
    if status == "appended": return
    if status == "duplicate":
        raise StartRefused("start_line_duplicate", f"the append layer already holds a start "
            f"line for (project_id={handover.project_id!r}, dispatch_id={handover.dispatch_id!r}, "
            f"attempt={handover.attempt}); this hand-over does not own that attempt")
    raise StartRefused("start_line_unconfirmed",
        f"the append layer reported status {status!r}; the start line is not confirmed on disk")


def begin(
    *, project_id: str, dispatch_id: str, role: str, base_sha: str, spec_file: str | Path,
    target_slot: Optional[str], state_dir: str | Path,
) -> Handover:
    """The hand-over: fix the key, write the start line, then the mode. With ``ledger_lifecycle``
    off nothing reaches the ledger; with it on, a refusal means no DB mode, slot or spawn."""
    state_path = Path(state_dir)
    door_pid = os.getpid()
    # Only a v3 line needs the door's kernel start time; a legacy fire is never refused
    # (or delayed) by an unreadable or slow ``ps`` (OI-2510 point 3), so it never probes.
    mode = MODE_V3 if lifecycle_enabled() else MODE_LEGACY
    if mode == MODE_V3:
        door_started = process_started_at(door_pid)
        if door_started is None:
            raise StartRefused("door_identity_unresolved", f"the door process {door_pid} has "
                "no readable start time; a start line cannot record liveness (ADR-045 §5)")
        with _handover_lock(state_path, project_id, dispatch_id):
            attempt = _plan_attempt(state_dir=state_path, project_id=project_id,
                                    dispatch_id=dispatch_id)
            handover = Handover(project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
                                lifecycle_mode=mode, door_pid=door_pid,
                                door_started_at=door_started, started_at=_now())
            _append_start_line(handover, role=role, base_sha=base_sha, spec_file=spec_file,
                               target_slot=target_slot, state_dir=state_path)
    else:
        state = read_attempt_state(state_path, project_id, dispatch_id)
        if state.status == ATTEMPT_UNREADABLE:
            logger.warning("[dispatch_start] attempt_count unreadable dispatch_id=%s "
                           "project_id=%s: %s; no attempt will be projected",
                           dispatch_id, project_id, state.detail)
        attempt = state.count if state.status == ATTEMPT_STORED else (1 if state.status == ATTEMPT_NO_ROW else 0)
        # A legacy hand-over records what it can; it needs no door identity, and probing
        # ``ps`` here would only delay the fire on a slow host.
        handover = Handover(project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
                            lifecycle_mode=mode, door_pid=door_pid,
                            door_started_at="", started_at=_now())
    projection = _record_lifecycle_mode(handover, state_dir=state_path,
                                        write_attempt=handover.attempt >= 1)
    if projection == MODE_WRITE_FAILED or handover.attempt < 1:
        return replace(handover, mode_write=MODE_WRITE_FAILED)
    return handover


def record_child(handover: Handover, *, state_dir: str | Path, child_pid: int) -> Optional[Path]:
    """The ``on_spawn`` hook: record the child right after the spawn; v3 only, and no
    readable child start time means nothing is written."""
    if not handover.is_v3 or handover.attempt < 1: return None
    started = process_started_at(child_pid)
    if started is None: return None
    path = spawn_file_path(state_dir, handover.project_id, handover.dispatch_id, handover.attempt)
    atomic_write_json(path, {"version": 1, "project_id": handover.project_id,
                             "dispatch_id": handover.dispatch_id, "attempt": handover.attempt,
                             "child_pid": child_pid, "child_started_at": started})
    return path
