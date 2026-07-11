"""litellm_spawn.py — LiteLLM-specific spawn handler extracted from litellm_adapter.

Extracted in Wave 4.6 PR-4.6.5. This module owns the "pure spawn+stream" slice:

  1. Spawn _litellm_runner.py via sys.executable -u with a JSON payload on stdin.
  2. Read OpenAI-shaped NDJSON output; normalize to CanonicalEvent objects via
     normalize_litellm_event().
  3. Tick health_monitor on each event.
  4. Invoke optional on_event callback per event (return False to stop early).

Callers handle: lease/manifest/receipt/event-archive/retry.

BILLING SAFETY: only subprocess.Popen([sys.executable, "-u", runner_path]) is
invoked. No Anthropic SDK, no direct API calls. LiteLLM is isolated to
_litellm_runner.py subprocess.

CREDENTIAL SAFETY (audit S2): every Popen env for these subprocesses is built via
_scrubbed_env(), which strips ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN after
merging the parent env — an external/untrusted model driven through the agentic
runner's run_command tool cannot read the Anthropic account's own credentials.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from _streaming_drainer import StreamingDrainerMixin  # noqa: E402
from canonical_event import CanonicalEvent  # noqa: E402

logger = logging.getLogger(__name__)

# Default runner path: adapters/_litellm_runner.py (sibling to litellm_adapter.py)
_RUNNER_PATH = Path(__file__).resolve().parents[1] / "adapters" / "_litellm_runner.py"

_TIER_STREAMING = 1

# Audit S2: these lanes exclusively drive non-Anthropic models (DeepSeek, OpenRouter, GLM,
# etc. via litellm) in a subprocess the external model itself can direct — the one-shot
# runner via completion(), the agentic runner via its run_command tool (shell=True, no
# allowlist; audit S1). Full os.environ inheritance would hand the Anthropic account's own
# credentials to that subprocess, readable/exfiltratable by an external/untrusted model
# through run_command. Neither key is ever needed by a litellm-routed child, so both are
# scrubbed from every Popen env unconditionally (not caller-optional — there is no legitimate
# case where a litellm child needs them).
_CREDENTIAL_SCRUB_KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def _scrubbed_env(extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Merge extra_env over the parent env, then strip Anthropic-account credentials."""
    env = {**os.environ, **(extra_env or {})}
    for _key in _CREDENTIAL_SCRUB_KEYS:
        env.pop(_key, None)
    return env


# ---------------------------------------------------------------------------
# Normalizer — single canonical implementation (extracted from litellm_adapter)
# ---------------------------------------------------------------------------

def normalize_litellm_event(
    chunk: Dict[str, Any],
    terminal_id: str,
    dispatch_id: str,
    *,
    sub_provider: Optional[str] = None,
    lane: Optional[str] = None,
) -> CanonicalEvent:
    """Map an OpenAI-shaped NDJSON chunk to a CanonicalEvent (Tier-1).

    Both _LiteLLMNormalizerHost (used by spawn_litellm) and
    LiteLLMAdapter._normalize delegate here to guarantee byte identity.
    Priority: error_type -> tool_calls -> finish_reason -> init -> text.
    sub_provider and lane are stored in CanonicalEvent for ADR-016 audit enrichment.
    """
    provider_meta: Dict[str, Any] = {}
    if lane is not None:
        provider_meta["lane"] = lane

    def make(etype: str, data: dict) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="litellm",
            event_type=etype,
            data=data,
            observability_tier=_TIER_STREAMING,
            sub_provider=sub_provider,
            provider_meta=provider_meta,
        )

    error_type = chunk.get("error_type")
    if error_type:
        return make("error", {"error_type": error_type, "message": chunk.get("message", "")})

    if chunk.get("event_type") == "usage_complete":
        return make("usage_complete", {"usage": chunk.get("usage") or {}})

    choices = chunk.get("choices") or []
    choice = choices[0] if choices else {}
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")

    if delta.get("tool_calls"):
        return make("tool_use", {"tool_calls": delta["tool_calls"]})

    if finish_reason in ("stop", "tool_calls", "end_turn", "length"):
        return make("complete", {"finish_reason": finish_reason, "model": chunk.get("model", "")})

    if delta.get("role") == "assistant" and not delta.get("content"):
        return make("init", {"model": chunk.get("model", "")})

    return make("text", {"content": delta.get("content") or ""})


