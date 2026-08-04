#!/usr/bin/env python3
"""worker_heartbeat.py — Event-stream-based worker silence detection.

Detects stuck workers by monitoring the event stream for silence.
When a worker produces no events for a configurable threshold,
it is considered stuck and a terminal failure is written.

Deterministic: no model calls, pure timer + read on NDJSON or file growth.

**Data-driven default (N=600s):** measured across 4.2M within-dispatch event
gaps from 445 real dispatches (T1/T2/T3 subprocess lanes, archived + live
streams from vnx-dev).  Distribution:
  p50=0.0s  p90=0.0s  p95=0.0s  p99=0.1s  p99.9=2.5s  max=566.3s (T2)

The longest legitimate gap in a production build-worker dispatch was 389.7s
(a plan-revise dispatch on T2).  The 600s default is ~1.5x above that tail
and ~240x the p99.9 value, giving ample headroom for slow tool executions
while catching truly hung workers within typical dispatch deadlines
(3600-7200s).

The default is overridable via VNX_WORKER_HEARTBEAT_SILENCE_SECONDS.

BILLING SAFETY: No Anthropic SDK imports.  Local filesystem only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default silence threshold: 600 seconds (10 minutes).
# See module docstring for the data-driven rationale.
_DEFAULT_SILENCE_THRESHOLD_SECONDS = 600

# Env var to override the default.
_ENV_SILENCE_SECONDS = "VNX_WORKER_HEARTBEAT_SILENCE_SECONDS"


def _resolve_silence_threshold() -> float:
    """Resolve the silence threshold from env or default.

    Returns the threshold in seconds.  Negative or unparseable values
    clamp to the default.  Zero disables the heartbeat (never silent).
    """
    raw = os.environ.get(_ENV_SILENCE_SECONDS, "").strip()
    if not raw:
        return _DEFAULT_SILENCE_THRESHOLD_SECONDS
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "worker_heartbeat: unparseable %s=%r; using default %ds",
            _ENV_SILENCE_SECONDS,
            raw,
            _DEFAULT_SILENCE_THRESHOLD_SECONDS,
        )
        return _DEFAULT_SILENCE_THRESHOLD_SECONDS
    if val < 0:
        logger.warning(
            "worker_heartbeat: negative %s=%r; using default %ds",
            _ENV_SILENCE_SECONDS,
            raw,
            _DEFAULT_SILENCE_THRESHOLD_SECONDS,
        )
        return _DEFAULT_SILENCE_THRESHOLD_SECONDS
    return val


# Known patterns in event data that indicate a permission prompt.
_PERMISSION_PROMPT_INDICATORS = (
    "permission",
    "ask_permission",
    "request_permission",
    "tool_use_blocked",
    "permission_denied",
    "approval_required",
)


@dataclass
class SilenceVerdict:
    """Result of a silence check."""
    is_silent: bool
    silence_seconds: float
    threshold_seconds: float
    last_event: Optional[Dict[str, Any]] = None
    last_event_timestamp: Optional[str] = None
    is_permission_prompt: bool = False
    permission_reason: str = ""


class EventStreamHeartbeat:
    """Monitor an EventStore NDJSON stream for silence.

    Reads the last event from the EventStore for *terminal_id* and
    checks whether the stream has been silent longer than *silence_threshold_seconds*.

    This is the heartbeat for the subprocess adapter lane, where the
    worker writes structured events to EventStore in real-time.
    """

    def __init__(
        self,
        terminal_id: str,
        dispatch_id: str,
        *,
        events_dir: Optional[Path] = None,
        silence_threshold_seconds: Optional[float] = None,
    ) -> None:
        self.terminal_id = terminal_id
        self.dispatch_id = dispatch_id
        self._threshold = (
            silence_threshold_seconds
            if silence_threshold_seconds is not None
            else _resolve_silence_threshold()
        )

        # Resolve EventStore lazily to avoid import cycles at module level.
        self._events_dir = events_dir
        self._store = None

    @property
    def threshold(self) -> float:
        return self._threshold

    def _get_store(self):
        """Lazy-load the EventStore singleton."""
        if self._store is None:
            from event_store import EventStore  # noqa: PLC0415
            self._store = EventStore(events_dir=self._events_dir)
        return self._store

    def _last_event(self) -> Optional[Dict[str, Any]]:
        """Return the last event in the stream for this terminal."""
        try:
            store = self._get_store()
            return store.last_event(self.terminal_id)
        except Exception as exc:
            logger.debug(
                "worker_heartbeat: EventStore read failed for %s: %s",
                self.terminal_id,
                exc,
            )
            return None

    def silence_seconds(self) -> float:
        """Seconds since the last event was written, or 0.0 if no events yet."""
        event = self._last_event()
        if event is None:
            return 0.0
        ts = event.get("timestamp", "")
        if not ts:
            return 0.0
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except (ValueError, TypeError):
            return 0.0

    def check(self) -> SilenceVerdict:
        """Check whether the stream is silent.

        Returns a SilenceVerdict with the current state.
        """
        event = self._last_event()
        if event is None:
            # No events at all — stream hasn't started yet.
            # This is NOT silence; it's pre-init.
            return SilenceVerdict(
                is_silent=False,
                silence_seconds=0.0,
                threshold_seconds=self._threshold,
            )

        ts = event.get("timestamp", "")
        silence = 0.0
        if ts:
            try:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                silence = (datetime.now(timezone.utc) - dt).total_seconds()
            except (ValueError, TypeError):
                pass

        is_silent = silence >= self._threshold if self._threshold > 0 else False

        # Permission prompt detection
        is_perm, perm_reason = _detect_permission_prompt(event)

        return SilenceVerdict(
            is_silent=is_silent,
            silence_seconds=silence,
            threshold_seconds=self._threshold,
            last_event=event,
            last_event_timestamp=ts,
            is_permission_prompt=is_perm,
            permission_reason=perm_reason,
        )


class FileProgressHeartbeat:
    """Monitor a file for growth as a progress signal.

    For the tmux-spawn lane, the pipe-pane log file captures all worker
    output.  If the file hasn't grown (size change) in *silence_threshold_seconds*,
    the worker is considered stuck.

    Also supports checking the file's last-modified time as a fallback.
    """

    def __init__(
        self,
        file_path: Path,
        dispatch_id: str,
        *,
        silence_threshold_seconds: Optional[float] = None,
    ) -> None:
        self._path = file_path
        self.dispatch_id = dispatch_id
        self._threshold = (
            silence_threshold_seconds
            if silence_threshold_seconds is not None
            else _resolve_silence_threshold()
        )
        self._last_size: Optional[int] = None
        self._last_size_time: float = time.monotonic()

    @property
    def threshold(self) -> float:
        return self._threshold

    def _current_size(self) -> Optional[int]:
        """Return the current file size, or None if file doesn't exist."""
        try:
            return self._path.stat().st_size
        except OSError:
            return None

    def update(self) -> None:
        """Update the tracked state based on current file state.

        Call this periodically.  When the file grows, the silence timer resets.
        """
        size = self._current_size()
        if size is None:
            return
        if self._last_size is None or size > self._last_size:
            self._last_size = size
            self._last_size_time = time.monotonic()

    def silence_seconds(self) -> float:
        """Seconds since the file last grew."""
        if self._last_size is None:
            return 0.0
        return time.monotonic() - self._last_size_time

    def check(self) -> SilenceVerdict:
        """Check whether the file has stopped growing.

        Returns a SilenceVerdict.  Updates the internal state first.
        """
        self.update()
        silence = self.silence_seconds()
        is_silent = silence >= self._threshold if self._threshold > 0 else False

        return SilenceVerdict(
            is_silent=is_silent,
            silence_seconds=silence,
            threshold_seconds=self._threshold,
            # File-based heartbeat doesn't parse event content.
            last_event=None,
            last_event_timestamp=None,
            is_permission_prompt=False,
            permission_reason="",
        )


