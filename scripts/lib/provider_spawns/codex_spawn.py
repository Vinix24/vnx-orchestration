"""codex_spawn.py — Codex-specific spawn handler extracted from codex_adapter.

Extracted in Wave 4.6 PR-4.6.3. This module owns the "pure spawn+stream" slice:

  1. Spawn `codex exec --json` via subprocess.Popen with stdin pipe for prompt.
  2. Stream stdout NDJSON; normalize to CanonicalEvent dicts via the normalizer
     logic (single implementation; codex_adapter delegates here for byte identity).
  3. Tick health_monitor on each event.
  4. Invoke optional on_event callback per event (return False to stop early).

Callers handle: lease/manifest/receipt/event-archive/retry/changed-files context.

BILLING SAFETY: only subprocess.Popen(["codex", "exec", "--json"]) is invoked.
No Anthropic SDK, no LiteLLM, no remote-API direct calls.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from _streaming_drainer import StreamingDrainerMixin  # noqa: E402
from canonical_event import CanonicalEvent  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_CODEX_MODEL = "gpt-5.5"


@dataclass
class CodexSpawnResult:
    """Return value from spawn_codex(); carries spawn outcome to the caller."""

    returncode: int
    # Final result text (concatenated agent_message content from the stream).
    completion_text: str
    # Number of normalized events processed.
    events_written: int
    # codex session identifier from thread.started/session_start event, else None.
    session_id: Optional[str]
    # True when chunk_timeout or total_deadline was breached.
    timed_out: bool
    # True when on_event returned False (early stream termination).
    stopped_early: bool = False
    # Token usage extracted from the stream's final token_count event, else None.
    token_usage: Optional[Dict[str, Any]] = None
    # Set when an exception terminated the stream read.
    error: Optional[str] = None
    # Number of times event_writer callback raised an exception.
    # > 0 indicates audit-trail gaps the caller must investigate per ADR-005.
    event_writer_failures: int = 0

    def frontmatter_fields(self) -> Dict[str, Any]:
        usage = self.token_usage or {}
        return {
            "provider": "codex",
            "sub_provider": "openai",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("input_tokens", 0) or 0),
                "output": int(usage.get("output_tokens", 0) or 0),
                "cache_read": int(usage.get("cache_read_tokens", 0) or 0),
            },
        }


# ---------------------------------------------------------------------------
# Normalizer helpers — single implementation; codex_adapter shims delegate here
# ---------------------------------------------------------------------------

_TOKEN_TEXT_RE = re.compile(
    r"tokens?(?:\s+used)?:\s*([\d,]+)\s+input(?:\s*/|,)(?:\s*[\d,]+\s+cached(?:\s*/|,))?\s*([\d,]+)\s+output",
    re.IGNORECASE,
)


def _extract_token_count_payload(event: dict) -> Optional[dict]:
    """Locate a `token_count` payload inside a Codex NDJSON event dict."""
    if not isinstance(event, dict):
        return None
    em = event.get("event_msg")
    if isinstance(em, dict):
        payload = em.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            return payload
        if em.get("type") == "token_count":
            return em
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            return usage
    usage = event.get("usage")
    if isinstance(usage, dict):
        return usage
    msg = event.get("msg")
    if isinstance(msg, dict) and msg.get("type") == "token_count":
        return msg
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "token_count":
        return item
    if event.get("type") in ("token_count", "token_usage"):
        return event
    return None


def _parse_token_int(value: Any) -> Optional[int]:
    """Return a token count from int or comma-formatted string values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.replace(",", "").strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _normalize_token_count(payload: dict) -> Optional[dict]:
    """Normalize a Codex token_count payload to the canonical token_usage dict."""
    if not isinstance(payload, dict):
        return None
    input_t = _parse_token_int(payload.get("input_tokens"))
    if input_t is None:
        input_t = _parse_token_int(payload.get("prompt_tokens")) or 0
    output_t = _parse_token_int(payload.get("output_tokens"))
    if output_t is None:
        output_t = _parse_token_int(payload.get("completion_tokens")) or 0
    if input_t == 0 and output_t == 0:
        return None
    cache_read = _parse_token_int(payload.get("cached_input_tokens"))
    if cache_read is None:
        cache_read = _parse_token_int(payload.get("cache_read_tokens")) or 0
    cache_creation = _parse_token_int(payload.get("cache_creation_input_tokens"))
    if cache_creation is None:
        cache_creation = _parse_token_int(payload.get("cache_creation_tokens")) or 0
    return {
        "input_tokens": int(input_t),
        "output_tokens": int(output_t),
        "cache_creation_tokens": int(cache_creation),
        "cache_read_tokens": int(cache_read),
    }


