"""gemini_spawn.py — Gemini-specific spawn handler extracted from gemini_adapter.

Extracted in Wave 4.6 PR-4.6.4. This module owns the "pure spawn+stream" slice:

  1. Spawn ``gemini --model <model> --output-format stream-json`` (or json) via
     subprocess.Popen with stdin pipe for the prompt.
  2. Read NDJSON / buffered-JSON output; normalize to CanonicalEvent objects via
     the standalone ``normalize_gemini_event`` function.
  3. Tick health_monitor on each event.
  4. Invoke optional on_event callback per event (return False to stop early).

Callers handle: lease/manifest/receipt/event-archive/retry.

BILLING SAFETY: only ``subprocess.Popen(["gemini", ...])`` is invoked. No SDK,
no LiteLLM, no direct API calls.
"""

from __future__ import annotations

import json
import logging
import os
import select as _select_mod
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from _streaming_drainer import StreamingDrainerMixin  # noqa: E402
from canonical_event import CanonicalEvent  # noqa: E402

logger = logging.getLogger(__name__)

_TIER_STREAMING = 1
_TIER_LEGACY = 3


# ---------------------------------------------------------------------------
# Token extraction helpers (single implementation; gemini_adapter delegates here)
# ---------------------------------------------------------------------------

def _extract_gemini_usage_metadata(data: dict) -> Optional[dict]:
    """Extract token counts from a parsed Gemini dict.

    Handles both top-level and nested usageMetadata. Returns None when
    both prompt and candidates counts are zero.
    """
    usage_meta = data.get("usageMetadata")
    if isinstance(usage_meta, dict):
        data = usage_meta
    prompt_t = data.get("promptTokenCount", 0) or 0
    candidates_t = data.get("candidatesTokenCount", 0) or 0
    if not isinstance(prompt_t, int) or not isinstance(candidates_t, int):
        return None
    if prompt_t == 0 and candidates_t == 0:
        return None
    return {
        "input_tokens": prompt_t,
        "output_tokens": candidates_t,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }


def _extract_gemini_token_count(raw: dict) -> Optional[dict]:
    """Locate and normalize token counts from a Gemini stream event dict."""
    usage_meta = raw.get("usageMetadata")
    if isinstance(usage_meta, dict):
        result = _extract_gemini_usage_metadata(usage_meta)
        if result:
            return result
    if "promptTokenCount" in raw or "candidatesTokenCount" in raw:
        result = _extract_gemini_usage_metadata(raw)
        if result:
            return result
    return None


def _parse_gemini_response(raw: str) -> str:
    """Extract findings text from a buffered JSON response; fall back to raw text."""
    stripped = raw.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            for key in ("response", "text", "content", "output"):
                if key in parsed:
                    return str(parsed[key])
        return stripped
    except (json.JSONDecodeError, ValueError):
        return stripped


