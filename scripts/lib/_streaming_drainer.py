#!/usr/bin/env python3
"""_streaming_drainer.py — Shared NDJSON streaming drainer mixin for all provider adapters.

All provider adapters (Codex, Gemini, LiteLLM, Ollama) compose this mixin to
drain their subprocess stdout streams into CanonicalEvent objects with Tier-1
observability. Subclasses define `_normalize(raw_chunk)` to map provider-specific
event shapes; the mixin handles buffering, timeouts, error recovery, and EventStore writes.

BILLING SAFETY: No Anthropic SDK imports. No external network calls.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from event_store import EventStore

from canonical_event import CanonicalEvent, VALID_EVENT_TYPES

logger = logging.getLogger(__name__)

# Sentinel object used to signal the producer thread has finished
_SENTINEL = object()

# Tier-1 means live per-event streaming (full observability)
_STREAMING_TIER = 1

# Canonical event types that represent successful completion
_COMPLETE_TYPES = frozenset({"complete", "result"})

# Default bounded queue size — large enough to buffer burst, small enough to apply backpressure
_DEFAULT_QUEUE_MAXSIZE = 256

# Stall floor for the per-chunk timeout, as a fraction of the total deadline.
#
# OI-903: a kimi worker with deadline_seconds=3600 was SIGTERM'd at 1890.6s by the
# 1200s chunk timeout after a legitimate 1200s silent thinking phase — the chunk
# timeout sat FAR below the deadline, so it (not the deadline) became the binding
# constraint. The chunk timeout measures silence, so a fixed value far under a long
# deadline kills legitimate long-think work and makes the deadline decorative.
#
# The relationship is now explicit: the effective chunk timeout is clamped into the
# band [total_deadline * _STALL_FLOOR_FRACTION, total_deadline]:
#   - never above the total deadline (a stall longer than the whole budget IS the
#     deadline),
#   - never below half the deadline (a worker that produces nothing for half its
#     budget is genuinely stalled; anything shorter stays legitimate).
#
# Explicit env-var overrides (VNX_CHUNK_TIMEOUT and the per-provider
# VNX_*_STALL_THRESHOLD vars) retain top precedence and are never floored — an
# operator who sets a stall threshold deliberately keeps it.
_STALL_FLOOR_FRACTION = 0.5

# OI-2066: the structured marker a drainer error event carries when the TOTAL
# deadline is what stopped the run — the plain deadline kill, and a stall
# window the deadline cut short. A kill on the chunk (stall) timeout alone
# never carries it. The keys are the ones the governed report carries
# (provider_dispatch._build_frontmatter), so the review-gate runner reads the
# lane's own statement instead of inferring a deadline from a duration.
DEADLINE_EXCEEDED_KEY = "deadline_exceeded"
DEADLINE_SECONDS_KEY = "deadline_seconds"

# OI-2128: how long the post-drain check waits for the child's exit code once
# its stream ended without a complete event.
#
# The kernel closes a dying child's stdout BEFORE the child becomes reapable, so
# EOF on the pipe does not mean the exit code is there yet. A bare poll() at
# that moment returns None, and the old check read None as "nothing to report":
# a provider that crashed (SIGKILL, exit 1) without a complete event ended in
# silence. Measured 2026-10-18: under CPU load the poll at EOF saw None in 9 of
# 100 SIGKILLed children; in the xdist shadow the post-drain poll saw it too
# and the crash produced no error event. The check now waits for the code.
#
# A child that is still running when the wait runs out closed its output and
# kept going: that is not a clean end either. The drainer kills it (the same
# process-group kill as its stall and deadline paths, so the caller is never
# left holding a live child behind a finished stream) and emits an error event.
# The reason deliberately carries neither "timeout" nor "deadline": the spawns
# read those words as a stall or deadline kill, and this is neither.
_EXIT_CODE_WAIT_SECONDS = 5.0


def deadline_exceeded_seconds(event_data: Any) -> Optional[float]:
    """The deadline a drainer error event says stopped the run, or None.

    None for any event that does not carry the marker exactly as
    :func:`_make_error_event` writes it — a ``True`` flag and a number — so a
    malformed or provider-shaped event can never read as a deadline kill.
    """
    if not isinstance(event_data, dict) or event_data.get(DEADLINE_EXCEEDED_KEY) is not True:
        return None
    seconds = event_data.get(DEADLINE_SECONDS_KEY)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    return float(seconds)


def coerce_chunk_stall(chunk_timeout: float, total_deadline: float) -> float:
    """Return *chunk_timeout* coerced into the stall band relative to *total_deadline*.

    The returned value satisfies::

        min(total_deadline * _STALL_FLOOR_FRACTION, chunk_timeout)
        <= value <= total_deadline

    when total_deadline > 0 and chunk_timeout > 0; otherwise *chunk_timeout* is
    returned unchanged (a non-positive deadline means the loop below fires
    immediately anyway, and a non-positive chunk timeout is an explicit
    "no stall tolerance" signal). See the module constant block for the
    rationale (OI-903).
    """
    if total_deadline <= 0 or chunk_timeout <= 0:
        return chunk_timeout
    chunk_timeout = min(chunk_timeout, total_deadline)
    return max(chunk_timeout, total_deadline * _STALL_FLOOR_FRACTION)


class StreamingDrainerMixin:
    """Mixin that drains provider subprocess stdout into CanonicalEvent objects.

    Compose this mixin into any provider adapter class. The subclass must set
    `provider_name` and implement `_normalize(raw_chunk)`.

    Usage::

        class MyAdapter(StreamingDrainerMixin, ProviderAdapter):
            provider_name = "myprovider"
            provider_observability_tier = 1  # declare adapter's observability tier

            def _normalize(self, raw_chunk: dict) -> CanonicalEvent:
                ...  # map raw provider event -> CanonicalEvent

            def run(self, process, terminal_id, dispatch_id, event_store):
                for event in self.drain_stream(process, terminal_id, dispatch_id, event_store):
                    # handle event
    """

    # Subclasses override these
    provider_name: str = "unknown"
    # Observability tier this adapter produces via the drainer (W7-G).
    # Tier 1 = live per-event streaming (full observability).
    # Subclasses that compose this mixin produce Tier-1 events by default.
    provider_observability_tier: int = 1

    def _normalize(self, raw_chunk: Dict[str, Any]) -> CanonicalEvent:
        """Map a raw provider JSON chunk to a CanonicalEvent.

        Subclasses must override this. The returned event's observability_tier
        will be overwritten to _STREAMING_TIER (1) by the mixin.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _normalize(raw_chunk)"
        )

    def drain_stream(
        self,
        process: subprocess.Popen,
        terminal_id: str,
        dispatch_id: str,
        event_store: Optional[Any] = None,
        chunk_timeout: float = 300.0,
        total_deadline: float = 900.0,
        _queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        raw_tee_path: Optional[Path] = None,
    ) -> Iterator[CanonicalEvent]:
        """Drain process stdout line-by-line, yielding CanonicalEvents.

        A producer thread reads from subprocess stdout and pushes parsed events
        onto a bounded queue. The caller iterates this generator to consume them.
        The bounded queue caps memory growth and creates backpressure: when the
        queue is full, the producer thread blocks, preventing unbounded buffering.

        On chunk_timeout (no output for N seconds), or total_deadline exceeded,
        the process is killed and a synthetic error CanonicalEvent is emitted.

        If the process exits non-zero without emitting a complete/result event,
        a synthetic error CanonicalEvent is appended to the stream. Its exit code
        is waited for after EOF (OI-2128); a process still running when that
        wait ends is killed and gets a synthetic error event as well.

        Args:
            process: Running subprocess with stdout=PIPE.
            terminal_id: Terminal identifier (e.g. "T1").
            dispatch_id: Current dispatch identifier.
            event_store: EventStore instance for live persistence. None = skip writes.
            chunk_timeout: Max seconds between consecutive output lines.
            total_deadline: Max total seconds for the entire drain.
            _queue_maxsize: Bounded queue capacity (default 256 events).
            raw_tee_path: OI-1044 — when provided, every raw byte chunk read from
              the subprocess stdout pipe is also appended to this file, so an
              external FileProgressHeartbeat can observe activity independently
              of NDJSON-line parsing (mirrors the tmux-spawn lane's pipe-pane
              capture). None (default) disables the tee — no behavior change for
              existing callers.
        """
        try:
            chunk_timeout = float(os.environ.get("VNX_CHUNK_TIMEOUT", chunk_timeout))
        except (TypeError, ValueError):
            pass
        try:
            total_deadline = float(os.environ.get("VNX_TOTAL_DEADLINE", total_deadline))
        except (TypeError, ValueError):
            pass

        # OI-903: tie the chunk (stall) timeout to the total deadline so a long
        # deadline is the binding constraint, not a fixed stall value far below it.
        # Skipped when VNX_CHUNK_TIMEOUT is set explicitly — env overrides retain
        # top precedence (the per-provider stall env vars are handled upstream in
        # each spawn, which then passes already-resolved values here).
        if "VNX_CHUNK_TIMEOUT" not in os.environ:
            chunk_timeout = coerce_chunk_stall(chunk_timeout, total_deadline)

        result_queue: queue.Queue = queue.Queue(maxsize=_queue_maxsize)
        seen_complete = threading.Event()
        timed_out = threading.Event()

        def _producer() -> None:
            """Read stdout in background thread; push CanonicalEvents onto result_queue."""
            try:
                _run_producer(
                    process=process,
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    event_store=event_store,
                    chunk_timeout=chunk_timeout,
                    total_deadline=total_deadline,
                    result_queue=result_queue,
                    seen_complete=seen_complete,
                    timed_out=timed_out,
                    normalize_fn=self._normalize,
                    provider_name=self.provider_name,
                    raw_tee_path=raw_tee_path,
                )
            except Exception:
                logger.exception(
                    "_streaming_drainer: producer thread crashed for %s/%s",
                    terminal_id, dispatch_id,
                )
            finally:
                result_queue.put(_SENTINEL)

        producer = threading.Thread(target=_producer, daemon=True, name=f"drainer-{terminal_id}")
        producer.start()

        while True:
            try:
                item = result_queue.get(timeout=chunk_timeout + 10.0)
            except queue.Empty:
                # Producer appears stuck — emit a synthetic error and stop
                logger.warning(
                    "_streaming_drainer: consumer queue timed out for %s/%s",
                    terminal_id, dispatch_id,
                )
                err = _make_error_event(
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    provider=self.provider_name,
                    raw=None,
                    reason="consumer queue wait exceeded chunk_timeout+10s",
                )
                _append_to_store(event_store, terminal_id, err, dispatch_id)
                yield err
                break

            if item is _SENTINEL:
                break

            yield item

        producer.join(timeout=5.0)

        # A stream that ended without a complete event is an error unless the
        # child exited 0 (OI-2128: wait for the code, never read EOF as exit).
        if not seen_complete.is_set():
            err = _exit_error_event(
                process=process,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                provider=self.provider_name,
            )
            if err is not None:
                _append_to_store(event_store, terminal_id, err, dispatch_id)
                yield err