def _extract_token_count_from_text(text: str) -> Optional[dict]:
    """Parse Codex's human-readable token summary line when it appears."""
    match = _TOKEN_TEXT_RE.search(text or "")
    if not match:
        return None
    return _normalize_token_count({
        "input_tokens": match.group(1),
        "output_tokens": match.group(2),
    })


# ---------------------------------------------------------------------------
# Normalizer internals — split to stay ≤70 lines per function
# ---------------------------------------------------------------------------

def _resolve_codex_event_parts(raw: dict) -> Tuple[str, dict, dict, str]:
    """Extract (etype, payload, item, item_type) from a raw Codex NDJSON event."""
    top_etype = raw.get("type", "")
    payload: dict = raw
    event_msg = raw.get("event_msg")
    if isinstance(event_msg, dict):
        inner = event_msg.get("payload")
        if isinstance(inner, dict):
            payload = inner
        elif isinstance(event_msg.get("type"), str):
            payload = event_msg

    etype = payload.get("type", "") if isinstance(payload, dict) else ""

    if top_etype and (
        top_etype.startswith("item.")
        or top_etype.startswith("thread.")
        or top_etype.startswith("turn.")
    ):
        etype = top_etype
        payload = raw

    item: dict = {}
    raw_item = raw.get("item") or (payload.get("item") if payload is not raw else None)
    if isinstance(raw_item, dict):
        item = raw_item
    return etype, payload, item, item.get("type", "")


def _normalize_text_events(
    etype: str,
    payload: dict,
    item: dict,
    item_type: str,
    make: Callable,
) -> Optional[CanonicalEvent]:
    """Handle init, agent_message, item.completed[agent], item.*[command] events."""
    if etype in ("thread.started", "session_start"):
        return make("init", {"raw_type": etype})
    if etype == "agent_message":
        content = payload.get("text", payload.get("content", payload.get("message", "")))
        data = {"text": str(content)}
        token_count = _extract_token_count_from_text(str(content))
        if token_count:
            data["token_count"] = token_count
        return make("text", data)
    if etype == "item.completed" and item_type == "agent_message":
        content = item.get("text", item.get("content", ""))
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict)]
            content = "\n".join(t for t in texts if t)
        data = {"text": str(content)}
        token_count = _extract_token_count_from_text(str(content))
        if token_count:
            data["token_count"] = token_count
        return make("text", data)
    if etype in ("item.started", "item.updated") and item_type == "command_execution":
        cmd_str = item.get("command", item.get("cmd", item.get("args", "")))
        if isinstance(cmd_str, list):
            cmd_str = " ".join(str(a) for a in cmd_str)
        return make("tool_use", {"command": str(cmd_str), "raw_type": etype})
    if etype == "item.completed" and item_type == "command_execution":
        output = item.get("output", item.get("result", ""))
        return make("tool_result", {"output": str(output), "exit_code": item.get("exit_code", 0)})
    return None


def _normalize_complete_events(
    etype: str,
    raw: dict,
    payload: dict,
    make: Callable,
) -> Optional[CanonicalEvent]:
    """Handle error, turn.completed, result/message, token_count events."""
    if etype == "error":
        msg = payload.get("message", payload.get("error", payload.get("text", "")))
        return make("error", {"message": str(msg) if msg else str(payload)[:200]})
    if etype == "turn.completed":
        tc = _extract_token_count_payload(raw)
        token_count = _normalize_token_count(tc) if tc else None
        data: dict = {"token_count": token_count} if token_count else {}
        return make("complete", data)
    if etype in ("result", "message"):
        content = payload.get("content", payload.get("text", payload.get("output", "")))
        tc = _extract_token_count_payload(raw)
        token_count = _normalize_token_count(tc) if tc else None
        if token_count is None:
            usage = raw.get("usage") or payload.get("usage")
            if isinstance(usage, dict):
                token_count = _normalize_token_count({
                    "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens", 0),
                })
        data = {"text": str(content)} if content else {}
        if token_count:
            data["token_count"] = token_count
        return make("complete", data)
    tc = _extract_token_count_payload(raw)
    if tc is not None:
        token_count = _normalize_token_count(tc)
        if token_count:
            return make("text", {"text": "", "token_count": token_count})
    return None


