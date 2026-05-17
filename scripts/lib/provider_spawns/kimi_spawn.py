"""kimi_spawn.py — Kimi CLI subprocess spawn handler (Wave 7.7).

Owns: spawn + stream-json parsing + canonical event normalization.
Caller (provider_dispatch.py) handles: receipt, unified report, lease, etc.

Kimi CLI invocation:
    kimi --print --output-format stream-json --yolo -p "<prompt>"

Authentication: OAuth via `kimi login` (operator-managed). No API key in spawn.

BILLING SAFETY: only subprocess.Popen(["kimi", ...]) is invoked.
No Anthropic SDK, no LiteLLM, no direct API calls.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from _streaming_drainer import StreamingDrainerMixin  # noqa: E402
from canonical_event import CanonicalEvent  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class KimiSpawnResult:
    """Return value from spawn_kimi(); carries spawn outcome to the caller."""

    returncode: int
    completion_text: str
    events_written: int
    session_id: Optional[str]
    timed_out: bool
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


def normalize_kimi_event(raw: dict, terminal_id: str, dispatch_id: str) -> CanonicalEvent:
    """Map a raw Kimi CLI stream-json event to a CanonicalEvent (Tier-1).

    Supports two event formats:

    Legacy (pre-v1.26):
      {"event_type": "assistant_text", "content": "..."}
      {"event_type": "tool_call", "name": "...", "input": {...}, "id": "..."}
      {"event_type": "tool_result", "tool_call_id": "...", "output": "..."}
      {"event_type": "usage_complete", "usage": {"prompt_tokens": N, ...}}
      {"event_type": "complete"}
      {"event_type": "error", "message": "..."}

    Wire Protocol camelCase (v1.26+):
      {"event_type": "TurnBegin", ...}   -> text (empty)
      {"event_type": "StepBegin", ...}   -> text (empty)
      {"event_type": "ContentPart", "content": "..."}  -> text
      {"event_type": "ThinkPart", "content": "..."}    -> thinking
      {"event_type": "TextPart", "text": "..."}        -> text
      {"event_type": "StatusUpdate", "token_count": {...}} -> text + token_count
      {"event_type": "TurnEnd", ...}     -> complete

    Unknown event_type values map to error events (never returns None).
    """
    def make(event_type: str, data: dict) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="kimi",
            event_type=event_type,
            data=data,
            observability_tier=1,
        )

    event_type = (raw.get("event_type") or raw.get("type") or "")

    if event_type in ("assistant_text", "text"):
        return make("text", {"text": str(raw.get("content", ""))})

    if event_type == "tool_call":
        return make("tool_use", {
            "name": str(raw.get("name", "")),
            "input": raw.get("input", {}),
            "id": str(raw.get("id", "")),
        })

    if event_type == "tool_result":
        return make("tool_result", {
            "tool_use_id": str(raw.get("tool_call_id", "")),
            "content": str(raw.get("output", "")),
        })

    if event_type == "usage_complete":
        usage = raw.get("usage") or {}
        token_count = {
            "input_tokens": int((usage.get("prompt_tokens") or 0)),
            "output_tokens": int((usage.get("completion_tokens") or 0)),
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        return make("text", {"text": "", "token_count": token_count})

    if event_type == "complete":
        return make("complete", {})

    if event_type == "error":
        msg = raw.get("message") or raw.get("error") or ""
        return make("error", {"message": str(msg) if msg else str(raw)[:200]})

    # Kimi CLI Wire Protocol v1.26+ camelCase event types
    if event_type == "TurnBegin":
        return make("text", {"text": ""})

    if event_type == "StepBegin":
        return make("text", {"text": ""})

    if event_type == "ContentPart":
        return make("text", {"text": str(raw.get("content") or raw.get("text") or "")})

    if event_type == "ThinkPart":
        return make("thinking", {"text": str(raw.get("content") or raw.get("text") or "")})

    if event_type == "TextPart":
        return make("text", {"text": str(raw.get("text") or raw.get("content") or "")})

    if event_type == "StatusUpdate":
        tc_raw = raw.get("token_count") or raw.get("usage") or {}
        token_count = {
            "input_tokens": int(tc_raw.get("input_tokens") or tc_raw.get("prompt_tokens") or 0),
            "output_tokens": int(tc_raw.get("output_tokens") or tc_raw.get("completion_tokens") or 0),
            "cache_creation_tokens": int(tc_raw.get("cache_creation_tokens") or 0),
            "cache_read_tokens": int(tc_raw.get("cache_read_tokens") or 0),
        }
        return make("text", {"text": "", "token_count": token_count})

    if event_type == "TurnEnd":
        return make("complete", {})

    logger.warning("kimi_spawn: unknown event_type %r, mapping to error", event_type)
    return make("error", {
        "reason": f"unrecognized kimi event_type: {event_type!r}",
        "raw_type": event_type,
        "raw": str(raw)[:300],
    })


class _KimiNormalizerHost(StreamingDrainerMixin):
    """Minimal state holder so StreamingDrainerMixin can call normalize_kimi_event."""

    provider_name = "kimi"
    provider_observability_tier = 1

    def __init__(self, terminal_id: str, dispatch_id: str) -> None:
        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

    def _normalize(self, raw: dict) -> CanonicalEvent:
        return normalize_kimi_event(raw, self._current_terminal_id, self._current_dispatch_id)


def _build_kimi_cmd(prompt: str, model: Optional[str], work_dir: Optional[Any]) -> list:
    """Build the kimi argv list."""
    cmd = ["kimi", "--print", "--output-format", "stream-json", "--yolo", "-p", prompt]
    if model:
        cmd.extend(["-m", model])
    if work_dir:
        cmd.extend(["-w", str(work_dir)])
    return cmd


def _start_kimi_subprocess(
    cmd: list,
    env: Dict[str, str],
    cwd_str: Optional[str],
) -> "tuple[subprocess.Popen | None, KimiSpawnResult | None]":
    """Start the kimi subprocess (no stdin — prompt passed via -p flag).

    Returns (proc, None) on success, or (None, KimiSpawnResult) on spawn failure.
    All subprocess-boundary errors convert to structured results; none are re-raised.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            cwd=cwd_str,
        )
    except FileNotFoundError as exc:
        return None, KimiSpawnResult(
            returncode=127,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage=None,
            error=f"kimi CLI not found: {exc}. Install: `uv tool install kimi-cli` and run `kimi login`.",
        )
    except OSError as exc:
        return None, KimiSpawnResult(
            returncode=126,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage=None,
            error=f"failed to spawn kimi: {exc}",
        )
    return proc, None