# ---------------------------------------------------------------------------
# Internal normalizer host (composes StreamingDrainerMixin for drain_stream)
# ---------------------------------------------------------------------------

class _LiteLLMNormalizerHost(StreamingDrainerMixin):
    """Minimal state holder so StreamingDrainerMixin can call normalize_litellm_event."""

    provider_name = "litellm"
    provider_observability_tier = _TIER_STREAMING

    def __init__(
        self,
        terminal_id: str,
        dispatch_id: str,
        *,
        sub_provider: Optional[str] = None,
        lane: Optional[str] = None,
    ) -> None:
        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id
        self._sub_provider = sub_provider
        self._lane = lane

    def _normalize(self, raw: Dict[str, Any]) -> CanonicalEvent:
        return normalize_litellm_event(
            raw,
            self._current_terminal_id,
            self._current_dispatch_id,
            sub_provider=self._sub_provider,
            lane=self._lane,
        )



# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class LiteLLMSpawnResult:
    """Return value from spawn_litellm(); carries spawn outcome to the caller."""

    returncode: int
    completion_text: str
    events_written: int
    session_id: Optional[str]
    timed_out: bool
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # Number of times event_writer callback raised an exception.
    # > 0 indicates audit-trail gaps the caller must investigate per ADR-005.
    event_writer_failures: int = 0

    def frontmatter_fields(self) -> Dict[str, Any]:
        usage = self.token_usage or {}
        cache_read = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_read = int(details.get("cached_tokens", 0) or 0)
        if not cache_read:
            cache_read = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        return {
            "provider": "litellm",
            "sub_provider": "none",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("prompt_tokens", 0) or 0),
                "output": int(usage.get("completion_tokens", 0) or 0),
                "cache_read": cache_read,
            },
        }


# ---------------------------------------------------------------------------
# Contract enforcement helpers
# ---------------------------------------------------------------------------

def _validate_tool_shape(prompt: str, tool_call_shape: Optional[str]) -> None:
    """Raise ValueError when prompt embeds tool definitions in the wrong format.

    Detects Anthropic-format tool markers (input_schema) in prompts destined
    for openai_tools lanes — a mismatch that would produce silent call failures.
    Only fires when tool_call_shape is explicitly provided by a BehaviorContract.
    """
    if not tool_call_shape:
        return
    if tool_call_shape == "openai_tools" and '"input_schema"' in prompt:
        raise ValueError(
            f"Prompt embeds Anthropic tool format (input_schema) but lane "
            f"contract expects {tool_call_shape!r} — tool calls would fail"
        )


# ---------------------------------------------------------------------------
# Subprocess spawn helpers
# ---------------------------------------------------------------------------

def _build_litellm_cmd(runner_path: str) -> list:
    """Build argv: [sys.executable, '-u', runner_path]."""
    return [sys.executable, "-u", runner_path]