def normalize_codex_event(raw: dict, terminal_id: str, dispatch_id: str) -> CanonicalEvent:
    """Map a raw Codex NDJSON event to a CanonicalEvent (Tier-1).

    Single canonical implementation; both _NormalizerHost (used by spawn_codex)
    and CodexAdapter._normalize (used by stream_events) delegate here.
    """
    def make(event_type: str, data: dict) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="codex",
            event_type=event_type,
            data=data,
            observability_tier=1,
        )

    etype, payload, item, item_type = _resolve_codex_event_parts(raw)

    result = _normalize_text_events(etype, payload, item, item_type, make)
    if result is not None:
        return result

    result = _normalize_complete_events(etype, raw, payload, make)
    if result is not None:
        return result

    # Unknown codex event type — non-fatal passthrough, same rationale as kimi.
    logger.debug("codex_spawn: unknown event type %r — mapping to info (non-fatal)", etype)
    return make("info", {
        "raw_type": etype,
        "raw": str(raw)[:300],
    })


def _resolve_codex_model(model: Optional[str]) -> str:
    """Resolve explicit model first, then env default, then the current CLI default."""
    requested = (model or "").strip()
    if requested:
        return requested
    return os.environ.get("VNX_CODEX_DEFAULT_MODEL", "").strip() or DEFAULT_CODEX_MODEL


def _build_cmd(model: Optional[str]) -> list:
    """Build the codex exec argv.

    `codex exec` runs non-interactively so approvals auto-downgrade to `never`, BUT the
    SANDBOX still applies. Without an explicit sandbox flag codex runs read-only and cannot
    write files or run commands — so any multi-file worker task fails fast (rc=1) after a
    few seconds. This was the t2+ "launch-flakiness": not launch/quota, the sandbox blocking
    file ops (t1 squeaked through on minimal ops). Worker dispatch needs file+command access.

    Default to the full bypass — the worker runs in an ISOLATED worktree cell (same posture
    as claude's --dangerously-skip-permissions in the bench). Override via VNX_CODEX_SANDBOX
    ("workspace-write" = recommended safer level that still allows workspace edits + sandboxed
    command exec; "read-only"/"danger-full-access" also accepted).
    """
    cmd = ["codex", "exec", "--json"]
    sandbox = (os.environ.get("VNX_CODEX_SANDBOX", "") or "").strip()
    if sandbox in ("workspace-write", "read-only", "danger-full-access"):
        cmd += ["--sandbox", sandbox]
    else:
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    cmd += ["--model", _resolve_codex_model(model)]
    return cmd


# ---------------------------------------------------------------------------
# Internal normalizer host (composes StreamingDrainerMixin for drain_stream)
# ---------------------------------------------------------------------------

class _NormalizerHost(StreamingDrainerMixin):
    """Minimal state holder so StreamingDrainerMixin can call normalize_codex_event."""

    provider_name = "codex"
    provider_observability_tier = 1

    def __init__(self, terminal_id: str, dispatch_id: str) -> None:
        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

    def _normalize(self, raw: dict) -> CanonicalEvent:
        return normalize_codex_event(raw, self._current_terminal_id, self._current_dispatch_id)


# ---------------------------------------------------------------------------
# Spawn helpers — split to stay ≤70 lines per function
# ---------------------------------------------------------------------------

def _launch_codex_proc(
    prompt: str,
    model: Optional[str],
    extra_env: Optional[Dict[str, str]],
    cwd: Optional[Any],
) -> Tuple[Optional[subprocess.Popen], Optional[CodexSpawnResult]]:
    """Start codex subprocess and write prompt to stdin.

    Returns (proc, None) on success, or (None, error_result) on failure.
    Returns (None, CodexSpawnResult(returncode=127)) when binary is missing.
    """
    cmd = _build_cmd(model)
    env = {**os.environ, **(extra_env or {})}
    cwd_str = str(cwd) if cwd is not None else None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd_str,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return None, CodexSpawnResult(
            returncode=127,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage=None,
            error=f"codex binary not found: {e}",
            event_writer_failures=0,
        )
    except OSError as exc:
        return None, CodexSpawnResult(
            returncode=1, completion_text="", events_written=0,
            session_id=None, timed_out=False, error=str(exc),
        )

    # Drain codex stderr in a daemon thread. Unlike SubprocessAdapter (claude/deepseek),
    # which logs stderr to a file (the H1 fix), codex_spawn's _drain_stream reads ONLY stdout
    # and never proc.stderr. With an undrained PIPE, codex BLOCKS once it emits >~64KB of
    # stderr — and codex with model_reasoning_effort=xhigh (config.toml) emits a lot — which
    # surfaced as the codex "flakiness" (t1 stayed under 64KB and passed; t2+ filled the pipe
    # in 5-17s, the review task in ~92s, all exiting rc=1 with incomplete work). Drain it and
    # keep a bounded tail for error reporting.
    import threading
    proc._vnx_stderr_tail = []  # type: ignore[attr-defined]
    if proc.stderr is not None:
        def _drain_stderr(p, buf):
            try:
                for line in iter(p.stderr.readline, b""):
                    buf.append(line)
                    if len(buf) > 200:
                        del buf[:100]
            except (ValueError, OSError):
                pass
        threading.Thread(
            target=_drain_stderr, args=(proc, proc._vnx_stderr_tail), daemon=True,
        ).start()

    if proc.stdin:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            return None, CodexSpawnResult(
                returncode=1, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                error="stdin write failed (BrokenPipeError): codex process exited early",
            )
    return proc, None


