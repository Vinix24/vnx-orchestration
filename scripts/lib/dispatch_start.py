"""ADR-045 (audit-spoor-kern K-4): the start line at the door's hand-over.

The hand-over to a lane is the one place a ``dispatch_started`` line can honestly be
written — before the occupancy slot and the spawn (§1/§5). Order is binding: ledger,
then ``lifecycle_mode`` on the row, then slot and spawn; with ``ledger_lifecycle`` on,
a line that cannot be written refuses the dispatch rather than running it untraced.
``schema_version``/``writer_sha`` are the append layer's stamping place; ends are K-5's.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from append_receipt_internals.common import AppendReceiptError
from append_receipt_internals.lifecycle import end_intent_path, lifecycle_enabled
from atomic_io import atomic_write_json
from coordination_db import db_path_from_state_dir, get_connection
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE

LEDGER_FILENAME = "t0_receipts.ndjson"
MODE_V3 = "v3"
MODE_LEGACY = "legacy"
REFUSAL_CODE = "start-line-refused"


class StartRefused(Exception):
    """A hand-over the door must refuse; ``code`` is the named reason."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Handover:
    """What the door fixed at the hand-over (K-5 threads this into the finalizer)."""

    project_id: str
    dispatch_id: str
    attempt: int
    lifecycle_mode: str
    door_pid: int
    door_started_at: str
    started_at: str

    @property
    def is_v3(self) -> bool:
        return self.lifecycle_mode == MODE_V3


def process_started_at(pid: int) -> Optional[str]:
    """The kernel's start time for *pid*, else None. Deliberately uncached: a reused pid
    must not inherit its predecessor's evidence (§5)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        probe = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                               text=True, timeout=10, env=dict(os.environ, LC_ALL="C"), check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    started = probe.stdout.strip()
    return started if probe.returncode == 0 and started else None


def read_attempt_count(state_dir: str | Path, project_id: str, dispatch_id: str) -> Optional[int]:
    """``dispatches.attempt_count`` for this (project, dispatch), or None."""
    if not Path(db_path_from_state_dir(state_dir)).is_file():
        return None
    try:
        with get_connection(state_dir) as conn:
            row = conn.execute(
                "SELECT attempt_count FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                (dispatch_id, project_id)).fetchone()
    except Exception:  # vnx-silent-except: an unreadable store is refused by the caller, never a crash here
        return None
    attempt = row["attempt_count"] if row is not None else None
    return attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1 else None


def _lifecycle_lines(state_dir: str | Path, project_id: str,
                     dispatch_id: str) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    """This key's start lines (file order) and its end lines, by attempt."""
    ledger = Path(state_dir) / LEDGER_FILENAME
    try:
        text = ledger.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], {}
    except OSError as exc:
        raise StartRefused("ledger_unreadable", f"cannot read {ledger}: {exc}") from exc
    starts: List[Dict[str, Any]] = []
    ends: Dict[int, Dict[str, Any]] = {}
    for raw in text.splitlines():
        if dispatch_id not in raw:  # format-agnostic prefilter; the separators are not ours
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a torn line is not evidence of a completed record
        if not isinstance(line, dict) or line.get("project_id") != project_id:
            continue
        attempt = line.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            continue
        event_type = str(line.get("event_type") or "").strip()
        if event_type == DISPATCH_STARTED_EVENT_TYPE:
            starts.append(line)
        elif event_type == DISPATCH_FINISHED_EVENT_TYPE:
            ends.setdefault(attempt, line)
    return starts, ends


def spawn_file_path(state_dir: str | Path, project_id: str, dispatch_id: str,
                    attempt: int) -> Path:
    """The §5 spawn file; derived from the intent path so both share its component rules."""
    try:
        return end_intent_path(state_dir, project_id, dispatch_id, attempt).with_suffix(".spawn.json")
    except AppendReceiptError as exc:
        raise StartRefused(exc.code, str(exc)) from exc