def _start_litellm_subprocess(
    runner_path: str,
    payload_json: str,
    extra_env: Optional[Dict[str, str]],
    cwd: Optional[Any],
) -> Tuple[Optional[subprocess.Popen], Optional[LiteLLMSpawnResult]]:
    """Start the litellm runner subprocess and write payload to stdin.

    Returns (proc, None) on success, or (None, LiteLLMSpawnResult) on spawn failure.
    All subprocess-boundary errors convert to structured results; none are re-raised.
    """
    cmd = _build_litellm_cmd(runner_path)
    env = _scrubbed_env(extra_env)
    cwd_str = str(cwd) if cwd is not None else None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            cwd=cwd_str,
        )
    except FileNotFoundError as exc:
        return None, LiteLLMSpawnResult(
            returncode=127, completion_text="", events_written=0,
            session_id=None, timed_out=False, stopped_early=False,
            token_usage=None, error=f"litellm runner not found: {exc}",
        )
    except OSError as exc:
        return None, LiteLLMSpawnResult(
            returncode=126, completion_text="", events_written=0,
            session_id=None, timed_out=False, stopped_early=False,
            token_usage=None, error=f"failed to spawn litellm runner: {exc}",
        )

    if proc.stdin:
        try:
            proc.stdin.write(payload_json.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError as exc:
            return None, LiteLLMSpawnResult(
                returncode=1, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                error=f"stdin write failed (BrokenPipeError): {exc}",
            )

    return proc, None


def _consume_litellm_stream(
    proc: subprocess.Popen,
    host: "_LiteLLMNormalizerHost",
    on_event: Optional[Callable],
    health_monitor: Optional[Any],
    event_writer: Optional[Callable],
    terminal_id: str,
    dispatch_id: str,
    event_store: Optional[Any],
    chunk_timeout: float,
    total_deadline: float,
) -> Tuple[str, int, bool, bool, int, Optional[Dict[str, Any]]]:
    """Drain the NDJSON stream; return (completion_text, events_written, timed_out, stopped_early, writer_failures, token_usage)."""
    events_written = 0
    completion_parts: list = []
    stopped_early = False
    timed_out = False
    _event_writer_failures = 0
    last_token_usage: Optional[Dict[str, Any]] = None

    for canonical_event in host.drain_stream(
        proc, terminal_id, dispatch_id, event_store,
        chunk_timeout=chunk_timeout, total_deadline=total_deadline,
    ):
        events_written += 1
        evt_type = canonical_event.event_type

        if evt_type == "text":
            content = (canonical_event.data or {}).get("content", "")
            if content:
                completion_parts.append(content)
        elif evt_type == "error":
            reason = ((canonical_event.data or {}).get("reason") or "").lower()
            if "timeout" in reason or "deadline" in reason:
                timed_out = True
        elif evt_type == "usage_complete":
            usage = (canonical_event.data or {}).get("usage")
            if isinstance(usage, dict):
                last_token_usage = usage

        if health_monitor is not None:
            health_monitor.update(canonical_event)

        if event_writer is not None:
            try:
                event_writer(terminal_id, canonical_event.to_dict(), dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.error(
                    "spawn_litellm: event_writer callback failed (dispatch=%s, event_count=%d): %s",
                    dispatch_id, events_written, _exc,
                )
                _event_writer_failures += 1

        if on_event is not None:
            if on_event(canonical_event) is False:
                stopped_early = True
                try:
                    proc.kill()
                except OSError as _ke:
                    logger.debug("spawn_litellm: kill after on_event=False failed: %s", _ke)
                break

    return "".join(completion_parts), events_written, timed_out, stopped_early, _event_writer_failures, last_token_usage


def _finalize_litellm_result(
    proc: subprocess.Popen,
    completion_text: str,
    events_written: int,
    timed_out: bool,
    stopped_early: bool,
    event_writer_failures: int = 0,
    error: Optional[str] = None,
    token_usage: Optional[Dict[str, Any]] = None,
) -> LiteLLMSpawnResult:
    """Wait for process exit and return a LiteLLMSpawnResult."""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    rc = proc.returncode if proc.returncode is not None else 1
    return LiteLLMSpawnResult(
        returncode=rc if error is None else (rc if rc != 0 else 1),
        completion_text=completion_text,
        events_written=events_written,
        session_id=None,
        timed_out=timed_out,
        stopped_early=stopped_early,
        event_writer_failures=event_writer_failures,
        error=error,
        token_usage=token_usage,
    )


# ---------------------------------------------------------------------------
# Streaming helper — extracted to keep spawn_litellm ≤70 lines
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
    extra_env: Optional[Dict[str, str]],
    cwd: Optional[Any],
    chunk_timeout: float,
    total_deadline: float,
    event_store: Optional[Any],
    runner_path: str,
    sub_provider: Optional[str] = None,
    lane: Optional[str] = None,
) -> LiteLLMSpawnResult:
    """Spawn _litellm_runner.py and drain the NDJSON event stream."""
    payload_json = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    })
    proc, err_result = _start_litellm_subprocess(runner_path, payload_json, extra_env, cwd)
    if err_result is not None:
        return err_result

    host = _LiteLLMNormalizerHost(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        sub_provider=sub_provider,
        lane=lane,
    )
    completion_text, events_written, timed_out, stopped_early, _ew_failures, token_usage = _consume_litellm_stream(
        proc=proc, host=host, on_event=on_event,
        health_monitor=health_monitor, event_writer=event_writer,
        terminal_id=terminal_id, dispatch_id=dispatch_id,
        event_store=event_store, chunk_timeout=chunk_timeout,
        total_deadline=total_deadline,
    )
    return _finalize_litellm_result(
        proc=proc, completion_text=completion_text,
        events_written=events_written, timed_out=timed_out,
        stopped_early=stopped_early, event_writer_failures=_ew_failures,
        token_usage=token_usage,
    )