# ---------------------------------------------------------------------------
# Internal helpers (module-level to avoid closure capture issues in threads)
# ---------------------------------------------------------------------------

def _run_producer(
    *,
    process: subprocess.Popen,
    terminal_id: str,
    dispatch_id: str,
    event_store: Optional[Any],
    chunk_timeout: float,
    total_deadline: float,
    result_queue: "queue.Queue[Any]",
    seen_complete: threading.Event,
    timed_out: threading.Event,
    normalize_fn: Callable[[Dict[str, Any]], CanonicalEvent],
    provider_name: str,
    raw_tee_path: Optional[Path] = None,
) -> None:
    """Read subprocess stdout, parse NDJSON, push events to queue."""
    if process.stdout is None:
        return

    fd = process.stdout.fileno()
    line_buffer = b""
    start = time.monotonic()

    tee_fh = None
    if raw_tee_path is not None:
        try:
            raw_tee_path.parent.mkdir(parents=True, exist_ok=True)
            tee_fh = open(raw_tee_path, "ab", buffering=0)
        except OSError as exc:
            logger.warning(
                "_streaming_drainer: raw tee open failed for %s (heartbeat disabled): %s",
                raw_tee_path, exc,
            )
            tee_fh = None

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= total_deadline:
                logger.warning(
                    "_streaming_drainer: total deadline (%.0fs) exceeded for %s",
                    total_deadline, terminal_id,
                )
                timed_out.set()
                _kill_process(process)
                err = _make_error_event(
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    provider=provider_name,
                    raw=None,
                    reason=f"total deadline {total_deadline:.0f}s exceeded",
                    deadline_seconds=total_deadline,
                )
                _append_to_store(event_store, terminal_id, err, dispatch_id)
                result_queue.put(err)
                return

            remaining = min(chunk_timeout, total_deadline - elapsed)
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (ValueError, OSError):
                break  # fd closed

            if not ready:
                # OI-1044 co-death investigation: when total_deadline is close,
                # `remaining` is compressed below the configured chunk_timeout
                # (see the min() above), so a `not ready` here can mean either a
                # genuine full-length chunk_timeout stall OR a much shorter gap
                # that only looks like a stall because the deadline capped the
                # wait window. Reported gap := the actual seconds waited
                # (`remaining`), so the log/receipt never blames a false Ns of
                # silence — a worker that paused for 1s right as its deadline
                # ran out must not be reported as "silent for chunk_timeout".
                gap = remaining
                deadline_compressed = remaining < chunk_timeout
                logger.warning(
                    "_streaming_drainer: %s (%.0fs) for %s",
                    "deadline-compressed stall" if deadline_compressed else "chunk timeout",
                    gap, terminal_id,
                )
                timed_out.set()
                _kill_process(process)
                reason = (
                    f"total deadline reached during final stall window "
                    f"({gap:.0f}s remaining, configured chunk_timeout={chunk_timeout:.0f}s not "
                    f"fully elapsed)"
                    if deadline_compressed
                    else f"chunk timeout {chunk_timeout:.0f}s exceeded"
                )
                err = _make_error_event(
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    provider=provider_name,
                    raw=None,
                    reason=reason,
                    # OI-2066: a compressed window ended AT the total deadline.
                    deadline_seconds=total_deadline if deadline_compressed else None,
                )
                _append_to_store(event_store, terminal_id, err, dispatch_id)
                result_queue.put(err)
                return

            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break  # fd closed (process killed)

            if not chunk:
                # EOF: the child closed its stdout. That is not yet its exit
                # (OI-2128); the post-drain check waits for the code.
                break

            if tee_fh is not None:
                try:
                    tee_fh.write(chunk)
                except OSError as exc:
                    logger.debug(
                        "_streaming_drainer: raw tee write failed for %s: %s",
                        raw_tee_path, exc,
                    )

            line_buffer += chunk
            while b"\n" in line_buffer:
                raw_line, line_buffer = line_buffer.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                event = _parse_line(
                    line=line,
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    provider_name=provider_name,
                    normalize_fn=normalize_fn,
                )

                # Stamp Tier-1 on all streaming events
                object.__setattr__(event, "observability_tier", _STREAMING_TIER) if hasattr(type(event), "__dataclass_fields__") else None
                # Dataclasses are not frozen, so direct assignment works
                try:
                    event.observability_tier = _STREAMING_TIER
                except AttributeError:
                    pass

                if event.event_type in _COMPLETE_TYPES:
                    seen_complete.set()

                _append_to_store(event_store, terminal_id, event, dispatch_id)
                result_queue.put(event)  # blocks if queue full (backpressure)
    finally:
        if tee_fh is not None:
            try:
                tee_fh.close()
            except OSError:
                pass