def _consume_kimi_stream(
    proc: subprocess.Popen,
    host: _KimiNormalizerHost,
    on_event: Optional[Callable],
    health_monitor: Optional[Any],
    event_writer: Optional[Callable],
    terminal_id: str,
    dispatch_id: str,
    event_store: Optional[Any],
    chunk_timeout: float,
    total_deadline: float,
) -> "tuple[str, int, Optional[Dict], bool, bool, int, list]":
    """Drain the stream; return (completion_text, events_written, token_usage, timed_out, stopped_early, failures, errors_captured)."""
    events_written = 0
    completion_parts: list = []
    token_usage: Optional[Dict[str, Any]] = None
    stopped_early = False
    timed_out = False
    _event_writer_failures = 0
    errors_captured: list = []

    for canonical_event in host.drain_stream(
        proc, terminal_id, dispatch_id, event_store,
        chunk_timeout=chunk_timeout, total_deadline=total_deadline,
    ):
        events_written += 1
        evt_type = canonical_event.event_type

        if evt_type in ("text", "complete"):
            text = (canonical_event.data or {}).get("text", "")
            if text:
                completion_parts.append(text)
            tc = (canonical_event.data or {}).get("token_count")
            if tc:
                token_usage = tc
        elif evt_type == "error":
            data = canonical_event.data or {}
            reason = (data.get("reason") or "").lower()
            if "timeout" in reason or "deadline" in reason:
                timed_out = True
            msg = data.get("message") or data.get("reason") or str(data)[:200]
            errors_captured.append(str(msg))

        if health_monitor is not None:
            health_monitor.update(canonical_event)

        if event_writer is not None:
            try:
                event_writer(terminal_id, canonical_event.to_dict(), dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.error(
                    "spawn_kimi: event_writer callback failed (dispatch=%s, event_count=%d): %s",
                    dispatch_id, events_written, _exc,
                )
                _event_writer_failures += 1

        if on_event is not None:
            if on_event(canonical_event) is False:
                stopped_early = True
                try:
                    proc.kill()
                except OSError as _ke:
                    logger.debug("spawn_kimi: kill after on_event=False failed: %s", _ke)
                break

    return "".join(completion_parts), events_written, token_usage, timed_out, stopped_early, _event_writer_failures, errors_captured


def _finalize_kimi_result(
    proc: subprocess.Popen,
    completion_text: str,
    events_written: int,
    token_usage: Optional[Dict[str, Any]],
    timed_out: bool,
    stopped_early: bool,
    event_writer_failures: int,
    errors_captured: Optional[list] = None,
) -> KimiSpawnResult:
    """Wait for process exit and return a KimiSpawnResult."""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    rc = proc.returncode if proc.returncode is not None else 1

    if errors_captured:
        error: Optional[str] = "\n".join(errors_captured)
        if rc == 0:
            rc = 1  # error event overrides false-success zero exit code
    elif rc != 0:
        error = f"kimi exited with code {rc}"
    else:
        error = None

    return KimiSpawnResult(
        returncode=rc,
        completion_text=completion_text,
        events_written=events_written,
        session_id=None,
        timed_out=timed_out,
        stopped_early=stopped_early,
        token_usage=token_usage,
        event_writer_failures=event_writer_failures,
        error=error,
    )


def spawn_kimi(
    prompt: str,
    model: Optional[str] = None,
    dispatch_id: str = "",
    terminal_id: str = "",
    *,
    event_writer: Optional[Callable[..., None]] = None,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    chunk_timeout: float = 60.0,
    total_deadline: float = 900.0,
    event_store: Optional[Any] = None,
    **kwargs: Any,
) -> KimiSpawnResult:
    """Spawn ``kimi --print --output-format stream-json --yolo -p <prompt>``.

    Returns KimiSpawnResult on completion (success OR controlled failure).
    Returns KimiSpawnResult(returncode=127) when the kimi binary is absent.
    Caller is responsible for lease/manifest/receipt/event-archive/retry.

    event_writer signature: ``(terminal_id, event_dict, dispatch_id=...)`` called
    per normalized event. Failures are counted in result.event_writer_failures.

    Auth: OAuth via ``kimi login`` (operator-managed). No API key required.

    DUPLICATE-WRITE CONTRACT: pass either ``event_writer`` OR ``event_store``, not
    both. ``event_store`` is forwarded to drain_stream (writes via drainer);
    ``event_writer`` is called per-event in _consume_kimi_stream. Passing both
    causes every event to be written twice.
    """
    if event_store is not None and event_writer is not None:
        raise ValueError("Pass either event_store OR event_writer, not both")

    try:
        chunk_timeout = float(os.environ.get("VNX_KIMI_STALL_THRESHOLD", chunk_timeout))
    except (TypeError, ValueError):
        pass
    try:
        total_deadline = float(os.environ.get("VNX_KIMI_TIMEOUT", total_deadline))
    except (TypeError, ValueError):
        pass

    env = {**os.environ, **(extra_env or {})}
    cwd_str = str(cwd) if cwd is not None else None

    cmd = _build_kimi_cmd(prompt, model, cwd)
    logger.info("kimi_spawn: launching %s", " ".join(cmd[:6]))

    proc, err_result = _start_kimi_subprocess(cmd, env, cwd_str)
    if err_result is not None:
        return err_result

    host = _KimiNormalizerHost(terminal_id=terminal_id, dispatch_id=dispatch_id)
    completion_text, events_written, token_usage, timed_out, stopped_early, _event_writer_failures, errors_captured = (
        _consume_kimi_stream(
            proc=proc, host=host, on_event=on_event,
            health_monitor=health_monitor, event_writer=event_writer,
            terminal_id=terminal_id, dispatch_id=dispatch_id,
            event_store=event_store, chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        )
    )
    return _finalize_kimi_result(
        proc=proc, completion_text=completion_text,
        events_written=events_written, token_usage=token_usage,
        timed_out=timed_out, stopped_early=stopped_early,
        event_writer_failures=_event_writer_failures,
        errors_captured=errors_captured,
    )