# ---------------------------------------------------------------------------
# Main spawn function
# ---------------------------------------------------------------------------

def spawn_litellm(
    prompt: str,
    model: str,
    dispatch_id: str,
    terminal_id: str,
    *,
    sub_provider: Optional[str] = None,
    lane: Optional[str] = None,
    tool_call_shape: Optional[str] = None,
    event_writer: Optional[Callable[..., None]] = None,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    event_store: Optional[Any] = None,
    runner_path: Optional[str] = None,
    chunk_timeout: float = 60.0,
    total_deadline: float = 600.0,
    **kwargs: Any,
) -> LiteLLMSpawnResult:
    """Spawn _litellm_runner.py and consume the NDJSON event stream.

    Returns LiteLLMSpawnResult on completion (success OR controlled failure).
    Returns LiteLLMSpawnResult(returncode=127) when the runner binary is absent.
    Caller is responsible for lease/manifest/receipt/event-archive/retry.

    model must be a full LiteLLM model string, e.g. "bedrock/claude-sonnet-4-6".
    When model is empty, falls back to VNX_LITELLM_MODEL env var or a sub_provider
    default, then to "anthropic/claude-sonnet-4-6".

    lane and tool_call_shape are sourced from BehaviorContract (Wave 7 PR-7.5).
    tool_call_shape triggers pre-spawn validation for format mismatch detection.
    sub_provider and lane are propagated to audit events per ADR-016.
    """
    try:
        chunk_timeout = float(os.environ.get("VNX_LITELLM_STALL_THRESHOLD", chunk_timeout))
    except (TypeError, ValueError):
        pass
    try:
        total_deadline = float(os.environ.get("VNX_LITELLM_TIMEOUT", total_deadline))
    except (TypeError, ValueError):
        pass

    try:
        _validate_tool_shape(prompt, tool_call_shape)
    except ValueError as exc:
        return LiteLLMSpawnResult(
            returncode=64, completion_text="", events_written=0,
            session_id=None, timed_out=False, error=str(exc),
        )

    if not model:
        env_model = os.environ.get("VNX_LITELLM_MODEL", "")
        if env_model:
            model = env_model
        elif sub_provider:
            model = f"{sub_provider}/default"
        else:
            model = "anthropic/claude-sonnet-4-6"

    _runner = runner_path or str(_RUNNER_PATH)

    return _spawn_streaming(
        prompt=prompt,
        model=model,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        event_writer=event_writer,
        health_monitor=health_monitor,
        on_event=on_event,
        extra_env=extra_env,
        cwd=cwd,
        chunk_timeout=chunk_timeout,
        total_deadline=total_deadline,
        event_store=event_store,
        runner_path=_runner,
        sub_provider=sub_provider,
        lane=lane,
    )


# ---------------------------------------------------------------------------
# Agentic spawn — drives _litellm_agentic_runner.py (tool-use loop)
# ---------------------------------------------------------------------------

# Agentic runner: gives the model file/shell tools and iterates until it
# finishes (vs the one-shot _RUNNER_PATH which gives no tools). Required for
# agentic coding tasks where the model must actually write files and run tests.
_AGENTIC_RUNNER_PATH = Path(__file__).resolve().parents[1] / "adapters" / "_litellm_agentic_runner.py"