def _parse_line(
    *,
    line: str,
    terminal_id: str,
    dispatch_id: str,
    provider_name: str,
    normalize_fn: Callable[[Dict[str, Any]], CanonicalEvent],
) -> CanonicalEvent:
    """Parse one NDJSON line and return a CanonicalEvent (or error event on failure)."""
    try:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"expected JSON object, got {type(raw).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        return _make_error_event(
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            provider=provider_name,
            raw=line,
            reason=str(exc),
        )

    try:
        return normalize_fn(raw)
    except Exception as exc:
        logger.warning(
            "_streaming_drainer: _normalize() raised for %s (line: %r): %s",
            terminal_id, line[:200], exc,
        )
        return _make_error_event(
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            provider=provider_name,
            raw=line,
            reason=f"normalize error: {exc}",
        )


def _make_error_event(
    *,
    terminal_id: str,
    dispatch_id: str,
    provider: str,
    raw: Optional[str],
    reason: str,
    deadline_seconds: Optional[float] = None,
) -> CanonicalEvent:
    """Build a canonical error event for malformed chunks or fatal conditions.

    ``deadline_seconds`` is passed only by a kill the TOTAL deadline caused; it
    stamps the OI-2066 marker (``DEADLINE_EXCEEDED_KEY`` plus the deadline)
    that :func:`deadline_exceeded_seconds` reads back.
    """
    data: Dict[str, Any] = {"reason": reason}
    if raw is not None:
        data["raw"] = raw[:500]  # truncate to avoid enormous NDJSON lines
    if deadline_seconds is not None:
        data[DEADLINE_EXCEEDED_KEY] = True
        data[DEADLINE_SECONDS_KEY] = float(deadline_seconds)
    return CanonicalEvent(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider=provider,
        event_type="error",
        data=data,
        observability_tier=_STREAMING_TIER,
    )


