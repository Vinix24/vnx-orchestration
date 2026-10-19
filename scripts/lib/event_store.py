#!/usr/bin/env python3
"""EventStore — NDJSON persistence for agent stream events.

Stores one NDJSON file per terminal at .vnx-data/events/{terminal}.ndjson,
except for a per-run terminal (``PER_RUN_TERMINALS``), which keeps one file
per run at .vnx-data/events/{terminal}/{dispatch_id}.ndjson. Supports atomic
append, tail-with-since, and clear (per-dispatch retention).

File locking via fcntl.flock ensures concurrent write safety.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem only.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import logging
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Union

if TYPE_CHECKING:
    from canonical_event import CanonicalEvent

logger = logging.getLogger(__name__)

# Size-rotation threshold (10 MB). Once the live stream exceeds this it is
# archived to the per-dispatch archive dir and truncated to 0 — a size-based
# rotation, not a log-only warning. Configurable per instance via the
# ``rotation_threshold_bytes`` ctor arg or the VNX_EVENT_STREAM_MAX_BYTES env
# var; this module constant is the default and stays patchable for tests.
_SIZE_ROTATION_BYTES = 10 * 1024 * 1024
_SIZE_ROTATION_ENV = "VNX_EVENT_STREAM_MAX_BYTES"
# Hard upper bound (50 MB): auto-truncate with emergency archive to prevent
# lane blockage when the size rotation keeps failing. Deliberately higher than
# the rotation threshold so rotation handles the normal case first.
_SIZE_HARD_LIMIT_BYTES = 50 * 1024 * 1024
# Flag file written alongside the oversize NDJSON when a size rotation FAILS,
# keeping the still-oversize condition visible to the dispatcher and the
# operator dashboard (ADR-005 observability).
_OVERSIZE_FLAG_SUFFIX = ".oversize"


# ---------------------------------------------------------------------------
# Archive file helpers (OI-1944 C1)
#
# An archive entry is ``<id>.ndjson`` or, once compressed, ``<id>.ndjson.gz``.
# Every reader goes through these helpers so a compressed archive is as
# readable as a plain one. Plain wins when both exist: the plain file is the
# original, the ``.gz`` is a copy that may still be in flight.
# ---------------------------------------------------------------------------

ARCHIVE_SUFFIX = ".ndjson"
ARCHIVE_GZ_SUFFIX = ".ndjson.gz"


def archive_id(path: Union[str, Path]) -> str:
    """Dispatch id of an archive file name: both suffixes stripped."""
    name = Path(path).name
    for suffix in (ARCHIVE_GZ_SUFFIX, ARCHIVE_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def is_archive_file(path: Union[str, Path]) -> bool:
    """True for ``*.ndjson`` and ``*.ndjson.gz``."""
    name = Path(path).name
    return name.endswith(ARCHIVE_SUFFIX) or name.endswith(ARCHIVE_GZ_SUFFIX)


def resolve_archive_file(directory: Union[str, Path], dispatch_id: str) -> Optional[Path]:
    """``<dir>/<id>.ndjson`` if it exists, else ``<dir>/<id>.ndjson.gz``, else None."""
    directory = Path(directory)
    for suffix in (ARCHIVE_SUFFIX, ARCHIVE_GZ_SUFFIX):
        candidate = directory / f"{dispatch_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def open_archive_text(path: Union[str, Path]) -> IO[str]:
    """Open a plain or gzip archive file as UTF-8 text, chosen by suffix."""
    path = Path(path)
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def list_archive_files(directory: Union[str, Path], recursive: bool = False) -> List[Path]:
    """Archive files under ``directory``, one per id, plain preferred over ``.gz``.

    Sorted by file name. ``recursive`` walks sub-directories (terminal dirs);
    an id is deduplicated per containing directory.
    """
    directory = Path(directory)
    if not directory.exists():
        return []
    walker = directory.rglob if recursive else directory.glob
    chosen: Dict[tuple, Path] = {}
    for path in walker("*"):
        if not is_archive_file(path) or not path.is_file():
            continue
        key = (path.parent, archive_id(path))
        current = chosen.get(key)
        if current is None or (current.name.endswith(".gz") and not path.name.endswith(".gz")):
            chosen[key] = path
    return sorted(chosen.values(), key=lambda p: (str(p.parent), p.name))


def find_archive_file(directory: Union[str, Path], dispatch_id: str) -> Optional[Path]:
    """Locate the archive of ``dispatch_id`` anywhere under ``directory``.

    Exact id match wins over a substring match; plain wins over ``.gz``.
    """
    files = list_archive_files(directory, recursive=True)
    for path in files:
        if archive_id(path) == dispatch_id:
            return path
    for path in files:
        if dispatch_id in archive_id(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Per-run live streams (OI-2115)
#
# Every harness seat run, a review-gate seat as well as a plan-gate or
# deliberation panel seat, is dispatched on the one terminal ``plan-gate``.
# With one live file per terminal, seats that ran at the same time shared
# ``events/plan-gate.ndjson``: the second run's boundary archived and cleared
# the first run's stream, and its end-of-run teardown truncated what the first
# had written since. Measured 2026-10-16 on deepseek_gate for #2075 and #2076,
# five seconds apart: the archive of #2076 held only its
# ``provider_teardown_worktree`` event, so its final answer was gone.
#
# A per-run terminal keeps one live file per dispatch id,
# ``events/<terminal>/<dispatch_id>.ndjson``, so a run's boundary touches its
# own stream and nothing else. The archive layout does not change
# (``events/archive/<terminal>/<dispatch_id>.ndjson``), so an archive written
# before this change reads exactly as before.
#
# The headless lane had the same defect (OI-1869, OI-1895, OI-1930): every
# claude dispatch the door sends to it runs under the one target id
# ``ephemeral``, so parallel builders shared ``events/ephemeral.ndjson``.
# Measured 2026-10-16 in the fire logs of three parallel opus builders: the
# boundary guard archived and cleared a running builder's stream three times
# (``20261016-cr-b1-r2 -> 20261016-xd-d1-shadow`` and two more). That terminal
# is a per-run terminal too.
# ---------------------------------------------------------------------------

# The terminal every harness seat is dispatched under: provider_dispatch gets
# ``--terminal-id plan-gate`` from plan_gate_panel._make_default_dispatcher, and
# the claude seat runs with plan_gate_panel._PANEL_TERMINAL_ID.
HARNESS_SEAT_TERMINAL = "plan-gate"

# The terminal every dispatch on the headless claude lane runs under: the
# door's plan gives the leaseless claude lane this target id
# (dispatch_plan.compile_plan, D12) and the envelope writes the worker's events
# under it.
HEADLESS_LANE_TERMINAL = "ephemeral"

# Terminals whose live stream is one file per run. A terminal-pinned stream
# (``T{n}``) stays the single per-terminal ring buffer it always was.
PER_RUN_TERMINALS = frozenset({HARNESS_SEAT_TERMINAL, HEADLESS_LANE_TERMINAL})

# How often an append opens its stream again when it finds the file it holds
# removed (a per-run stream moved into its archive while the append waited on
# the lock). One reopen is the normal case; more means the stream is moved
# again and again under the writer, which is a fault worth raising.
_APPEND_REOPEN_ATTEMPTS = 8


def is_single_name(dispatch_id: str) -> bool:
    """A dispatch id that names one file in a directory and nothing else.

    The one rule for turning a dispatch id into a file name: the per-run live
    stream and the final-answer reader (``final_answer_verdict``) both use it.
    """
    value = (dispatch_id or "").strip()
    return bool(value) and value == dispatch_id and value not in (".", "..") \
        and "/" not in value and "\\" not in value and "\x00" not in value


def _stream_belongs_to(content: bytes, dispatch_id: str) -> bool:
    """True when *content* holds an event of *dispatch_id* and none of another dispatch.

    An event names its dispatch in ``dispatch_id``; one that names another,
    the empty id included, makes the stream shared. A line that is not a JSON
    object (a torn write) names no dispatch and decides nothing.
    """
    own = False
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or "dispatch_id" not in event:
            continue
        if event["dispatch_id"] != dispatch_id:
            return False
        own = True
    return own


def _replace_from_scratch(dest: Path, mode: int, *chunks: bytes) -> None:
    """Write *chunks* to a scratch file beside *dest* and move it onto *dest*.

    The scratch name comes from ``mkstemp``, unique to this writer: a name
    derived from *dest* alone is shared by every concurrent writer of it
    (OI-1486). The scratch takes *mode*, so the archive carries the permission
    bits ``archive()``'s ``copy2`` gives it. On any failure the scratch is
    removed and the error re-raised; *dest* is never half-written.
    """
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp_f:
            os.fchmod(tmp_f.fileno(), mode)
            for chunk in chunks:
                tmp_f.write(chunk)
            tmp_f.flush()
            os.fsync(tmp_f.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _events_dir() -> Path:
    """Resolve the events directory, failing loud on a read-only version store.

    OI-1179: the previous two-flag guard honored ``VNX_DATA_DIR`` ONLY when
    ``VNX_DATA_DIR_EXPLICIT=1`` was also set, so an operator who set a
    legitimate ``VNX_DATA_DIR`` (worktree/CI override) was silently ignored and
    the fallback landed on the read-only pinned version store
    ``~/.vnx-system/versions/<v>/.vnx-data`` — where the append then failed
    with a permission error.

    Resolution order is now env-first and shared with
    ``headless_dispatch_daemon._default_data_dir`` via
    ``data_dir_resolution.resolve_data_dir_fail_loud``:
    1. VNX_DATA_DIR set           → VNX_DATA_DIR/events
    2. VNX_PROJECT_ID set         → $HOME/.vnx-data/<project_id>/events  (central ledger)
    3. Fallback → canonical resolver (vnx_paths.resolve_paths()["VNX_DATA_DIR"])

    Every step is refused with a ``DataDirResolutionError`` naming the chain if
    it would land under ``~/.vnx-system/versions/`` — a read-only version store
    is never a valid data dir.
    """
    from data_dir_resolution import resolve_data_dir_fail_loud
    return resolve_data_dir_fail_loud() / "events"


class EventStore:
    """NDJSON event store with per-terminal files and file locking."""

    def __init__(
        self,
        events_dir: Optional[Path] = None,
        rotation_threshold_bytes: Optional[int] = None,
    ) -> None:
        self._events_dir = events_dir or _events_dir()
        # None means "resolve at write time" so the module default stays
        # patchable in tests even after the store is constructed.
        self._rotation_threshold_bytes = rotation_threshold_bytes
        self._sequences: Dict[str, int] = {}
        # Last dispatch_id appended per terminal (OI-878). Used to detect a
        # dispatch boundary on the write side so a live file that still holds a
        # previous dispatch's events (because the end-of-dispatch teardown never
        # ran) is archived + truncated before the new dispatch's events mix in.
        self._boundary_terminal: Dict[str, str] = {}

    def _terminal_path(self, terminal: str) -> Path:
        return self._events_dir / f"{terminal}.ndjson"

    @staticmethod
    def _run_id(terminal: str, dispatch_id: Optional[str]) -> Optional[str]:
        """The run *terminal* keeps a stream of its own for, or None for the shared stream.

        A per-run terminal keeps one for a dispatch id that names one file. Any
        other terminal, and a per-run terminal without such an id (an event
        that names no dispatch), keeps the one stream ``<terminal>.ndjson``.
        """
        if terminal in PER_RUN_TERMINALS and dispatch_id and is_single_name(dispatch_id):
            return dispatch_id
        return None

    def _stream_path(self, terminal: str, run_id: Optional[str] = None) -> Path:
        if run_id:
            return self._events_dir / terminal / f"{run_id}{ARCHIVE_SUFFIX}"
        return self._terminal_path(terminal)

    @staticmethod
    def _stream_key(terminal: str, run_id: Optional[str] = None) -> str:
        """The key of a stream's sequence counter: the terminal, or ``<terminal>/<run>``."""
        return f"{terminal}/{run_id}" if run_id else terminal

    def live_path(self, terminal: str, dispatch_id: Optional[str] = None) -> Path:
        """The live file *terminal*'s events of *dispatch_id* are appended to.

        ``events/<terminal>/<dispatch_id>.ndjson`` for a per-run terminal,
        ``events/<terminal>.ndjson`` for every other terminal (the dispatch id
        then changes nothing).
        """
        return self._stream_path(terminal, self._run_id(terminal, dispatch_id))

    def _next_sequence(self, terminal: str) -> int:
        seq = self._sequences.get(terminal, 0) + 1
        self._sequences[terminal] = seq
        return seq

    def _rotation_threshold(self) -> int:
        """Resolve the size-rotation threshold.

        Precedence: explicit ctor arg > VNX_EVENT_STREAM_MAX_BYTES env var >
        module default (_SIZE_ROTATION_BYTES). The module default stays
        patchable so tests can construct a store without an explicit value and
        still lower the threshold.
        """
        if self._rotation_threshold_bytes is not None:
            return int(self._rotation_threshold_bytes)
        env = os.environ.get(_SIZE_ROTATION_ENV)
        if env:
            try:
                return int(env)
            except ValueError:
                logger.warning(
                    "event_store: invalid %s=%r — using default %d bytes",
                    _SIZE_ROTATION_ENV,
                    env,
                    _SIZE_ROTATION_BYTES,
                )
        return _SIZE_ROTATION_BYTES

    def _oversize_flag_path(self, terminal: str, run_id: Optional[str] = None) -> Path:
        return self._stream_path(terminal, run_id).with_suffix(_OVERSIZE_FLAG_SUFFIX)

    def _write_oversize_flag(self, terminal: str, run_id: Optional[str] = None) -> None:
        """Persist the still-oversize marker after a failed rotation."""
        try:
            self._oversize_flag_path(terminal, run_id).write_text(
                f"oversize:{self._stream_key(terminal, run_id)}:rotation-failed:"
                f"{datetime.now(timezone.utc).isoformat()}\n"
            )
        except OSError:
            pass

    def _remove_oversize_flag(self, terminal: str, run_id: Optional[str] = None) -> None:
        try:
            self._oversize_flag_path(terminal, run_id).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _size_rotation_archive_id() -> str:
        """Unique archive id for a size rotation.

        Uses a timestamp namespace (like the existing ``emergency-*`` backstop)
        so it never collides with the per-dispatch teardown archive
        ``{dispatch_id}.ndjson`` — the teardown writes that same filename later
        and would otherwise overwrite the rotation's events.
        """
        return "size-rotation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    def _rotate_on_size(self, terminal: str, run_id: Optional[str] = None) -> None:
        """Rotate the live stream once it exceeds the size threshold.

        Archives the current content to
        ``events/archive/{terminal}/size-rotation-{ts}.ndjson`` (the existing
        per-dispatch archive layout, not a second schema) and truncates the live
        file to 0 bytes. The copy and the truncate happen under a single
        LOCK_EX on the live file, so a concurrent appender that is blocked on
        the lock writes into the fresh file afterwards — it can never lose an
        event to a truncate that lands between the archive read and the clear.
        *run_id* names a per-run stream; its rotation lands in the same
        terminal archive dir.

        Never raises: a failed rotation logs ERROR (visible) and leaves the
        live file in place, so a broken log rotation never blocks the dispatch.
        The hard-limit backstop still bounds worst-case growth.
        """
        path = self._stream_path(terminal, run_id)
        if not path.exists():
            return
        threshold = self._rotation_threshold()
        rotated_size: Optional[int] = None
        tmp: Optional[Path] = None
        try:
            with open(path, "ab+") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    # Double-checked under the lock: another process may have
                    # rotated while we waited, dropping the size back below the
                    # threshold.
                    size = os.fstat(f.fileno()).st_size
                    if size <= threshold:
                        return
                    dest = (
                        self.archive_dir(terminal)
                        / f"{self._size_rotation_archive_id()}.ndjson"
                    )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".tmp")
                    f.seek(0)
                    with open(tmp, "wb") as tmp_f:
                        shutil.copyfileobj(f, tmp_f)
                    os.replace(tmp, dest)
                    tmp = None
                    f.seek(0)
                    f.truncate(0)
                    f.flush()
                    rotated_size = size
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:  # vnx-silent-except: a broken rotation must never block the dispatch — fail visibly and continue
            logger.error(
                "event_store: size rotation failed for %s (threshold %d bytes): %s",
                path,
                threshold,
                exc,
                exc_info=True,
            )
            self._write_oversize_flag(terminal, run_id)
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return

        self._sequences.pop(self._stream_key(terminal, run_id), None)
        self._remove_oversize_flag(terminal, run_id)
        logger.info(
            "event_store: rotated %s (%d bytes) over %d-byte threshold",
            path,
            rotated_size,
            threshold,
        )

    def _rotate_at_dispatch_boundary(self, terminal: str, new_dispatch_id: str) -> None:
        """Enforce the per-dispatch ring buffer on the write side (OI-878).

        A new dispatch appending to a live file that still holds a PREVIOUS
        dispatch's events means the teardown that should have archived + cleared
        at the end of that dispatch never ran (the door's provider-lane envelope
        path never calls ``_emit_governance``, so the END-of-dispatch archive is
        skipped there; the adapter only clears at the START of the next dispatch).

        When that happens, archive the previous dispatch's events under its own
        dispatch_id and truncate the live file so the new dispatch starts a
        fresh ring buffer.  This guarantees the archive entry exists under the
        dispatch that produced the events, instead of the events being stranded
        in the live file until a manual rescue.

        Idempotent and cheap: the boundary is checked once per (store instance,
        terminal, dispatch_id) via the in-memory tracker, and the live file is
        re-read only when the boundary actually changes.  Never raises — a
        rotation failure degrades to the pre-fix behavior (append to a mixed
        stream) rather than breaking the event pipeline.
        """
        if not new_dispatch_id:
            return
        if self._boundary_terminal.get(terminal) == new_dispatch_id:
            return
        self._boundary_terminal[terminal] = new_dispatch_id

        path = self._terminal_path(terminal)
        if not path.exists():
            return
        try:
            if path.stat().st_size == 0:
                return
        except OSError:
            return
        try:
            last = self.last_event(terminal)
        except Exception:  # vnx-silent-except: rotation is best-effort; a read failure must never break the append path
            last = None
        if last is None:
            return
        prev_dispatch_id = last.get("dispatch_id") or ""
        if not prev_dispatch_id or prev_dispatch_id == new_dispatch_id:
            return
        try:
            # The shared stream itself, never a run's own stream: on a per-run
            # terminal this is the stream of events that name no usable id.
            self._copy_to_archive(self._terminal_path(terminal), terminal, prev_dispatch_id)
            self._reset_stream(terminal)
            self._sequences.pop(terminal, None)
            logger.warning(
                "event_store: dispatch boundary %s -> %s — archived previous "
                "dispatch events and cleared live file (end-teardown had not run)",
                prev_dispatch_id,
                new_dispatch_id,
            )
        except Exception:  # vnx-silent-except: rotation failure degrades to appending to the mixed stream rather than raising into the dispatch
            logger.warning(
                "event_store: dispatch boundary rotation failed for %s "
                "(prev=%s new=%s) — previous events left in place",
                terminal,
                prev_dispatch_id,
                new_dispatch_id,
                exc_info=True,
            )

    def append(
        self,
        terminal: str,
        event: "Union[Dict[str, Any], CanonicalEvent]",
        dispatch_id: Optional[str] = None,
    ) -> None:
        """Append a single event as an atomic NDJSON line.

        Accepts both legacy dict events and CanonicalEvent instances.
        Uses LOCK_EX for write safety. The line is written in a single write()
        call including the trailing newline to prevent partial reads.

        dispatch_id precedence: explicit kwarg (when not None) wins over the
        event's own dispatch_id field. Omitting the kwarg (None) falls back to
        the event's field. Fixes OI-1349.

        A per-run terminal (``PER_RUN_TERMINALS``) appends to the stream of
        the effective dispatch id (``live_path``); the envelope still names the
        terminal itself, so a reader binds an event to its terminal as before.
        """
        from canonical_event import CanonicalEvent as _CE, EventShapeError  # noqa: F401 (used below)

        if isinstance(event, _CE):
            event.validate_shape()  # raises EventShapeError on schema violations
            effective_dispatch_id = dispatch_id if dispatch_id is not None else event.dispatch_id
        else:
            effective_dispatch_id = dispatch_id if dispatch_id is not None else event.get("dispatch_id", "")

        run_id = self._run_id(terminal, effective_dispatch_id)
        path = self._stream_path(terminal, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # OI-878: enforce the per-dispatch ring-buffer boundary on the write
        # side.  When a new dispatch's first event arrives and the live file
        # still holds a PREVIOUS dispatch's events (the teardown that should
        # have archived + cleared at the end of that dispatch never ran), archive
        # the previous dispatch's events under its own dispatch_id and truncate
        # the live file before appending. Runs BEFORE the envelope is built so a
        # rotation's sequence reset is visible to this event's sequence number.
        # A per-run stream holds one dispatch by construction: it has no boundary.
        if run_id is None:
            self._rotate_at_dispatch_boundary(terminal, effective_dispatch_id)

        envelope = self._envelope(
            event, terminal, effective_dispatch_id,
            self._next_sequence(self._stream_key(terminal, run_id)),
        )
        self._append_line(path, json.dumps(envelope, separators=(",", ":")) + "\n")

        # Size-based rotation: once the live stream exceeds the rotation
        # threshold, archive it to the per-dispatch archive dir and truncate to
        # 0 so the file can never grow unbounded. This replaces the old log-only
        # "operator intervention recommended" warning (OI-858) that fired every
        # dispatch and changed nothing.
        self._rotate_on_size(terminal, run_id)
        self._enforce_hard_limit(terminal, run_id)

    @staticmethod
    def _envelope(
        event: "Union[Dict[str, Any], CanonicalEvent]", terminal: str, dispatch_id: str, sequence: int,
    ) -> Dict[str, Any]:
        """The NDJSON envelope ``append`` writes for *event*."""
        from canonical_event import CanonicalEvent as _CE  # noqa: PLC0415

        if isinstance(event, _CE):
            return {
                "type": event.event_type,
                "timestamp": event.timestamp,
                "dispatch_id": dispatch_id,
                "terminal": terminal,
                "sequence": sequence,
                "data": event.data,
                "observability_tier": event.observability_tier,
                "event_id": event.event_id,
                "provider": event.provider,
                "terminal_id": event.terminal_id,
                "provider_meta": event.provider_meta,
            }
        # Legacy dict path — default tier 2 (buffered) for backwards compat
        return {
            "type": event.get("type", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "dispatch_id": dispatch_id,
            "terminal": terminal,
            "sequence": sequence,
            "data": event.get("data", event),
            "observability_tier": int(event.get("observability_tier", 2)),
        }

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        """Append *line* under LOCK_EX to the file that is at *path* once the lock is held.

        The line is written in a single write() call including the trailing
        newline. A per-run stream is removed when it moves into its archive
        (``_move_to_archive``). An append that opened the file before and then
        waited on the lock would write into the removed file and the event
        would be lost; it finds no link left and opens the path again, which
        starts a fresh stream.
        """
        for _ in range(_APPEND_REOPEN_ATTEMPTS):
            with open(path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    if os.fstat(f.fileno()).st_nlink == 0:
                        continue
                    f.write(line)
                    f.flush()
                    return
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        raise OSError(
            f"event_store: {path} was removed {_APPEND_REOPEN_ATTEMPTS} times under one append"
        )

    def _enforce_hard_limit(self, terminal: str, run_id: Optional[str]) -> None:
        """Hard upper bound: emergency archive + truncate past ``_SIZE_HARD_LIMIT_BYTES``.

        If the file has grown past the hard limit (50 MB), auto-truncate with an
        emergency archive so the lane doesn't block. This must work without any
        teardown — the write itself is the gate. The emergency archive uses a
        synthetic dispatch_id so the events are not lost, just moved out of the
        live stream.
        """
        path = self._stream_path(terminal, run_id)
        try:
            if path.stat().st_size <= _SIZE_HARD_LIMIT_BYTES:
                return
        except OSError:
            return
        emergency_id = f"emergency-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        logger.error(
            "event_store: HARD LIMIT (%d bytes) exceeded for %s — "
            "emergency archive+truncate to prevent lane blockage (OI-858)",
            _SIZE_HARD_LIMIT_BYTES,
            path,
        )
        try:
            self._copy_to_archive(path, terminal, emergency_id)
        except Exception as _arch_exc:
            logger.error(
                "event_store: emergency archive failed for %s: %s",
                self._stream_key(terminal, run_id),
                _arch_exc,
            )
        # The truncate runs AFTER the archive so the archive can read the full
        # file before truncation; it re-counts sequences from 1.
        try:
            self._reset_stream(terminal, run_id)
        except OSError:
            pass

    def tail(
        self, terminal: str, since: Optional[str] = None, *, dispatch_id: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield events since a timestamp (ISO 8601 string).

        Uses LOCK_SH for read safety. Events are yielded in file order.
        If since is None, all events are returned. *dispatch_id* selects the
        run's own stream on a per-run terminal (``live_path``).
        """
        path = self.live_path(terminal, dispatch_id)
        try:
            f = open(path, "r", encoding="utf-8")
        except FileNotFoundError:
            return

        with f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                for raw_line in f:
                    raw_line = raw_line.rstrip("\n")
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        logger.warning("event_store: malformed line in %s (skipped)", path)
                        continue

                    if since and event.get("timestamp", "") <= since:
                        continue

                    # Backfill tier for events written before observability_tier was added
                    if "observability_tier" not in event:
                        event["observability_tier"] = 2

                    yield event
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def run_ids(self, terminal: str) -> List[str]:
        """Dispatch ids of the runs of per-run terminal *terminal* with a live stream, sorted.

        A run's stream is in this list from its first event until its teardown
        moves it into its archive. Empty for every other terminal.
        """
        if terminal not in PER_RUN_TERMINALS:
            return []
        directory = self._events_dir / terminal
        if not directory.is_dir():
            return []
        ids = (archive_id(path) for path in directory.glob(f"*{ARCHIVE_SUFFIX}") if path.is_file())
        return sorted(run_id for run_id in ids if self._run_id(terminal, run_id) == run_id)

    def tail_lane(self, lane: str, since: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Yield the live events of lane *lane* since a timestamp, as the dashboard shows a lane.

        A terminal-pinned lane is its one stream (``tail``). A per-run
        terminal's lane is its shared stream and every run's own live stream,
        in timestamp order: the view the one shared file gave before each run
        got a stream of its own (OI-2115, OI-1869).
        """
        if lane not in PER_RUN_TERMINALS:
            yield from self.tail(lane, since)
            return
        events = list(self.tail(lane, since))
        for run_id in self.run_ids(lane):
            events.extend(self.tail(lane, since, dispatch_id=run_id))
        events.sort(key=lambda event: str(event.get("timestamp") or ""))
        yield from events

    def archive_dir(self, terminal: str) -> Path:
        """Return the archive directory for a terminal."""
        return self._events_dir / "archive" / terminal

    def resolve_archive(self, terminal: str, dispatch_id: str) -> Optional[Path]:
        """Archive file of a dispatch in a terminal dir: plain, else ``.gz``."""
        return resolve_archive_file(self.archive_dir(terminal), dispatch_id)

    def list_archive_ids(self, terminal: str) -> List[Path]:
        """Archive files of a terminal, one per dispatch id, plain preferred."""
        return list_archive_files(self.archive_dir(terminal))

    def archive(self, terminal: str, dispatch_id: str) -> Optional[Path]:
        """Copy current event file to archive before clearing.

        Returns the archive path on success, None if nothing to archive.

        On a per-run terminal the run's own stream moves into its archive:
        the archive it already has is extended, never replaced, and the live
        file is removed in the same locked step (``_move_to_archive``), so a
        second call archives nothing twice and a late event of the run is
        added instead of replacing what was archived before it.
        """
        run_id = self._run_id(terminal, dispatch_id)
        if run_id is not None:
            return self._archive_run_stream(terminal, run_id, owned_only=False)
        return self._copy_to_archive(self._terminal_path(terminal), terminal, dispatch_id)

    def _copy_to_archive(self, event_file: Path, terminal: str, archive_id: str) -> Optional[Path]:
        """Copy *event_file* to ``<archive_dir(terminal)>/<archive_id>.ndjson``, replacing it."""
        if not event_file.exists() or event_file.stat().st_size == 0:
            return None

        archive_path = self.archive_dir(terminal)
        archive_path.mkdir(parents=True, exist_ok=True)
        dest = archive_path / f"{archive_id}.ndjson"
        shutil.copy2(str(event_file), str(dest))
        logger.info("event_store: archived %s -> %s", event_file, dest)
        return dest

    def clear(
        self,
        terminal: str,
        archive_dispatch_id: Optional[str] = None,
        *,
        dispatch_id: Optional[str] = None,
    ) -> None:
        """Truncate the event file for a terminal (new dispatch clears old events).

        If archive_dispatch_id is provided and the file has content, the events
        are archived to .vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
        before truncation.

        Also resets the sequence counter and removes any oversize flag file.

        On a per-run terminal the stream is the run's own: *dispatch_id*, or
        else *archive_dispatch_id*, names it. Its events belong to that run
        and nowhere else, so they move into that run's archive
        (``archive``) rather than being dropped, and another run's stream is
        never touched. Without either id it is the terminal's shared stream,
        as on every other terminal; there *dispatch_id* changes nothing.
        """
        run_id = self._run_id(terminal, dispatch_id or archive_dispatch_id)
        if run_id is not None:
            self._archive_run_stream(terminal, run_id, owned_only=False)
            return
        if archive_dispatch_id:
            self._copy_to_archive(self._terminal_path(terminal), terminal, archive_dispatch_id)
        self._reset_stream(terminal)

    def _reset_stream(self, terminal: str, run_id: Optional[str] = None) -> None:
        """Truncate a live stream, reset its counters and remove its oversize flag."""
        path = self._stream_path(terminal, run_id)
        key = self._stream_key(terminal, run_id)
        self._sequences.pop(key, None)
        self._boundary_terminal.pop(key, None)

        # Remove any oversize flag file on clean teardown
        self._remove_oversize_flag(terminal, run_id)

        if not path.exists():
            return

        with open(path, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.truncate(0)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def archive_completed_stream(self, terminal: str, dispatch_id: str) -> Optional[Path]:
        """Archive and clear the live stream at the end of *dispatch_id*'s run, when it is its alone.

        For a lane whose run ends without a teardown that archives: the
        plan-gate's claude seat writes here through SubprocessAdapter, and
        neither the adapter nor dispatch_govern archives, so the archive came
        into being only when the NEXT dispatch on the terminal rotated the
        boundary. A reader at the end of the run (R6, OI-2039) found none.

        Only a stream that holds this dispatch's events and no other
        dispatch's is archived. A stream another dispatch writes into as well
        (OI-1032: parallel dispatches on one terminal-pinned stream share its
        ring buffer) is left untouched for the boundary guard, as before:
        archiving it would file the other dispatch's events under this id, and
        truncating it would drop that dispatch's live events. A per-run
        stream (OI-2115) holds another dispatch's event only through a write
        that bypassed this store; it is left in place too.

        An archive this dispatch already has (a parallel dispatch's boundary
        guard archived the head of its stream) is extended, never replaced. The
        check, the copy and the truncate run under one LOCK_EX on the live
        file, the lock every appender takes, so no event lands between the copy
        and the truncate; the archive is written to a per-writer scratch file
        and moved into place (``_replace_from_scratch``). A per-run stream's
        live file is removed in that same step. Never raises: a failure is
        logged, leaves no scratch file, and leaves the live file for the
        boundary guard. Returns the archive path, or None when nothing was
        archived.
        """
        if not dispatch_id:
            return None
        run_id = self._run_id(terminal, dispatch_id)
        path = self._stream_path(terminal, run_id)
        try:
            if run_id is not None:
                dest = self._archive_run_stream(terminal, run_id, owned_only=True)
            else:
                dest = self._move_to_archive(
                    path, self.archive_dir(terminal) / f"{dispatch_id}{ARCHIVE_SUFFIX}", dispatch_id,
                    owned_only=True, remove_live=False,
                )
        except Exception as exc:  # vnx-silent-except: an end-of-run archive must never break the run; logged, live file left for the boundary guard
            logger.error(
                "event_store: end-of-run archive failed for %s (dispatch %s): %s",
                path, dispatch_id, exc, exc_info=True,
            )
            return None
        if dest is None:
            return None
        if run_id is None:
            self._sequences.pop(terminal, None)
            self._boundary_terminal.pop(terminal, None)
            self._remove_oversize_flag(terminal)
        logger.info("event_store: end-of-run archived %s -> %s", path, dest)
        return dest

    def _archive_run_stream(self, terminal: str, run_id: str, *, owned_only: bool) -> Optional[Path]:
        """Move per-run stream *run_id* into its archive and reset its counters.

        Raises what ``_move_to_archive`` raises; the live file then stays.
        """
        dest = self._move_to_archive(
            self._stream_path(terminal, run_id),
            self.archive_dir(terminal) / f"{run_id}{ARCHIVE_SUFFIX}",
            run_id,
            owned_only=owned_only,
            remove_live=True,
        )
        if dest is not None:
            self._sequences.pop(self._stream_key(terminal, run_id), None)
            self._remove_oversize_flag(terminal, run_id)
            logger.info("event_store: archived run stream %s/%s -> %s", terminal, run_id, dest)
        return dest

    @staticmethod
    def _move_to_archive(
        path: Path, dest: Path, dispatch_id: str, *, owned_only: bool, remove_live: bool,
    ) -> Optional[Path]:
        """Add the live stream at *path* to the archive *dest* and empty it, under one lock.

        The archive *dest* already holds is extended, never replaced; a head
        without a final newline is not glued to the tail. The read, the
        archive write (a per-writer scratch file moved into place) and the
        truncate run under one LOCK_EX on the live file, the lock every
        appender takes, so no event lands between the copy and the truncate.
        *owned_only* leaves a stream that holds no event of *dispatch_id*, or
        an event of another dispatch, untouched. *remove_live* also removes
        the emptied live file (a per-run stream); an append that waited on the
        lock then opens a fresh one (``_append_line``), and a second mover that
        waited finds no link left and archives nothing twice.

        Returns *dest*, or None when nothing was archived. Raises on a failed
        write, leaving the live file as it was and no scratch file behind.
        """
        try:
            f = open(path, "rb+")
        except FileNotFoundError:
            return None
        with f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                if os.fstat(f.fileno()).st_nlink == 0:
                    return None
                content = f.read()
                if owned_only and not _stream_belongs_to(content, dispatch_id):
                    return None
                if content:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    head = dest.read_bytes() if dest.is_file() else b""
                    if head and not head.endswith(b"\n"):
                        head += b"\n"
                    live_mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
                    _replace_from_scratch(dest, live_mode, head, content)
                    f.seek(0)
                    f.truncate(0)
                    f.flush()
                if remove_live:
                    path.unlink(missing_ok=True)
                return dest if content else None
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def oversize_flags(self) -> list[Path]:
        """Return paths to all oversize flag files currently present.

        Each flag file marks a terminal whose event file exceeded the size-
        rotation threshold but whose rotation FAILED — i.e. the live file is
        still oversize and needs manual attention. A successful rotation removes
        the flag. The dispatcher can call this to surface the condition in
        ``vnx pool status`` without tailing the log stream (OI-858 consumer).
        A per-run stream's flag sits beside it, under ``events/<terminal>/``.
        """
        if not self._events_dir.exists():
            return []
        flags = list(self._events_dir.glob(f"*{_OVERSIZE_FLAG_SUFFIX}"))
        for terminal in sorted(PER_RUN_TERMINALS):
            flags.extend((self._events_dir / terminal).glob(f"*{_OVERSIZE_FLAG_SUFFIX}"))
        return sorted(flags)

    def event_count(self, terminal: str, dispatch_id: Optional[str] = None) -> int:
        """Count events in the NDJSON file for a terminal.

        *dispatch_id* selects the run's own stream on a per-run terminal.
        """
        path = self.live_path(terminal, dispatch_id)
        try:
            f = open(path, "r", encoding="utf-8")
        except FileNotFoundError:
            return 0
        count = 0
        with f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def last_event(self, terminal: str, dispatch_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the last event for a terminal, or None.

        *dispatch_id* selects the run's own stream on a per-run terminal
        (``live_path``); on every other terminal it changes nothing.
        """
        path = self.live_path(terminal, dispatch_id)
        try:
            f = open(path, "r", encoding="utf-8")
        except FileNotFoundError:
            return None
        last = None
        with f:
            for raw_line in f:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue
                try:
                    last = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
        return last

    @staticmethod
    def _is_agent_event(event: Dict[str, Any]) -> bool:
        """True if a record is an agent-conversation event (CanonicalEvent
        envelope), not a domain/system stream record.

        Agent events carry ``type`` + ``dispatch_id`` + ``sequence``; system
        streams (decisions, provider_costs, schema_migrations, ...) live in the
        same directory but use domain-specific shapes without those fields.
        """
        return (
            "type" in event
            and "dispatch_id" in event
            and "sequence" in event
        )

    def list_lanes(self) -> list[Dict[str, Any]]:
        """Discover agent-conversation lanes from the events directory.

        A "lane" is any ``{id}.ndjson`` whose last record is an agent event.
        Returns one dict per lane with id, event_count, last_timestamp, and the
        provider/dispatch_id of the most recent event — so the dashboard can
        render whatever lanes actually produced events (terminals T0-T3,
        provider lanes, tmux-spawn dispatch ids) instead of a fixed set.

        Domain/system streams (decisions.ndjson, provider_costs.ndjson, ...) are
        skipped via the agent-envelope check.

        A per-run terminal is one lane over its shared stream and every run's
        own live stream (``tail_lane``): the counts add up, the most recent
        event names the provider and dispatch, and ``runs`` lists each live
        run's stream the same way, most recent first, so a reader can follow
        one run (``tail(terminal, dispatch_id=...)``).
        """
        if not self._events_dir.exists():
            return []
        lane_ids = {path.stem for path in self._events_dir.glob("*.ndjson")} | PER_RUN_TERMINALS
        lanes = [lane for lane in map(self._lane_summary, sorted(lane_ids)) if lane is not None]
        lanes.sort(key=lambda x: x.get("last_timestamp") or "", reverse=True)
        return lanes

    def _stream_summary(self, terminal: str, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Count and most recent event of one live stream, or None when it holds no agent event."""
        last = self.last_event(terminal, dispatch_id=run_id)
        if last is None or not self._is_agent_event(last):
            return None
        return {
            "event_count": self.event_count(terminal, dispatch_id=run_id),
            "last_timestamp": last.get("timestamp"),
            "provider": last.get("provider"),
            "dispatch_id": last.get("dispatch_id"),
        }

    def _lane_summary(self, lane: str) -> Optional[Dict[str, Any]]:
        """The ``list_lanes`` entry of *lane*, or None when it has no agent event."""
        runs = [summary for summary in (self._stream_summary(lane, run_id) for run_id in self.run_ids(lane))
                if summary is not None]
        shared = self._stream_summary(lane)
        streams = runs + ([shared] if shared is not None else [])
        if not streams:
            return None
        latest = max(streams, key=lambda summary: str(summary.get("last_timestamp") or ""))
        entry: Dict[str, Any] = {"id": lane, **latest, "event_count": sum(s["event_count"] for s in streams)}
        if lane in PER_RUN_TERMINALS:
            entry["runs"] = sorted(runs, key=lambda summary: str(summary.get("last_timestamp") or ""), reverse=True)
        return entry
