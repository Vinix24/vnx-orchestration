"""kimi_spawn.py — Kimi CLI subprocess spawn handler (Wave 7.7).

Owns: spawn + stream-json parsing + canonical event normalization.
Caller (provider_dispatch.py) handles: receipt, unified report, lease, etc.

Kimi CLI invocation:
    kimi --print --output-format stream-json --yolo -p "<prompt>" [-m <model>] [-w <worktree>]

``--yolo`` is always passed — see ``_build_kimi_cmd`` for why. ``-w`` scopes the
agent to the dispatch's isolated worktree when one is supplied; without it kimi
would auto-dismiss its own tool calls (fabrication) rather than execute them.

Authentication: OAuth via `kimi login` (operator-managed). No API key in spawn.

OUTPUT FORMAT (kimi-cli 1.44.0, wire protocol 1.10):
    `--output-format stream-json` emits Anthropic-style content-block message
    objects, one JSON object per line, with NO ``event_type`` field. Each line:

        {"role": "assistant", "content": [
            {"type": "think", "think": "<reasoning>"},
            {"type": "text",  "text":  "<answer>"}],
         "tool_calls": [{"type": "function", "id": "...",
                         "function": {"name": "...", "arguments": "..."}}]}
        {"role": "tool", "content": [{"type": "text", "text": "..."}],
         "tool_call_id": "..."}

    The answer text lives in ``content[]`` blocks where ``type == "text"`` (field
    ``text``); reasoning in blocks where ``type == "think"`` (field ``think``).
    The whole assistant message arrives end-loaded (after the model finishes
    thinking), so per-chunk stall detection must tolerate a long first-token gap.
    Token/usage accounting is NOT reported by this format — usage is recorded as
    explicitly-unavailable rather than a silently-measured zero.

    Legacy ``event_type`` event-stream shapes (pre-1.44) are still parsed for
    backward compatibility.

BILLING SAFETY: only subprocess.Popen(["kimi", ...]) is invoked.
No Anthropic SDK, no LiteLLM, no direct API calls.
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

    @property
    def token_usage_measured(self) -> bool:
        """True when actual token accounting was observed in the stream.

        kimi-cli 1.44.0 stream-json does not report token accounting, so this
        is False when token_usage is None (no token_count event was observed).
        Available as an attribute for receipt/metadata consumers without being
        part of the cross-provider frontmatter_fields() contract.
        """
        return self.token_usage is not None

    def frontmatter_fields(self) -> Dict[str, Any]:
        # kimi-cli 1.44.0 stream-json reports no token accounting, and completion_text
        # is the (often empty) final message — not the agentic generation volume — so
        # there is no reliable token count to report. Honest "unavailable" zeros with
        # token_usage_measured=False; the scorer renders tokens/sec as n/a for kimi.
        usage = self.token_usage or {}
        return {
            "provider": "kimi",
            "sub_provider": "moonshot",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("input_tokens", 0) or 0),
                "output": int(usage.get("output_tokens", 0) or 0),
                "cache_read": int(usage.get("cache_read_tokens", 0) or 0),
            },
        }


def _extract_content_blocks(content: list) -> "tuple[str, str]":
    """Join the text and reasoning from a 1.44.0 ``content[]`` block list.

    Returns ``(text, reasoning)`` where ``text`` concatenates every block with
    ``type == "text"`` (field ``text``) and ``reasoning`` concatenates every
    block with ``type == "think"`` (field ``think``, falling back to ``text``).
    Unknown block types are ignored (non-fatal).
    """
    texts: list = []
    thinks: list = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            value = block.get("text")
            if value:
                texts.append(str(value))
        elif btype == "think":
            value = block.get("think") or block.get("text")
            if value:
                thinks.append(str(value))
    return "".join(texts), "\n".join(thinks)


def normalize_kimi_event(raw: dict, terminal_id: str, dispatch_id: str) -> CanonicalEvent:
    """Map a raw Kimi CLI stream-json event to a CanonicalEvent (Tier-1).

    Supports three event formats:

    Content-block message (kimi-cli 1.44.0, wire protocol 1.10):
      {"role": "assistant", "content": [{"type": "think", "think": "..."},
                                        {"type": "text", "text": "..."}],
       "tool_calls": [...]}                 -> text (+ reasoning, tool_calls)
      {"role": "assistant", "content": "plain answer text"}   -> text
       (kimi-cli sometimes emits the FINAL assistant message with content
        as a plain STRING instead of an array-of-blocks; the string itself
        is the answer text, equivalent to a single text block)
      {"role": "tool", "content": [{"type": "text", "text": "..."}],
       "tool_call_id": "..."}               -> tool_result
      {"role": "user"|"system"|other, "content": [...]}  -> info (non-fatal;
       NEVER extracted as answer text — guards against an echoed prompt/
       transcript turn being concatenated into completion_text)

    Below: legacy ``event_type`` shapes, retained for backward compatibility.

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

    Unknown event_type values map to "info" events (non-fatal passthrough, never returns None).
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

    # Detect HTTP-error-like JSON responses (e.g. {"status": 403, "message": "quota exceeded"})
    # that kimi CLI may emit before or instead of stream-json events.
    http_status = raw.get("status") or raw.get("code") or raw.get("error_code") or 0
    try:
        http_status = int(http_status)
    except (TypeError, ValueError):
        http_status = 0
    raw_msg = raw.get("message") or raw.get("msg") or raw.get("error") or ""
    if http_status in (401, 403, 429) or (not event_type and _is_quota_or_auth_error(str(raw_msg))):
        return make("error", {
            "reason": "quota_or_auth",
            "message": f"[quota_or_auth] provider=kimi http_status={http_status} raw={str(raw)[:200]}",
        })

    # kimi-cli 1.44.0 content-block message: {"role": ..., "content": [...]}.
    # Detected by a list-valued ``content`` field with a string ``role`` and no
    # legacy ``event_type``. Legacy events carry ``content`` as a string, so this
    # check never shadows them.
    role = raw.get("role")
    content = raw.get("content")
    if not event_type and isinstance(content, list) and isinstance(role, str):
        if role == "tool":
            text, reasoning = _extract_content_blocks(content)
            return make("tool_result", {
                "tool_use_id": str(raw.get("tool_call_id", "")),
                "content": text or reasoning,
            })
        if role == "assistant":
            # The answer text is the payload; reasoning + tool_calls ride along
            # for observability but never become completion text. Intermediate
            # tool-call turns legitimately carry an empty text block — only the
            # final assistant message contributes text.
            text, reasoning = _extract_content_blocks(content)
            data: Dict[str, Any] = {"text": text}
            if reasoning:
                data["reasoning"] = reasoning
            tool_calls = raw.get("tool_calls")
            if tool_calls:
                data["tool_calls"] = tool_calls
            return make("text", data)
        # Any other role (e.g. "user" — an echoed prompt/transcript turn, or
        # "system") must NEVER be extracted as answer text. Deep-fix for the
        # #763 info-bug follow-up: the original content-block detection keyed
        # only on "isinstance(role, str)" and treated every non-"tool" role as
        # assistant output, so a CLI that echoes the user's own turn back into
        # the stream (common for transcript/session-resume modes) would have
        # its prompt silently concatenated into completion_text. Route to
        # "info" instead: non-fatal, does not corrupt completion_text, and
        # still counts toward saw_stream_output so fail-loud still fires if no
        # real assistant text ever arrives.
        logger.debug(
            "kimi_spawn: content-block role=%r is not assistant/tool — mapping to info", role
        )
        return make("info", {
            "raw_type": f"role:{role}",
            "raw": str(raw)[:300],
        })

    # Plain-string assistant content: kimi-cli sometimes emits the FINAL
    # assistant message with ``content`` as a bare string rather than an
    # array-of-blocks. The string itself IS the answer text (equivalent to a
    # single {"type": "text", "text": <str>} block). Scoped strictly to
    # role == "assistant" with NO legacy event_type: legacy events also carry
    # string content but are routed via their event_type below, so this check
    # never shadows them. An empty/whitespace-only string still yields empty
    # text here, so the fail-loud guard in _finalize_kimi_result stays intact
    # for the genuinely-empty case. The string-content shape has no separate
    # reasoning/tool_calls, so those stay empty for it.
    if not event_type and role == "assistant" and isinstance(content, str):
        return make("text", {"text": content})

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

    # Unknown event type — map to "info" (non-fatal passthrough).
    # An unrecognized informational event must not flip the dispatch status to
    # failure: "info" falls through the consumer's error-capture branch so it
    # never enters errors_captured, never sets result.error, and never forces
    # rc = 1 on an otherwise-successful completion.
    logger.debug("kimi_spawn: unknown event_type %r — mapping to info (non-fatal)", event_type)
    return make("info", {
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


_QUOTA_OR_AUTH_PATTERNS = frozenset({
    "403", "quota", "rate_limit", "ratelimit", "rate limit",
    "unauthorized", "unauthenticated", "forbidden", "authentication",
    "token expired", "invalid token", "access denied",
})


def _is_quota_or_auth_error(text: str) -> bool:
    """Return True when text contains a kimi quota / auth / 403 signal."""
    lower = (text or "").lower()
    return any(pat in lower for pat in _QUOTA_OR_AUTH_PATTERNS)


def _build_kimi_cmd(prompt: str, model: Optional[str], work_dir: Optional[Any]) -> list:
    """Build the kimi argv list.

    ``--yolo`` is always passed (confirmed against kimi-cli 1.46.0 ``--help``):
    kimi's ``--print`` mode is non-interactive but still AUTO-DISMISSES tool-call
    approval prompts without ``--yolo``/``--yes``/``-y`` — the model emits
    tool_call intent, nothing actually runs, and the dispatch comes back
    GATE-GREEN with zero real file edits (the fabrication bug this fixes).
    ``--yolo`` here is the same posture as codex's default
    ``--dangerously-bypass-approvals-and-sandbox``: the dispatch worktree
    (``-w``, when supplied) bounds the blast radius exactly like codex's
    isolated worktree cell. Never silent — ``spawn_kimi`` logs the effective
    argv (and, when an event sink is wired, records it in the event stream so
    it lands in the receipt), and ``_finalize_kimi_result`` fails loud if the
    stream shows tool_calls with no corresponding worktree diff.
    """
    cmd = ["kimi", "--print", "--output-format", "stream-json", "--yolo"]
    cmd.extend(["-p", prompt])
    if model:
        cmd.extend(["-m", model])
    if work_dir:
        cmd.extend(["-w", str(work_dir)])
    return cmd


def _worktree_has_changes(worktree: Any) -> Optional[bool]:
    """Return True/False for uncommitted git changes in *worktree*, or None if unknown.

    None means the check itself could not be performed (git missing, *worktree*
    is not a git repo, or the command timed out) — callers must treat that as
    "cannot verify" and skip the fabrication-invariant rather than treating an
    inability to check as evidence of fabrication.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "kimi_spawn: fabrication-invariant git status failed for %s (skipping check): %s",
            worktree, exc,
        )
        return None
    if proc.returncode != 0:
        logger.warning(
            "kimi_spawn: fabrication-invariant git status exited %d for %s (skipping check): %s",
            proc.returncode, worktree, (proc.stderr or "")[:200],
        )
        return None
    return bool(proc.stdout.strip())


# Inherited venv-activation vars that point Python at a FOREIGN site-packages.
# The kimi CLI is a standalone `uv tool` with its own isolated venv; if VNX is
# invoked from inside an unrelated project's virtualenv (e.g. a worker spawned
# under SEOcrawler's .venv), these vars shadow kimi's own dependencies and the
# CLI dies on an import collision (live-proven: `mcp.types` clash → exit 1 in
# ~0.6s, 0 tokens, no review). kimi must always run with a clean Python env.
_VENV_POLLUTION_VARS = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")


def _isolate_kimi_env(env: Dict[str, str]) -> Dict[str, str]:
    """Return *env* without the venv-activation vars that break the kimi CLI.

    Stripped unconditionally (after any extra_env merge): a standalone uv tool
    has no legitimate use for an inherited VIRTUAL_ENV/PYTHONPATH/PYTHONHOME, and
    leaving them in lets a foreign venv's site-packages shadow kimi's own deps.
    """
    return {k: v for k, v in env.items() if k not in _VENV_POLLUTION_VARS}


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
) -> "tuple[str, int, Optional[Dict], bool, bool, int, list, bool, list, bool]":
    """Drain the stream.

    Returns (completion_text, events_written, token_usage, timed_out,
    stopped_early, failures, errors_captured, saw_stream_output, raw_samples,
    saw_tool_calls).

    ``saw_stream_output`` is True once the CLI emits any real message line (text,
    tool, thinking, or info event). Combined with an empty ``completion_text`` it
    is the fail-loud signal: output arrived but no answer text was extracted.
    ``raw_samples`` carries short excerpts of those lines for the error message.

    ``saw_tool_calls`` is True once the stream shows evidence kimi attempted to
    execute a tool (a legacy ``tool_use``/``tool_result`` event, or a 1.44.0+
    content-block assistant message carrying a ``tool_calls`` list). Feeds the
    completion-vs-execution invariant in ``_finalize_kimi_result``: intent to
    call a tool with no corresponding worktree diff is the fabrication signature.
    """
    events_written = 0
    completion_parts: list = []
    token_usage: Optional[Dict[str, Any]] = None
    stopped_early = False
    timed_out = False
    _event_writer_failures = 0
    errors_captured: list = []
    saw_stream_output = False
    saw_tool_calls = False
    raw_samples: list = []

    _CONTENT_EVENT_TYPES = ("text", "tool_use", "tool_result", "thinking", "info")

    for canonical_event in host.drain_stream(
        proc, terminal_id, dispatch_id, event_store,
        chunk_timeout=chunk_timeout, total_deadline=total_deadline,
    ):
        events_written += 1
        evt_type = canonical_event.event_type

        if evt_type in _CONTENT_EVENT_TYPES:
            saw_stream_output = True
            if len(raw_samples) < 6:
                try:
                    raw_samples.append(json.dumps(canonical_event.data)[:200])
                except (TypeError, ValueError):
                    raw_samples.append(str(canonical_event.data)[:200])

        if evt_type in ("tool_use", "tool_result"):
            saw_tool_calls = True
        elif evt_type == "text" and (canonical_event.data or {}).get("tool_calls"):
            saw_tool_calls = True

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
            raw_line = data.get("raw", "")
            msg_text = data.get("message") or data.get("reason") or str(data)[:200]
            # Detect quota / auth / 403 signals from non-JSON lines or JSON error bodies.
            # The drainer stores the original line in data["raw"] when JSON parsing fails.
            if _is_quota_or_auth_error(raw_line) or _is_quota_or_auth_error(str(msg_text)):
                errors_captured.append(
                    f"[quota_or_auth] provider=kimi reason=quota_or_auth"
                    f" msg={str(msg_text)[:200]!r} raw={str(raw_line)[:200]!r}"
                )
            else:
                errors_captured.append(str(msg_text))

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

    return (
        "".join(completion_parts), events_written, token_usage, timed_out,
        stopped_early, _event_writer_failures, errors_captured,
        saw_stream_output, raw_samples, saw_tool_calls,
    )


def _finalize_kimi_result(
    proc: subprocess.Popen,
    completion_text: str,
    events_written: int,
    token_usage: Optional[Dict[str, Any]],
    timed_out: bool,
    stopped_early: bool,
    event_writer_failures: int,
    errors_captured: Optional[list] = None,
    saw_stream_output: bool = False,
    raw_samples: Optional[list] = None,
    saw_tool_calls: bool = False,
    worktree: Optional[Any] = None,
) -> KimiSpawnResult:
    """Wait for process exit and return a KimiSpawnResult.

    ``saw_tool_calls`` + ``worktree`` feed the completion-vs-execution invariant:
    when the stream showed kimi attempting to call a tool, a clean result is only
    accepted if the dispatch worktree actually changed. This defends against a
    FUTURE regression silently re-introducing fabrication even with ``--yolo``
    present (e.g. kimi dismissing its own tool call, or a CLI update changing
    approval defaults again). ``worktree=None`` (no isolation worktree known,
    e.g. non-worktree dispatches) skips the check gracefully — there is nothing
    to diff against.
    """
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    rc = proc.returncode if proc.returncode is not None else 1

    empty_extraction = not (completion_text or "").strip()

    worktree_unchanged = False
    if saw_tool_calls and worktree is not None:
        worktree_unchanged = _worktree_has_changes(worktree) is False

    if errors_captured:
        error: Optional[str] = "\n".join(errors_captured)
        if rc == 0:
            rc = 1  # error event overrides false-success zero exit code
    elif empty_extraction and saw_stream_output and not stopped_early:
        # FAIL-LOUD: the CLI emitted message lines but text extraction yielded
        # ZERO characters — almost always a kimi-cli output-format change (the
        # 1.44.0 content-block regression). NEVER report this as a silent
        # success with an empty report; surface it as a failure with the raw
        # output captured so the format drift is diagnosable.
        sample = " | ".join(raw_samples or []) or "(no sample captured)"
        error = (
            "kimi returned a non-empty response but text extraction yielded ZERO "
            "characters — likely a kimi-cli stream-json format change. "
            f"events={events_written} raw_event_sample={sample}"
        )
        if rc == 0:
            rc = 1
    elif worktree_unchanged:
        # FAIL-LOUD: the stream shows kimi attempting to call a tool, but the
        # dispatch worktree has no git changes — completion without execution,
        # the exact fabrication pattern --yolo exists to prevent. Never accept
        # this as a silent clean success.
        error = (
            "kimi emitted tool_calls but the dispatch worktree shows no git "
            "changes — completion without execution (fabrication guard). "
            f"worktree={worktree} events={events_written}"
        )
        if rc == 0:
            rc = 1
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
    chunk_timeout: float = 600.0,
    total_deadline: float = 900.0,
    event_store: Optional[Any] = None,
    **kwargs: Any,
) -> KimiSpawnResult:
    """Spawn ``kimi --print --output-format stream-json --yolo -p <prompt>``.

    Returns KimiSpawnResult on completion (success OR controlled failure).
    Returns KimiSpawnResult(returncode=127) when the kimi binary is absent.
    Caller is responsible for lease/manifest/receipt/event-archive/retry.

    The per-chunk stall default is 600s (overridable via VNX_KIMI_STALL_THRESHOLD):
    Kimi is a reasoning model whose 1.44.0 content-block output is end-loaded, so
    the first token can arrive only after a long reasoning gap (a 300s default
    spuriously killed adversarial-review dispatches mid-think). A FAILURE is
    returned (never a silent empty
    success) when the CLI emits output but no answer text is extracted.

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

    env = _isolate_kimi_env({**os.environ, **(extra_env or {})})
    cwd_str = str(cwd) if cwd is not None else None

    cmd = _build_kimi_cmd(prompt, model, cwd)

    # Never launch --yolo silently: log the effective argv (prompt redacted to a
    # char count) and, when an event sink is wired, record it as an "info" event
    # so it lands in the archived event stream the receipt points to via
    # events_path — the "always logged, never hidden" posture for YOLO mode.
    _redacted_argv = [tok if tok != prompt else f"<prompt:{len(prompt)}chars>" for tok in cmd]
    logger.info(
        "kimi_spawn: launching kimi -p <%d chars> -m %s effective_argv=%s",
        len(prompt),
        cmd[cmd.index("-m") + 1] if "-m" in cmd else "default",
        _redacted_argv,
    )
    _flags_sink = event_writer or (event_store.append if event_store is not None else None)
    if _flags_sink is not None:
        try:
            _flags_event = CanonicalEvent(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="kimi",
                event_type="info",
                data={
                    "kind": "effective_argv",
                    "argv": _redacted_argv,
                    "yolo": "--yolo" in cmd,
                    "work_dir": cwd_str,
                },
                observability_tier=1,
            )
            _flags_sink(terminal_id, _flags_event.to_dict(), dispatch_id=dispatch_id)
        except Exception as _flags_exc:
            logger.debug(
                "kimi_spawn: effective-argv event emission failed (non-fatal): %s", _flags_exc,
            )

    proc, err_result = _start_kimi_subprocess(cmd, env, cwd_str)
    if err_result is not None:
        return err_result

    host = _KimiNormalizerHost(terminal_id=terminal_id, dispatch_id=dispatch_id)
    (
        completion_text, events_written, token_usage, timed_out, stopped_early,
        _event_writer_failures, errors_captured, saw_stream_output, raw_samples,
        saw_tool_calls,
    ) = _consume_kimi_stream(
        proc=proc, host=host, on_event=on_event,
        health_monitor=health_monitor, event_writer=event_writer,
        terminal_id=terminal_id, dispatch_id=dispatch_id,
        event_store=event_store, chunk_timeout=chunk_timeout,
        total_deadline=total_deadline,
    )
    return _finalize_kimi_result(
        proc=proc, completion_text=completion_text,
        events_written=events_written, token_usage=token_usage,
        timed_out=timed_out, stopped_early=stopped_early,
        event_writer_failures=_event_writer_failures,
        errors_captured=errors_captured,
        saw_stream_output=saw_stream_output,
        raw_samples=raw_samples,
        saw_tool_calls=saw_tool_calls,
        worktree=cwd,
    )