def _process_one_event(
    canonical_event: CanonicalEvent,
    terminal_id: str,
    dispatch_id: str,
    health_monitor: Optional[Any],
    event_writer: Optional[Callable],
    on_event: Optional[Callable],
    HealthStatus: Any,
    SLOW_THRESHOLD: float,
    last_stuck_log: float,
    events_written: int = 0,
) -> Tuple[bool, float, bool]:
    """Process one canonical event: health tick, event_writer, on_event.

    Returns (stop_requested, updated_last_stuck_log, writer_failed).
    """
    if health_monitor is not None:
        health_monitor.update(canonical_event)
        if HealthStatus is not None:
            now = time.monotonic()
            if now - last_stuck_log >= SLOW_THRESHOLD:
                h = health_monitor.health_status()
                if h.status == HealthStatus.STUCK:
                    health_monitor.log_stuck_event()
                    last_stuck_log = now

    writer_failed = False
    if event_writer is not None:
        try:
            event_writer(
                terminal_id,
                canonical_event.to_dict(),
                dispatch_id=dispatch_id,
            )
        except Exception as _exc:
            # event_writer is caller-supplied (typically writes to NDJSON ledger).
            # Failures are ADR-005 audit gaps — log as ERROR + count for caller inspection.
            logger.error(
                "spawn_codex: event_writer callback failed (dispatch=%s, event_count=%d): %s",
                dispatch_id, events_written, _exc,
            )
            writer_failed = True

    if on_event is not None and on_event(canonical_event) is False:
        return True, last_stuck_log, writer_failed
    return False, last_stuck_log, writer_failed


def _wait_proc(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Wait for proc to exit; kill it on timeout."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _drain_stream(
    normalizer: _NormalizerHost,
    proc: subprocess.Popen,
    terminal_id: str,
    dispatch_id: str,
    event_store: Optional[Any],
    health_monitor: Optional[Any],
    event_writer: Optional[Callable],
    on_event: Optional[Callable],
    chunk_timeout: float,
    total_deadline: float,
    HealthStatus: Any,
    SLOW_THRESHOLD: float,
) -> Tuple[list, int, Optional[str], bool, bool, Optional[Dict[str, Any]], Optional[str], int]:
    """Drain codex NDJSON stream; return (parts, count, session_id, timed_out, stopped, usage, error, writer_failures)."""
    completion_parts: list = []
    events_written = 0
    _event_writer_failures = 0
    session_id: Optional[str] = None
    timed_out = False
    stopped_early = False
    last_token_usage: Optional[Dict[str, Any]] = None
    last_stuck_log = 0.0

    try:
        for canonical_event in normalizer.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=chunk_timeout, total_deadline=total_deadline,
        ):
            events_written += 1

            if canonical_event.event_type == "init" and session_id is None:
                session_id = (canonical_event.data or {}).get("session_id")
            if canonical_event.event_type in ("text", "complete"):
                text = (canonical_event.data or {}).get("text", "")
                if text:
                    completion_parts.append(text)
                tc = (canonical_event.data or {}).get("token_count")
                if tc:
                    last_token_usage = tc
            if canonical_event.event_type == "error":
                reason = (canonical_event.data or {}).get("reason", "")
                if "timeout" in (reason or "").lower():
                    timed_out = True

            stop, last_stuck_log, writer_failed = _process_one_event(
                canonical_event, terminal_id, dispatch_id,
                health_monitor, event_writer, on_event,
                HealthStatus, SLOW_THRESHOLD, last_stuck_log,
                events_written=events_written,
            )
            if writer_failed:
                _event_writer_failures += 1
            if stop:
                stopped_early = True
                try:
                    _kill_proc(proc)
                except Exception as _exc:
                    logger.debug("spawn_codex: kill after on_event=False failed: %s", _exc)
                break

    except Exception as exc:
        logger.error("spawn_codex: stream read failure %s/%s: %s", dispatch_id, terminal_id, exc)
        try:
            _kill_proc(proc)
        except Exception as _kill_exc:
            logger.debug("spawn_codex: kill in error handler failed: %s", _kill_exc)
        _wait_proc(proc)
        return completion_parts, events_written, session_id, timed_out, stopped_early, last_token_usage, str(exc), _event_writer_failures

    return completion_parts, events_written, session_id, timed_out, stopped_early, last_token_usage, None, _event_writer_failures