def _parse_gemini_token_usage(raw: str) -> Optional[dict]:
    """Parse token counts from Gemini CLI stdout (JSON or NDJSON format)."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            result = _extract_gemini_usage_metadata(parsed)
            if result:
                return result
    except json.JSONDecodeError:
        pass
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                result = _extract_gemini_usage_metadata(parsed)
                if result:
                    return result
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Normalizer — single canonical implementation
# ---------------------------------------------------------------------------

def normalize_gemini_event(raw: dict, terminal_id: str, dispatch_id: str) -> CanonicalEvent:
    """Map a raw Gemini stream-json event to a CanonicalEvent (Tier-1).

    Both _GeminiNormalizerHost (used by spawn_gemini streaming path) and
    GeminiAdapter._normalize delegate here to guarantee byte identity.
    """
    def make(event_type: str, data: dict) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="gemini",
            event_type=event_type,
            data=data,
            observability_tier=_TIER_STREAMING,
        )

    etype = raw.get("type", "")

    if etype in ("session_start", "init"):
        return make("init", {"raw_type": etype})

    if etype in ("message", "text", "content"):
        text = raw.get("text") or raw.get("content") or raw.get("message") or ""
        return make("text", {"text": str(text)})

    if etype in ("tool_use", "tool_call", "function_call"):
        name = raw.get("name") or raw.get("function_name") or raw.get("tool", "")
        args = raw.get("args") or raw.get("input") or raw.get("arguments") or {}
        if not isinstance(args, dict):
            args = {"raw": str(args)}
        return make("tool_use", {"name": str(name), "args": args})

    if etype in ("tool_result", "tool_response", "function_response"):
        output = raw.get("output") or raw.get("result") or raw.get("content") or ""
        return make("tool_result", {"output": str(output)})

    if etype in ("result", "done", "complete", "finish"):
        data: dict = {}
        text = raw.get("text") or raw.get("content") or raw.get("output") or ""
        if text:
            data["text"] = str(text)
        token_count = _extract_gemini_token_count(raw)
        if token_count:
            data["token_count"] = token_count
        return make("complete", data)

    if etype == "error":
        msg = raw.get("message") or raw.get("error") or raw.get("text") or ""
        return make("error", {"message": str(msg) if msg else str(raw)[:200]})

    if "usageMetadata" in raw:
        token_count = _extract_gemini_token_count(raw)
        if token_count:
            return make("text", {"text": "", "token_count": token_count})

    return make("error", {
        "reason": f"unrecognized gemini event type: {etype!r}",
        "raw_type": etype,
        "raw": str(raw)[:300],
    })


# ---------------------------------------------------------------------------
# Internal normalizer host (composes StreamingDrainerMixin for drain_stream)
# ---------------------------------------------------------------------------

class _GeminiNormalizerHost(StreamingDrainerMixin):
    """Minimal state holder so StreamingDrainerMixin can call normalize_gemini_event."""

    provider_name = "gemini"
    provider_observability_tier = _TIER_STREAMING

    def __init__(self, terminal_id: str, dispatch_id: str) -> None:
        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

    def _normalize(self, raw: dict) -> CanonicalEvent:
        return normalize_gemini_event(raw, self._current_terminal_id, self._current_dispatch_id)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class GeminiSpawnResult:
    """Return value from spawn_gemini(); carries spawn outcome to the caller."""

    returncode: int
    completion_text: str
    events_written: int
    session_id: Optional[str]
    timed_out: bool
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Main spawn function
# ---------------------------------------------------------------------------

def spawn_gemini(
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
    chunk_timeout: float = 60.0,
    total_deadline: float = 600.0,
    event_store: Optional[Any] = None,
    **kwargs: Any,
) -> GeminiSpawnResult:
    """Spawn ``gemini`` and consume the event stream.

    Routes through streaming drainer (NDJSON, Tier-1) when VNX_GEMINI_STREAM=1;
    otherwise uses the legacy buffered-JSON path (Tier-3, single result event).
    Returns GeminiSpawnResult on completion. Caller manages lease/manifest/receipt.

    Parameters
    ----------
    prompt:
        Fully assembled dispatch instruction written to gemini stdin.
    model:
        Gemini model identifier, e.g. ``"gemini-2.5-pro"``.
    dispatch_id, terminal_id:
        Identifiers forwarded to the normalizer and EventStore.
    event_writer:
        Optional callback ``(terminal_id, event_dict, dispatch_id=...)`` called
        for every normalized event. Used by GeminiAdapter.execute() to collect events.
    health_monitor:
        Optional WorkerHealthMonitor; ``health_monitor.update(event)`` called per event.
    on_event:
        Optional per-event callback ``(CanonicalEvent) -> bool|None``. Return
        ``False`` to stop the stream early.
    extra_env:
        Additional environment variables merged into the subprocess environment.
    cwd:
        Working directory for the gemini subprocess.
    chunk_timeout:
        Max seconds between consecutive output lines (streaming path only).
    total_deadline:
        Max total seconds for the entire dispatch.
    event_store:
        EventStore instance for live persistence; None = skip.
    **kwargs:
        Accepted for forward-compatibility; ignored.

    A missing gemini binary returns returncode=127 with result.error set;
    it does NOT raise FileNotFoundError. Check result.error to detect spawn failures.
    """
    try:
        chunk_timeout = float(os.environ.get("VNX_GEMINI_STALL_THRESHOLD", chunk_timeout))
    except (TypeError, ValueError):
        pass
    try:
        total_deadline = float(os.environ.get("VNX_GEMINI_TIMEOUT", total_deadline))
    except (TypeError, ValueError):
        pass

    env = {**os.environ, **(extra_env or {})}
    cwd_str = str(cwd) if cwd is not None else None

    if os.environ.get("VNX_GEMINI_STREAM", "0").strip() == "1":
        return _spawn_streaming(
            prompt=prompt, model=model,
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            event_writer=event_writer, health_monitor=health_monitor,
            on_event=on_event, env=env, cwd_str=cwd_str,
            chunk_timeout=chunk_timeout, total_deadline=total_deadline,
            event_store=event_store,
        )
    return _spawn_legacy(
        prompt=prompt, model=model,
        dispatch_id=dispatch_id, terminal_id=terminal_id,
        event_writer=event_writer, env=env, cwd_str=cwd_str,
        total_deadline=total_deadline,
    )


# ---------------------------------------------------------------------------
# Subprocess spawn helpers
# ---------------------------------------------------------------------------

def _build_gemini_cmd(model: str, output_format: str) -> list:
    """Build the gemini argv list for the given model and output format."""
    return ["gemini", "--model", model, "--output-format", output_format]


def _start_gemini_subprocess(
    cmd: list,
    env: Optional[Dict],
    cwd_str: Optional[str],
    prompt: str,
) -> "tuple[subprocess.Popen | None, GeminiSpawnResult | None]":
    """Start the gemini subprocess and write prompt to stdin.

    Returns (proc, None) on success, or (None, GeminiSpawnResult) on spawn failure.
    All subprocess-boundary errors convert to structured results; none are re-raised.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
            env=env, cwd=cwd_str,
        )
    except FileNotFoundError as exc:
        return None, GeminiSpawnResult(
            returncode=127, completion_text="", events_written=0,
            session_id=None, timed_out=False, stopped_early=False,
            token_usage=None, error=f"gemini binary not found: {exc}",
        )
    except OSError as exc:
        return None, GeminiSpawnResult(
            returncode=126, completion_text="", events_written=0,
            session_id=None, timed_out=False, stopped_early=False,
            token_usage=None, error=f"failed to spawn gemini: {exc}",
        )

    if proc.stdin:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError as exc:
            return None, GeminiSpawnResult(
                returncode=1, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                error=f"stdin write failed (BrokenPipeError): {exc}",
            )

    return proc, None