def _agentic_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def spawn_litellm_agentic(
    prompt: str,
    model: str,
    dispatch_id: str,
    terminal_id: str,
    *,
    sub_provider: Optional[str] = None,
    lane: Optional[str] = None,
    event_writer: Optional[Callable[..., None]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    runner_path: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_tokens: Optional[int] = None,
    command_timeout: Optional[int] = None,
    total_deadline: Optional[float] = None,
    **kwargs: Any,
) -> LiteLLMSpawnResult:
    """Agentic counterpart of spawn_litellm.

    Drives ``_litellm_agentic_runner.py``, which gives the model
    read_file / write_file / list_dir / run_command tools and iterates the
    tool-use loop until the model stops or hits max_turns. The model writes its
    deliverables directly into ``cwd`` (the isolated worker cell), so agentic
    coding tasks produce real files — unlike the one-shot ``spawn_litellm``.

    Returns the same LiteLLMSpawnResult shape so callers (_emit_governance,
    frontmatter token_usage) need no special-casing.
    """
    max_turns = max_turns or _agentic_int_env("VNX_LITELLM_AGENTIC_MAX_TURNS", 30)
    max_tokens = max_tokens or _agentic_int_env("VNX_LITELLM_AGENTIC_MAX_TOKENS", 8192)
    command_timeout = command_timeout or _agentic_int_env("VNX_LITELLM_AGENTIC_CMD_TIMEOUT", 120)
    if total_deadline is None:
        total_deadline = float(_agentic_int_env("VNX_LITELLM_AGENTIC_DEADLINE", 1800))

    if not model:
        model = (
            os.environ.get("VNX_LITELLM_MODEL", "")
            or (f"{sub_provider}/default" if sub_provider else "anthropic/claude-sonnet-4-6")
        )

    _runner = runner_path or str(_AGENTIC_RUNNER_PATH)
    payload_json = json.dumps({
        "model": model,
        "prompt": prompt,
        "cwd": str(cwd) if cwd is not None else os.getcwd(),
        "max_turns": max_turns,
        "max_tokens": max_tokens,
        "command_timeout": command_timeout,
    })

    # Start the runner ourselves and drive stdin/stdout via communicate(input=...).
    # (We can't reuse _start_litellm_subprocess: it pre-writes AND closes stdin, which
    # makes a later communicate() raise "flush of closed file".)
    cmd = _build_litellm_cmd(_runner)
    env = _scrubbed_env(extra_env)
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=env, cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError as exc:
        return LiteLLMSpawnResult(
            returncode=127, completion_text="", events_written=0, session_id=None,
            timed_out=False, error=f"agentic runner not found: {exc}",
        )
    except OSError as exc:
        return LiteLLMSpawnResult(
            returncode=126, completion_text="", events_written=0, session_id=None,
            timed_out=False, error=f"failed to spawn agentic runner: {exc}",
        )

    timed_out = False
    try:
        stdout_b, _stderr_b = proc.communicate(input=payload_json.encode("utf-8"), timeout=total_deadline)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_b, _stderr_b = proc.communicate()
        timed_out = True

    completion_parts: list = []
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    events_written = 0
    ew_failures = 0

    def _canon(etype: str, data: dict) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="litellm",
            event_type=etype,
            data=data,
            observability_tier=_TIER_STREAMING,
            sub_provider=sub_provider,
            provider_meta={"lane": lane} if lane is not None else {},
        )

    text = stdout_b.decode("utf-8", "replace") if stdout_b else ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        events_written += 1

        if "error_type" in evt:
            error = evt.get("message") or evt.get("error_type")
            canonical = _canon("error", {"error_type": evt.get("error_type"), "message": evt.get("message", "")})
        else:
            etype = evt.get("event_type") or "info"
            if etype == "text":
                content = evt.get("content") or ""
                if content:
                    completion_parts.append(content)
                canonical = _canon("text", {"content": evt.get("content", "")})
            elif etype == "usage_complete":
                usage = evt.get("usage")
                if isinstance(usage, dict):
                    token_usage = {
                        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    }
                canonical = _canon("usage_complete", {"usage": evt.get("usage") or {}})
            else:
                canonical = _canon(etype, {k: v for k, v in evt.items() if k != "event_type"})

        if event_writer is not None:
            try:
                event_writer(terminal_id, canonical.to_dict(), dispatch_id=dispatch_id)
            except Exception as _exc:  # noqa: BLE001
                logger.error(
                    "spawn_litellm_agentic: event_writer callback failed (dispatch=%s): %s",
                    dispatch_id, _exc,
                )
                ew_failures += 1

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    rc = proc.returncode if proc.returncode is not None else 1
    if error and rc == 0:
        rc = 1

    return LiteLLMSpawnResult(
        returncode=rc,
        completion_text="".join(completion_parts),
        events_written=events_written,
        session_id=None,
        timed_out=timed_out,
        stopped_early=False,
        event_writer_failures=ew_failures,
        error=error,
        token_usage=token_usage,
    )