def _live(pid: object, started_at: object) -> bool:
    """True only when *pid* is alive AND carries the recorded start time."""
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and isinstance(started_at, str) and bool(started_at.strip())
            and process_started_at(pid) == started_at)


def _occupancy_held(state_dir: Path, dispatch_id: str) -> bool:
    """True when a live process holds the worktree's occupancy flock (the isolation
    module's own probe, so the lock is exactly ``_acquire_occupancy``'s; unmeasurable
    counts as held)."""
    from dispatch_worktree_isolation import claims_dir_for_state_dir, probe_occupancy  # noqa: PLC0415

    try:
        return probe_occupancy(claims_dir_for_state_dir(state_dir), dispatch_id) == "held"
    except OSError:
        return True


def _liveness(start_line: Dict[str, Any], *, state_dir: Path, dispatch_id: str) -> Optional[str]:
    """The first of §5's three liveness signals that holds, else None."""
    if _live(start_line.get("door_pid"), start_line.get("door_started_at")):
        return "door_pid"
    try:
        spawn = json.loads(spawn_file_path(state_dir, start_line["project_id"], dispatch_id,
                                           int(start_line["attempt"])).read_text(encoding="utf-8"))
    except (KeyError, TypeError, ValueError, OSError, StartRefused, json.JSONDecodeError):
        spawn = None
    if isinstance(spawn, dict) and _live(spawn.get("child_pid"), spawn.get("child_started_at")):
        return "child_pid"
    return "occupancy_flock" if _occupancy_held(state_dir, dispatch_id) else None


def _plan_attempt(*, state_dir: Path, project_id: str, dispatch_id: str) -> int:
    """The attempt this hand-over is (§3; plan-gate round 2 req. 2): the row's count unless
    the ledger already carries that attempt's start — then this fire is a refire. Refused
    while the previous attempt has a start with no end and liveness (same worktree)."""
    row_attempt = read_attempt_count(state_dir, project_id, dispatch_id)
    if row_attempt is None:
        raise StartRefused(
            "start_attempt_unreadable",
            f"dispatches.attempt_count is unreadable for (project_id={project_id!r}, "
            f"dispatch_id={dispatch_id!r}); the start line's key cannot be formed")
    starts, ends = _lifecycle_lines(state_dir, project_id, dispatch_id)
    if not starts:
        return row_attempt
    last = max(starts, key=lambda line: line["attempt"])
    if last["attempt"] not in ends:
        alive = _liveness(last, state_dir=state_dir, dispatch_id=dispatch_id)
        if alive is not None:
            raise StartRefused(
                "refire_attempt_live",
                f"attempt {last['attempt']} already has a dispatch_started without a "
                f"dispatch_finished and is still live ({alive}); refusing the refire "
                "(ADR-045 §3/§5)")
    return max(row_attempt, int(last["attempt"]) + 1)


def _record_lifecycle_mode(handover: Handover, *, state_dir: Path) -> str:
    """Step 2: ``lifecycle_mode`` (and the attempt) into ``dispatches``, idempotent, only
    after the line is on disk; an unwritable store is not fatal and nothing downgrades v3."""
    if not Path(db_path_from_state_dir(state_dir)).is_file():
        return "no_store"
    try:
        with get_connection(state_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT metadata_json, attempt_count FROM dispatches "
                "WHERE dispatch_id = ? AND project_id = ?",
                (handover.dispatch_id, handover.project_id)).fetchone()
            metadata = json.loads((row["metadata_json"] if row is not None else None) or "{}")
            if row is None or not isinstance(metadata, dict):
                conn.rollback()
                return "no_row" if row is None else "metadata_unreadable"
            current = metadata.get("lifecycle_mode")
            if current != MODE_V3:
                metadata["lifecycle_mode"] = handover.lifecycle_mode
            if metadata.get("lifecycle_mode") == current and row["attempt_count"] == handover.attempt:
                conn.rollback()
                return "unchanged"
            conn.execute(
                "UPDATE dispatches SET metadata_json = ?, attempt_count = ?, updated_at = ? "
                "WHERE dispatch_id = ? AND project_id = ?",
                (json.dumps(metadata, sort_keys=True), handover.attempt, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 handover.dispatch_id, handover.project_id))
            conn.commit()
            return "written"
    except Exception:  # vnx-silent-except: ledger is the source; a DB hiccup must not kill the dispatch after its start line
        return "write_failed"


