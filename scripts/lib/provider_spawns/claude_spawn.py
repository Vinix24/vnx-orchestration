"""claude_spawn.py — Claude-specific spawn handler extracted from subprocess_dispatch.

Extracted in Wave 4.6 PR-4.6.2.  This module owns the "pure spawn+stream" slice:

  1. Spawn ``claude -p --output-format stream-json`` via SubprocessAdapter.
  2. Read the NDJSON event stream; write normalized events to EventStore (via
     SubprocessAdapter) and optionally call ``event_writer`` per event.
  3. Tick ``health_monitor`` on each event.
  4. Invoke the optional ``on_event`` callback per event; return False from it
     to stop the stream early (used by delivery.py for rotation detection).

Callers handle: lease/manifest/receipt/event-archive/retry/pattern-confidence.

BILLING SAFETY: only ``subprocess.Popen(["claude", ...])`` is invoked via
SubprocessAdapter.  No Anthropic SDK is imported anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Ensure scripts/lib/ is on sys.path so sibling modules are importable regardless
# of whether this module is loaded via subprocess_dispatch or directly in tests.
_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from subprocess_adapter import SubprocessAdapter  # noqa: E402 (path-setup above)

logger = logging.getLogger(__name__)


@dataclass
class ClaudeSpawnResult:
    """Return value from spawn_claude(); carries spawn outcome back to the caller."""

    returncode: int
    # Final ``result`` event data from the claude subprocess (agent_message + metadata).
    # Empty dict when the subprocess exited before emitting a result event.
    completion: Dict[str, Any]
    # Number of normalized events processed by the event loop.
    events_written: int
    # session_id extracted from the init event; None when the stream never
    # emitted an init or when delivery failed before the stream began.
    session_id: Optional[str]
    # True when chunk_timeout or total_deadline was breached and the subprocess
    # was killed by the timeout guard in SubprocessAdapter.
    timed_out: bool
    # True when on_event() returned False, causing early stream termination
    # (e.g. context-rotation threshold crossed).
    stopped_early: bool = False
    # Set when an exception terminated the stream read; forwarded to receipt.
    error: Optional[str] = None
    # Token usage extracted from the result event (input_tokens, output_tokens,
    # cache_read_input_tokens). None when the stream never emitted a result event
    # with usage data or when the SubprocessAdapter version does not forward it.
    token_usage: Optional[Dict[str, Any]] = None
    # Internal: the SubprocessAdapter instance used for this spawn.
    # Callers may use this for post-spawn cleanup (event_store.clear,
    # trigger_report_pipeline) to ensure the same session_id and state are
    # available.  Not included in repr to avoid verbose output.
    _adapter: Any = field(default=None, repr=False)

    def frontmatter_fields(self) -> Dict[str, Any]:
        usage = self.token_usage or {}
        return {
            "provider": "claude",
            "sub_provider": "anthropic",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("input_tokens", 0) or 0),
                "output": int(usage.get("output_tokens", 0) or 0),
                "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
            },
        }


def spawn_claude(
    prompt: str,
    model: str,
    dispatch_id: str,
    terminal_id: str,
    *,
    event_writer: Optional[Callable[..., None]] = None,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    resume_session: Optional[str] = None,
    skip_permissions: bool = False,
    chunk_timeout: float = 300.0,
    total_deadline: float = 900.0,
    role: Optional[str] = None,
    requires_mcp: bool = False,
    **kwargs: Any,
) -> ClaudeSpawnResult:
    """Spawn ``claude -p --output-format stream-json`` and consume the event stream.

    Returns ClaudeSpawnResult on completion (success OR controlled failure).
    Caller is responsible for lease acquisition/release, manifest write,
    receipt emission, event archive, and retry loop.

    Parameters
    ----------
    prompt:
        Fully assembled dispatch instruction (post-assembly, with skill context
        and permission profile already injected by the caller).
    model:
        Claude model identifier, e.g. ``"sonnet"``, ``"haiku"``.
    dispatch_id:
        Dispatch identifier forwarded to SubprocessAdapter and EventStore.
    terminal_id:
        Worker terminal, e.g. ``"T1"``.
    event_writer:
        Optional callback ``(terminal_id, event_dict, dispatch_id=...)`` called
        for every normalized event in addition to SubprocessAdapter's internal
        EventStore write.  Designed for future providers that won't use
        SubprocessAdapter internally; for claude, SubprocessAdapter already
        writes to EventStore so callers should pass ``None`` to avoid double writes.
    health_monitor:
        Optional WorkerHealthMonitor instance.  ``health_monitor.update(event)``
        is called on every event; stuck-event logging runs at SLOW_THRESHOLD.
    on_event:
        Optional per-event callback ``(StreamEvent) -> bool|None``.  Return
        ``False`` to stop the stream early (delivery.py uses this for context
        rotation detection).  ``None`` or any truthy return continues.
    extra_env:
        Environment variables merged into the worker subprocess environment
        (used for VNX identity propagation).
    cwd:
        Working directory for the claude subprocess.
    resume_session:
        Prior session ID for ``--resume`` (session continuity).
    skip_permissions:
        Forwarded for interface parity with future providers; currently unused
        because SubprocessAdapter always passes ``--dangerously-skip-permissions``.
    role:
        Dispatch role forwarded to SubprocessAdapter.deliver so role-specific
        capability scoping is applied (tool allow-list, MCP isolation). Falls
        back to the default code-worker profile when None.
    requires_mcp:
        When True, the worker retains its normal ambient MCP config instead of
        the default force-empty posture. Forwarded to SubprocessAdapter.deliver.
    chunk_timeout:
        Max seconds to wait for the next output chunk.  Override with
        ``VNX_CHUNK_TIMEOUT`` env var.
    total_deadline:
        Max total seconds for the entire dispatch.  Override with
        ``VNX_TOTAL_DEADLINE`` env var.
    **kwargs:
        Accepted for forward-compatibility; ignored.
    """
    import os
    try:
        from worker_health_monitor import HealthStatus, SLOW_THRESHOLD
    except ImportError:
        HealthStatus = None  # type: ignore[assignment]
        SLOW_THRESHOLD = 120.0

    # Apply env overrides (same logic as delivery.py for byte-identical timeouts).
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
    except (KeyError, ValueError):
        pass

    adapter = SubprocessAdapter()
    deliver_result = adapter.deliver(
        terminal_id,
        dispatch_id,
        instruction=prompt,
        model=model,
        cwd=cwd,
        resume_session=resume_session,
        extra_env=extra_env,
        role=role,
        requires_mcp=requires_mcp,
    )

    if not deliver_result.success:
        logger.warning(
            "spawn_claude: SubprocessAdapter.deliver() failed for %s — %s",
            terminal_id,
            deliver_result.failure_reason,
        )
        return ClaudeSpawnResult(
            returncode=1,
            completion={},
            events_written=0,
            session_id=None,
            timed_out=False,
            _adapter=adapter,
        )

    # Wire event_store into health_monitor so STUCK events persist to NDJSON
    # (mirrors _wire_event_store_into_health in delivery.py).
    if health_monitor is not None:
        es = adapter.event_store
        if es is not None and getattr(health_monitor, "_event_store", None) is None:
            health_monitor._event_store = es

    completion: Dict[str, Any] = {}
    events_written = 0
    stopped_early = False
    last_stuck_log = 0.0
    token_usage: Optional[Dict[str, Any]] = None

    try:
        for event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            events_written += 1

            # Capture final result event (agent_message + metadata + usage).
            if event.type == "result":
                completion = dict(event.data) if isinstance(event.data, dict) else {}
                _raw_usage = completion.get("usage") if completion else None
                if isinstance(_raw_usage, dict) and _raw_usage:
                    token_usage = _raw_usage

            # Tick health monitor; run stuck-event logging at threshold.
            if health_monitor is not None:
                health_monitor.update(event)
                if HealthStatus is not None:
                    now = time.monotonic()
                    if now - last_stuck_log >= SLOW_THRESHOLD:
                        h = health_monitor.health_status()
                        if h.status == HealthStatus.STUCK:
                            health_monitor.log_stuck_event()
                            last_stuck_log = now

            # Optional secondary event_writer (for future provider parity).
            if event_writer is not None:
                try:
                    event_writer(
                        terminal_id,
                        {"type": event.type, "data": event.data},
                        dispatch_id=dispatch_id,
                    )
                except Exception as _exc:
                    logger.debug("spawn_claude: event_writer raised: %s", _exc)

            # Per-event callback — return False to stop stream (e.g. rotation).
            if on_event is not None:
                should_continue = on_event(event)
                if should_continue is False:
                    stopped_early = True
                    try:
                        adapter.stop(terminal_id)
                    except Exception as _exc:
                        logger.debug(
                            "spawn_claude: adapter.stop() after on_event=False failed: %s",
                            _exc,
                        )
                    break

    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        logger.error(
            "spawn_claude: stream read failure for dispatch=%s terminal=%s: %s",
            dispatch_id,
            terminal_id,
            e,
        )
        # Fail closed: stop subprocess + return non-zero immediately.
        try:
            adapter.stop(terminal_id)
        except Exception as _stop_exc:
            logger.debug(
                "spawn_claude: adapter.stop() in error handler raised: %s", _stop_exc
            )
        obs = adapter.observe(terminal_id)
        _rc = obs.transport_state.get("returncode") or 1
        return ClaudeSpawnResult(
            returncode=_rc,
            completion={},
            events_written=events_written,
            session_id=adapter.get_session_id(terminal_id),
            timed_out=False,
            error=str(e),
        )

    timed_out = adapter.was_timed_out(terminal_id)
    obs = adapter.observe(terminal_id)
    returncode: Optional[int] = obs.transport_state.get("returncode")
    if returncode is None:
        returncode = getattr(adapter, "_returncode_cache", {}).get(terminal_id)
    if returncode is None:
        returncode = 1

    return ClaudeSpawnResult(
        returncode=returncode,
        completion=completion,
        events_written=events_written,
        session_id=adapter.get_session_id(terminal_id),
        timed_out=timed_out,
        stopped_early=stopped_early,
        token_usage=token_usage,
        _adapter=adapter,
    )