# ---------------------------------------------------------------------------
# Main spawn function
# ---------------------------------------------------------------------------

def spawn_codex(
    prompt: str,
    model: Optional[str],
    dispatch_id: str,
    terminal_id: str,
    *,
    event_writer: Optional[Callable[..., None]] = None,
    event_writer_strict: bool = False,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    event_store: Optional[Any] = None,
    chunk_timeout: float = 60.0,
    total_deadline: float = 600.0,
    **kwargs: Any,
) -> CodexSpawnResult:
    """Spawn `codex exec --json` and consume the NDJSON event stream.

    Returns CodexSpawnResult on completion (success OR controlled failure).
    Returns CodexSpawnResult(returncode=127) when the codex binary is absent.
    Caller is responsible for lease/manifest/receipt/event-archive/retry.

    event_writer_strict=True raises RuntimeError if any event_writer call failed,
    for callers that require strict ADR-005 audit-trail integrity.
    """
    try:
        from worker_health_monitor import HealthStatus, SLOW_THRESHOLD
    except ImportError:
        HealthStatus = None  # type: ignore[assignment]
        SLOW_THRESHOLD = 120.0

    try:
        chunk_timeout = float(os.environ["VNX_CODEX_STALL_THRESHOLD"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_CODEX_TIMEOUT"])
    except (KeyError, ValueError):
        pass

    proc, early_result = _launch_codex_proc(prompt, model, extra_env, cwd)
    if early_result is not None:
        return early_result

    normalizer = _NormalizerHost(terminal_id=terminal_id, dispatch_id=dispatch_id)

    parts, events_written, session_id, timed_out, stopped_early, token_usage, error, _event_writer_failures = _drain_stream(
        normalizer, proc, terminal_id, dispatch_id, event_store,
        health_monitor, event_writer, on_event,
        chunk_timeout, total_deadline, HealthStatus, SLOW_THRESHOLD,
    )

    if error is None:
        _wait_proc(proc, timeout=10.0)

    returncode = proc.returncode if proc.returncode is not None else 1

    # Surface codex's stderr tail on failure (previously discarded → "rc=1" with no reason).
    if returncode != 0 or error:
        tail_lines = getattr(proc, "_vnx_stderr_tail", None) or []
        tail = b"".join(tail_lines[-60:]).decode("utf-8", "ignore").strip()[-1200:]
        if tail:
            error = ((error + " | ") if error else "") + f"codex stderr tail: {tail}"

    if event_writer_strict and _event_writer_failures > 0:
        raise RuntimeError(
            f"spawn_codex: event_writer failed {_event_writer_failures} time(s) "
            f"(dispatch={dispatch_id}) — strict audit mode requires zero failures"
        )

    return CodexSpawnResult(
        returncode=returncode if error is None else (returncode if returncode != 0 else 1),
        completion_text="\n\n".join(parts),
        events_written=events_written,
        session_id=session_id,
        timed_out=timed_out,
        stopped_early=stopped_early,
        token_usage=token_usage,
        error=error,
        event_writer_failures=_event_writer_failures,
    )


def _kill_proc(proc: subprocess.Popen) -> None:
    """Send SIGTERM then SIGKILL to process group."""
    import signal as _signal
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, _signal.SIGTERM)
        time.sleep(0.2)
        os.killpg(pgid, _signal.SIGKILL)
    except (ProcessLookupError, OSError):
        proc.kill()
        try:
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired) as exc:
            # Process already terminated or wait timed out — both acceptable
            # at kill-fallback boundary. Log for forensics.
            logger.warning(
                "_kill_proc: fallback wait failed (pid=%s): %s",
                proc.pid, exc,
            )