def _base_sha(repo_root: Path, base_ref: str) -> str:
    """Resolve *base_ref* to a commit sha; a refusal when git cannot."""
    try:
        probe = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--verify",
                                f"{base_ref}^{{commit}}"],
                               capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartRefused("base_sha_unresolved", f"cannot resolve {base_ref!r}: {exc}") from exc
    if probe.returncode != 0 or not probe.stdout.strip():
        raise StartRefused("base_sha_unresolved",
                           f"git rev-parse {base_ref!r} failed: {probe.stderr.strip() or 'no output'}")
    return probe.stdout.strip()


def begin(
    *, project_id: str, dispatch_id: str, role: str, base_ref: str, spec_file: str | Path,
    target_slot: Optional[str], state_dir: str | Path, project_root: Path,
) -> Handover:
    """The hand-over: fix the key, write the start line, then the mode.

    On the shipped default (``ledger_lifecycle`` off) nothing reaches the ledger and the row
    only learns ``lifecycle_mode: legacy``; with it on, a refusal means the door wrote no DB
    mode, took no slot and spawned nothing.
    """
    state_path = Path(state_dir)
    door_pid = os.getpid()
    door_started = process_started_at(door_pid)
    if door_started is None:
        raise StartRefused(
            "door_identity_unresolved",
            f"the door process {door_pid} has no readable start time; a start line "
            "cannot record liveness (ADR-045 §5)")
    mode = MODE_V3 if lifecycle_enabled() else MODE_LEGACY
    if mode == MODE_V3:
        attempt = _plan_attempt(state_dir=state_path, project_id=project_id, dispatch_id=dispatch_id)
    else:
        attempt = read_attempt_count(state_path, project_id, dispatch_id) or 1
    handover = Handover(project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
                        lifecycle_mode=mode, door_pid=door_pid, door_started_at=door_started,
                        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if mode == MODE_V3:
        from append_receipt_internals.payload import append_receipt_payload  # noqa: PLC0415
        slot = (target_slot or "").strip()
        try:
            spec_sha = hashlib.sha256(Path(spec_file).read_bytes()).hexdigest()
        except OSError as exc:
            raise StartRefused("spec_unreadable", f"cannot read the staged spec {spec_file}: {exc}") from exc
        receipt = {
            "timestamp": handover.started_at, "event_type": DISPATCH_STARTED_EVENT_TYPE,
            "project_id": project_id, "dispatch_id": dispatch_id, "attempt": attempt,
            "role": role, "base_sha": _base_sha(project_root, base_ref),
            "spec_sha256": spec_sha,
            "requested_by": (os.environ.get("VNX_REQUESTED_BY") or "").strip()
            or (f"terminal:{slot}" if slot else f"door:{door_pid}"),
            "lifecycle_mode": mode, "door_pid": door_pid, "door_started_at": door_started,
        }
        try:
            append_receipt_payload(receipt, receipts_file=str(state_path / LEDGER_FILENAME))
        except AppendReceiptError as exc:
            raise StartRefused(exc.code, str(exc)) from exc
    _record_lifecycle_mode(handover, state_dir=state_path)
    return handover


def record_child(handover: Handover, *, state_dir: str | Path, child_pid: int) -> Optional[Path]:
    """The ``on_spawn`` hook: record the child the moment the spawn returns its pid."""
    started = process_started_at(child_pid)
    if started is None:
        return None
    path = spawn_file_path(state_dir, handover.project_id, handover.dispatch_id, handover.attempt)
    atomic_write_json(path, {"version": 1, "project_id": handover.project_id,
                             "dispatch_id": handover.dispatch_id, "attempt": handover.attempt,
                             "child_pid": child_pid, "child_started_at": started})
    return path