def _exit_error_event(
    *,
    process: subprocess.Popen,
    terminal_id: str,
    dispatch_id: str,
    provider: str,
) -> Optional[CanonicalEvent]:
    """The error event for a stream that ended without a complete event, or None.

    None only for a child that exited 0. The exit code is waited for, bounded
    by ``_EXIT_CODE_WAIT_SECONDS``, because EOF on stdout comes before the
    code (OI-2128). A child still running after that wait is killed and gets
    an error event of its own; see the constant for why.
    """
    rc = process.poll()
    if rc is None:
        try:
            rc = process.wait(timeout=_EXIT_CODE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process(process)
            return _make_error_event(
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                provider=provider,
                raw=None,
                reason=(
                    f"subprocess closed its output without a complete event and was "
                    f"still running {_EXIT_CODE_WAIT_SECONDS:.0f}s later; killed "
                    f"(exit code {process.poll()})"
                ),
            )
    if rc == 0:
        return None
    return _make_error_event(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        provider=provider,
        raw=None,
        reason=f"subprocess exited with code {rc} before complete event",
    )


def _append_to_store(
    event_store: Optional[Any],
    terminal_id: str,
    event: CanonicalEvent,
    dispatch_id: str,
) -> None:
    """Write event to EventStore; swallow all errors so the drainer stays live."""
    if event_store is None:
        return
    try:
        # Explicit dispatch_id kwarg wins (OI-1349 fix)
        event_store.append(terminal_id, event, dispatch_id=dispatch_id)
    except Exception:
        logger.exception(
            "_streaming_drainer: EventStore.append failed for %s", terminal_id
        )


def _kill_process(process: subprocess.Popen) -> None:
    """Send SIGTERM then SIGKILL to process group."""
    import signal as _signal
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, _signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, _signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