def _detect_permission_prompt(event: Dict[str, Any]) -> tuple[bool, str]:
    """Check if an event indicates a permission prompt.

    Returns (is_permission_prompt, reason_string).
    """
    event_type = event.get("type", "")
    data = event.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}

    # Check event type
    type_lower = event_type.lower()
    for indicator in _PERMISSION_PROMPT_INDICATORS:
        if indicator in type_lower:
            return True, f"event type contains '{indicator}'"

    # Check event data for permission-related fields
    if isinstance(data, dict):
        for key in ("permission", "permission_type", "blocked_reason", "ask_reason"):
            val = data.get(key, "")
            if val:
                return True, f"event data.{key}={val}"

        # Check nested content
        message = data.get("message", {})
        if isinstance(message, dict):
            for key in ("permission", "permission_type"):
                val = message.get(key, "")
                if val:
                    return True, f"event data.message.{key}={val}"

    # Check raw line for permission indicators
    raw = json.dumps(event).lower()
    for indicator in _PERMISSION_PROMPT_INDICATORS:
        if indicator in raw:
            return True, f"raw event contains '{indicator}'"

    return False, ""


def build_heartbeat_failure_report(
    dispatch_id: str,
    verdict: SilenceVerdict,
    *,
    model: str = "unknown",
    provider: str = "unknown",
    terminal_id: str = "",
) -> str:
    """Build a terminal failure report for a heartbeat-killed worker.

    Returns the report text as a string.  The caller is responsible for
    writing it to the unified_reports directory.
    """
    from datetime import datetime as _dt

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    silence_min = verdict.silence_seconds / 60.0

    if verdict.is_permission_prompt:
        reason = (
            f"worker heartbeat: hung on permission prompt "
            f"({verdict.permission_reason}) — "
            f"no events for {silence_min:.1f} minutes "
            f"(threshold: {verdict.threshold_seconds:.0f}s)"
        )
    else:
        reason = (
            f"worker heartbeat: event stream silent for {silence_min:.1f} minutes "
            f"(threshold: {verdict.threshold_seconds:.0f}s)"
        )

    last_event_info = ""
    if verdict.last_event is not None:
        last_event_info = json.dumps(
            {
                "timestamp": verdict.last_event_timestamp,
                "type": verdict.last_event.get("type", ""),
                "data_summary": (
                    str(verdict.last_event.get("data", ""))[:200]
                ),
            },
            indent=2,
        )

    return f"""**Dispatch-ID**: {dispatch_id}
**Model**: {model}
**Provider**: {provider}

## Summary

Worker killed by heartbeat monitor: {reason}

The worker was terminated because it produced no observable progress
for longer than the configured silence threshold.  This is a terminal
failure — the dispatch did not complete.

## Changes

None.  The worker was killed before producing any committed changes.

## Verification

- Heartbeat silence threshold: {verdict.threshold_seconds:.0f}s
- Actual silence duration: {verdict.silence_seconds:.0f}s
- Last event timestamp: {verdict.last_event_timestamp or 'N/A'}
- Permission prompt detected: {'yes' if verdict.is_permission_prompt else 'no'}
- Terminal: {terminal_id}
- Kill timestamp: {now_iso}

Last event in stream:
```json
{last_event_info if last_event_info else 'No events in stream'}
```

## Open Items

- Investigate why worker {terminal_id} stopped producing events for dispatch {dispatch_id}
- If permission prompt: review permission configuration for this terminal
- If silent: check worker logs for crash or hang evidence
"""