def _consume_gemini_stream(
    proc: subprocess.Popen,
    host: "_GeminiNormalizerHost",
    on_event: Optional[Callable],
    health_monitor: Optional[Any],
    event_writer: Optional[Callable],
    terminal_id: str,
    dispatch_id: str,
    event_store: Optional[Any],
    chunk_timeout: float,
    total_deadline: float,
) -> "tuple[str, int, Optional[Dict[str, Any]], bool, bool]":
    """Drain the NDJSON stream; return (completion_text, events_written, token_usage, timed_out, stopped_early)."""
    events_written = 0
    completion_text = ""
    token_usage: Optional[Dict[str, Any]] = None
    stopped_early = False
    timed_out = False

    for canonical_event in host.drain_stream(
        proc, terminal_id, dispatch_id, event_store,
        chunk_timeout=chunk_timeout, total_deadline=total_deadline,
    ):
        events_written += 1
        evt_type = canonical_event.event_type

        if evt_type == "complete":
            completion_text = canonical_event.data.get("text", "")
            tc = canonical_event.data.get("token_count")
            if tc:
                token_usage = tc
        elif evt_type == "text":
            tc = canonical_event.data.get("token_count")
            if tc:
                token_usage = tc
        elif evt_type == "error":
            reason = (canonical_event.data.get("reason") or "").lower()
            if "timeout" in reason or "deadline" in reason:
                timed_out = True

        if health_monitor is not None:
            health_monitor.update(canonical_event)

        if event_writer is not None:
            try:
                event_writer(terminal_id, canonical_event.to_dict(), dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.debug("spawn_gemini: event_writer raised: %s", _exc)

        if on_event is not None:
            if on_event(canonical_event) is False:
                stopped_early = True
                try:
                    proc.kill()
                except OSError as _ke:
                    logger.debug("spawn_gemini: kill after on_event=False failed: %s", _ke)
                break

    return completion_text, events_written, token_usage, timed_out, stopped_early


def _finalize_gemini_result(
    proc: subprocess.Popen,
    completion_text: str,
    events_written: int,
    token_usage: Optional[Dict[str, Any]],
    timed_out: bool,
    stopped_early: bool,
) -> GeminiSpawnResult:
    """Wait for process exit and return a GeminiSpawnResult."""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    rc = proc.returncode if proc.returncode is not None else 1
    return GeminiSpawnResult(
        returncode=rc, completion_text=completion_text, events_written=events_written,
        session_id=None, timed_out=timed_out, stopped_early=stopped_early,
        token_usage=token_usage,
    )


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------

def _spawn_streaming(
    *,
    prompt: str,
    model: str,
    dispatch_id: str,
    terminal_id: str,
    event_writer: Optional[Callable],
    health_monitor: Optional[Any],
    on_event: Optional[Callable],
    env: Optional[Dict],
    cwd_str: Optional[str],
    chunk_timeout: float,
    total_deadline: float,
    event_store: Optional[Any],
) -> GeminiSpawnResult:
    """Spawn ``gemini --output-format stream-json`` and drain the NDJSON event stream."""
    cmd = _build_gemini_cmd(model, "stream-json")
    proc, err_result = _start_gemini_subprocess(cmd, env, cwd_str, prompt)
    if err_result is not None:
        return err_result

    host = _GeminiNormalizerHost(terminal_id=terminal_id, dispatch_id=dispatch_id)
    completion_text, events_written, token_usage, timed_out, stopped_early = _consume_gemini_stream(
        proc=proc, host=host, on_event=on_event,
        health_monitor=health_monitor, event_writer=event_writer,
        terminal_id=terminal_id, dispatch_id=dispatch_id,
        event_store=event_store, chunk_timeout=chunk_timeout,
        total_deadline=total_deadline,
    )
    return _finalize_gemini_result(
        proc=proc, completion_text=completion_text,
        events_written=events_written, token_usage=token_usage,
        timed_out=timed_out, stopped_early=stopped_early,
    )


# ---------------------------------------------------------------------------
# Legacy buffered path
# ---------------------------------------------------------------------------

def _spawn_legacy(
    *,
    prompt: str,
    model: str,
    dispatch_id: str,
    terminal_id: str,
    event_writer: Optional[Callable],
    env: Optional[Dict],
    cwd_str: Optional[str],
    total_deadline: float,
) -> GeminiSpawnResult:
    """Spawn ``gemini --output-format json`` and collect a single buffered response."""
    cmd = _build_gemini_cmd(model, "json")
    proc, err_result = _start_gemini_subprocess(cmd, env, cwd_str, prompt)
    if err_result is not None:
        return err_result

    timeout_int = max(1, int(total_deadline))
    stdout, stderr, status = _drain_buffered(proc, timeout_int)

    if status == "timeout":
        _kill_proc(proc)
        return GeminiSpawnResult(
            returncode=1, completion_text="", events_written=0,
            session_id=None, timed_out=True,
            error=f"gemini CLI exceeded {timeout_int}s timeout",
        )

    if proc.returncode != 0:
        return GeminiSpawnResult(
            returncode=proc.returncode, completion_text="",
            events_written=0, session_id=None, timed_out=False,
            error=(stderr or stdout)[:500],
        )

    text = _parse_gemini_response(stdout)
    token_usage = _parse_gemini_token_usage(stdout)
    synthetic = {"type": "result", "data": text, "observability_tier": _TIER_LEGACY}

    if event_writer is not None:
        try:
            event_writer(terminal_id, synthetic, dispatch_id=dispatch_id)
        except Exception as _exc:
            logger.debug("spawn_gemini: event_writer (legacy) raised: %s", _exc)

    return GeminiSpawnResult(
        returncode=0, completion_text=text, events_written=1,
        session_id=None, timed_out=False, token_usage=token_usage,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _drain_buffered(proc: subprocess.Popen, timeout: int) -> tuple:
    """Read stdout/stderr with a wall-clock timeout; returns (stdout, stderr, status)."""
    stdout_parts: list = []
    stderr_parts: list = []
    start = time.monotonic()
    stdout_fd = proc.stdout.fileno() if proc.stdout else -1
    stderr_fd = proc.stderr.fileno() if proc.stderr else -1
    fd_map: dict = {}
    if stdout_fd >= 0:
        fd_map[stdout_fd] = "stdout"
    if stderr_fd >= 0:
        fd_map[stderr_fd] = "stderr"
    raw_fds = list(fd_map)

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return (
                b"".join(stdout_parts).decode("utf-8", errors="replace"),
                b"".join(stderr_parts).decode("utf-8", errors="replace"),
                "timeout",
            )
        remaining = max(timeout - elapsed, 0.1)
        try:
            readable, _, _ = _select_mod.select(raw_fds, [], [], min(remaining, 1.0))
        except (ValueError, OSError):
            break
        for fd_num in readable:
            try:
                chunk = os.read(fd_num, 4096)
            except OSError:
                chunk = b""
            if chunk:
                if fd_map.get(fd_num) == "stdout":
                    stdout_parts.append(chunk)
                else:
                    stderr_parts.append(chunk)
        if proc.poll() is not None:
            for fd_num in raw_fds:
                try:
                    while True:
                        rem = os.read(fd_num, 4096)
                        if not rem:
                            break
                        if fd_map.get(fd_num) == "stdout":
                            stdout_parts.append(rem)
                        else:
                            stderr_parts.append(rem)
                except OSError:
                    pass
            break

    return (
        b"".join(stdout_parts).decode("utf-8", errors="replace"),
        b"".join(stderr_parts).decode("utf-8", errors="replace"),
        "ok",
    )


def _kill_proc(proc: subprocess.Popen) -> None:
    """Send SIGTERM then SIGKILL to process group."""
    import signal as _signal
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, _signal.SIGTERM)
        time.sleep(0.2)
        os.killpg(pgid, _signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
