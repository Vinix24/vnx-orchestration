"""kimi_spawn.py — Kimi CLI subprocess spawn handler (Wave 7.7).

Owns: spawn + stream-json parsing + canonical event normalization.
Caller (provider_dispatch.py) handles: receipt, unified report, lease, etc.

Kimi CLI invocation:
    kimi --print --output-format stream-json --yolo -p "<prompt>" [-m <model>] [-w <worktree>]

``--yolo`` is always passed — see ``_build_kimi_cmd`` for why. ``-w`` scopes the
agent to the dispatch's isolated worktree when one is supplied; without it kimi
would auto-dismiss its own tool calls (fabrication) rather than execute them.

Authentication: OAuth via `kimi login` (operator-managed). No API key in spawn.

OUTPUT FORMAT (kimi-cli 1.46.0, wire protocol 1.10):
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
    Token/usage accounting is NOT reported by this format — measured on
    kimi-cli 1.46.0 (the installed CLI) on both plain and tool-using workloads.
    The default ``--print`` event-stream (no ``--output-format``) DOES carry a
    ``StatusUpdate`` event with ``token_usage=TokenUsage(input_other, output,
    input_cache_read, input_cache_creation)``, but as Python-repr display text,
    not NDJSON — the line-based drainer would mis-parse it. The measured tokens
    are instead recovered post-run from the session's ``wire.jsonl`` via
    ``kimi export <session_id>`` (see ``_harvest_session_token_usage``). If that
    harvest yields nothing, usage is recorded as explicitly-unavailable rather
    than a silently-measured zero.

    Legacy ``event_type`` event-stream shapes (pre-1.44) are still parsed for
    backward compatibility.

BILLING SAFETY: only subprocess.Popen(["kimi", ...]) is invoked.
No Anthropic SDK, no LiteLLM, no direct API calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from _streaming_drainer import StreamingDrainerMixin, _kill_process, coerce_chunk_stall  # noqa: E402
from canonical_event import CanonicalEvent  # noqa: E402
# OI-1087: the read-only task-class list lives in ONE place — phantom_guard's
# REVIEW_TASK_CLASSES is the fabric's SSOT for "a verdict, not a diff, is the
# expected deliverable". The fabrication guard below keys off the same list so
# the two guards can never drift apart.
from phantom_guard import REVIEW_TASK_CLASSES  # noqa: E402

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
        """True when actual token accounting was observed.

        kimi-cli 1.46.0 stream-json does not report token accounting in the
        stream; tokens are recovered post-run from the session's ``wire.jsonl``
        (see ``_harvest_session_token_usage``). This is False when token_usage
        is None — either the harvest was skipped (failed dispatch) or the
        session export yielded no StatusUpdate. Available as an attribute for
        receipt/metadata consumers without being part of the cross-provider
        frontmatter_fields() contract.
        """
        return self.token_usage is not None

    def frontmatter_fields(self) -> Dict[str, Any]:
        # kimi-cli 1.46.0 stream-json reports no token accounting in the stream,
        # and completion_text is the (often empty) final message — not the agentic
        # generation volume. Measured tokens arrive via the post-run wire.jsonl
        # harvest (token_usage set) or stay honestly unavailable (token_usage=None,
        # zeros + token_usage_measured=False; the scorer renders tokens/sec as n/a).
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


# kimi prints the resume handle to stderr after every --print run:
#   "To resume this session: kimi -r <session_id>"
# The session id is the key to the session's wire.jsonl, where the CLI records
# the measured token accounting that stream-json itself never emits.
_SESSION_ID_RE = re.compile(r"kimi\s+(-r|--resume)\s+([0-9a-fA-F-]+)")


def _extract_session_id(stderr_text: str) -> Optional[str]:
    """Return the kimi session id from the stderr resume line, or None.

    ``kimi --print`` writes ``To resume this session: kimi -r <id>`` to stderr
    on every run. The id is a UUID-ish string (dashes allowed). Missing or
    malformed stderr returns None — the caller then skips the token harvest and
    stays honestly unavailable.
    """
    m = _SESSION_ID_RE.search(stderr_text or "")
    return m.group(2) if m else None


def _parse_wire_token_usage(wire_jsonl: str) -> Optional[Dict[str, int]]:
    """Aggregate ``StatusUpdate.token_usage`` from a session ``wire.jsonl``.

    Each line is ``{"timestamp": ..., "message": {"type": "...", "payload": ...}}``.
    A ``StatusUpdate`` payload carries the model call's accounting as
    ``token_usage=TokenUsage(input_other, output, input_cache_read,
    input_cache_creation)``. ``input_other`` and ``output`` are per-call NEW
    tokens — summed across calls for the run total. ``input_cache_read`` is the
    cumulative context-cache read for that call, so the LAST one is the run
    total; ``input_cache_creation`` likewise.

    Returns None when no StatusUpdate with a token_usage payload is present
    (fail-open: the caller reports unavailable rather than a fabricated zero).
    Malformed lines are skipped non-fatally.
    """
    total_input = 0
    total_output = 0
    last_cache_read = 0
    last_cache_creation = 0
    found = False
    for line in (wire_jsonl or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("type") != "StatusUpdate":
            continue
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            continue
        tu = payload.get("token_usage")
        if not isinstance(tu, dict):
            continue
        found = True
        total_input += int(tu.get("input_other", 0) or 0)
        total_output += int(tu.get("output", 0) or 0)
        last_cache_read = int(tu.get("input_cache_read", 0) or 0)
        last_cache_creation = int(tu.get("input_cache_creation", 0) or 0)
    if not found:
        return None
    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": last_cache_read,
        "cache_creation_tokens": last_cache_creation,
    }


def _harvest_session_token_usage(
    session_id: str,
    env: Dict[str, str],
    cwd_str: Optional[str] = None,
    timeout: float = 30.0,
) -> Optional[Dict[str, int]]:
    """Export the session and read StatusUpdate token_usage from its wire.jsonl.

    Keeps the lane's token-less stream-json output (canonical events unchanged)
    while still reporting the API's measured token accounting: after a clean
    run, ``kimi export <session_id> -o <tmp.zip> --yes`` materialises the
    session archive and the ``wire.jsonl`` inside it records every StatusUpdate
    the CLI saw. Fail-open: any failure (export error, missing wire.jsonl, no
    StatusUpdate) returns None — the receipt stays 'unavailable', never broken.
    """
    zip_path: Optional[Path] = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="kimi-wire-")
        zip_path = Path(tmp_dir) / "session.zip"
        export_cmd = ["kimi", "export", session_id, "-o", str(zip_path), "--yes"]
        proc = subprocess.run(
            export_cmd,
            capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd_str,
        )
        if proc.returncode != 0 or not zip_path.exists():
            logger.debug(
                "kimi_spawn: session export failed (rc=%s) — tokens stay unavailable",
                proc.returncode,
            )
            return None
        with zipfile.ZipFile(zip_path) as zf:
            wire = zf.read("wire.jsonl").decode("utf-8", errors="replace")
        return _parse_wire_token_usage(wire)
    except Exception as exc:  # noqa: BLE001 — harvest must never break the dispatch
        logger.debug("kimi_spawn: token harvest failed (fail-open): %s", exc)
        return None
    finally:
        if zip_path is not None:
            try:
                import shutil as _shutil
                _shutil.rmtree(zip_path.parent, ignore_errors=True)
            except Exception:  # vnx-silent-except: cleanup must never raise
                pass


# kimi-cli persists one resumable session dir per invocation with no auto-GC,
# TTL, or no-persist flag. One-shot --print dispatches are never resumed, so
# without an explicit reap the session dirs (wire.jsonl + context.jsonl +
# state.json) accrue unbounded (543 dirs / 697MB on 2026-07-28, manually
# cleaned to 316M). OI-812: after the token harvest (which still needs the
# export), the dead session is reaped.
_SESSION_ID_VALUE_RE = re.compile(r"[0-9a-fA-F-]+\Z")


def _kimi_share_dir(env: Dict[str, str]) -> Path:
    """Resolve the kimi share dir exactly as kimi-cli 1.46.0 does.

    ``kimi_cli/share.py::get_share_dir`` returns ``KIMI_SHARE_DIR`` when set,
    else ``~/.kimi``. The reap must resolve the SAME dir the spawned kimi wrote
    to, so it reads the env the subprocess was launched with, not the ambient
    process env.
    """
    override = (env or {}).get("KIMI_SHARE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kimi"


def _find_kimi_session_dir(session_id: str, env: Dict[str, str]) -> Optional[Path]:
    """Locate the on-disk session dir for *session_id*, or None.

    Sessions live at ``<share_dir>/sessions/<work_dir_md5>/<session_id>/``.
    The outer bucket is the md5 of the work-dir path, which the resume id alone
    cannot tell us, so every bucket under ``sessions/`` is scanned for a
    directory named exactly *session_id*. Returns None when the session is not
    on disk (already reaped, or the share dir has no sessions subtree).
    """
    if not session_id or not _SESSION_ID_VALUE_RE.match(session_id):
        return None
    sessions_root = _kimi_share_dir(env) / "sessions"
    if not sessions_root.is_dir():
        return None
    try:
        for bucket in sessions_root.iterdir():
            if not bucket.is_dir():
                continue
            candidate = bucket / session_id
            if candidate.is_dir():
                return candidate
    except OSError as exc:
        logger.debug("kimi_spawn: session-dir scan failed (fail-open): %s", exc)
        return None
    return None


def _reap_kimi_session(session_id: str, env: Dict[str, str]) -> bool:
    """Remove the on-disk session dir of a one-shot kimi dispatch.

    One-shot dispatches are never resumed, so after the token harvest the
    session dir is dead weight: reap it. Best-effort — a reap failure (or a
    session already gone) must never break an otherwise-clean dispatch, so this
    returns False instead of raising.

    Safety: only a directory whose path is exactly
    ``<share_dir>/sessions/<bucket>/<session_id>`` is ever removed — the leaf
    must equal *session_id* and sit two levels under the sessions subtree, the
    layout kimi's ``WorkDirMeta.sessions_dir`` always produces. Anything else
    (the sessions root, a bucket dir, a depth-1 dir, a path outside the tree)
    is refused with a warning.
    """
    if not session_id or not _SESSION_ID_VALUE_RE.match(session_id):
        return False
    share_dir = _kimi_share_dir(env)
    sessions_root = share_dir / "sessions"
    session_dir = _find_kimi_session_dir(session_id, env)
    if session_dir is None:
        return False
    try:
        resolved = session_dir.resolve()
        root_resolved = sessions_root.resolve()
    except OSError as exc:
        logger.warning("kimi_spawn: could not resolve reap path %s: %s", session_dir, exc)
        return False
    try:
        rel = resolved.relative_to(root_resolved)
    except ValueError:
        logger.warning(
            "kimi_spawn: refusing to reap %s (outside kimi sessions tree %s)",
            resolved, root_resolved,
        )
        return False
    if len(rel.parts) != 2 or rel.parts[1] != session_id:
        logger.warning(
            "kimi_spawn: refusing to reap %s (not a two-level session dir under %s)",
            resolved, root_resolved,
        )
        return False
    try:
        shutil.rmtree(resolved, ignore_errors=True)
    except Exception:  # vnx-silent-except: reap must never break the dispatch
        return False
    reaped = not resolved.exists()
    if reaped:
        logger.debug("kimi_spawn: reaped one-shot kimi session dir %s", resolved)
    return reaped


def _worktree_has_changes(worktree: Any, base_ref: str = "origin/main") -> Optional[bool]:
    """Return True/False for REAL git work in *worktree*, or None if unknown.

    True  — uncommitted/untracked changes (non-empty ``git status --porcelain``),
            OR committed work whose TREE differs from the merge-base with
            *base_ref* (the normal fix-forward: worker committed and pushed).
    False — clean tree AND either no commits at all (unborn HEAD) or a committed
            tree identical to the merge-base (genuine no-op, ``--allow-empty``
            commit, or revert-back-to-base — all fabrication).
    None  — the check itself could not be performed (git missing, *worktree* is
            not a git repo, timeout, or unresolvable *base_ref*) — callers must
            treat that as "cannot verify" and skip the fabrication-invariant
            rather than treating an inability to check as evidence of
            fabrication.

    The committed-work half mirrors ``phantom_guard.compute_worktree_diff``: it
    compares TREES (``git diff --quiet <merge-base> HEAD``), never commit COUNT,
    so an empty commit or a commit reverting to the base tree cannot bypass the
    guard.
    """
    wt = str(worktree)

    def _git(args: list) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                ["git", *args], cwd=wt, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "kimi_spawn: fabrication-invariant git %s failed for %s (skipping check): %s",
                args[0] if args else "?", wt, exc,
            )
            return None

    # 1. Uncommitted / untracked work.
    status = _git(["status", "--porcelain"])
    if status is None:
        return None
    if status.returncode != 0:
        logger.warning(
            "kimi_spawn: fabrication-invariant git status exited %d for %s (skipping check): %s",
            status.returncode, wt, (status.stderr or "")[:200],
        )
        return None
    if status.stdout.strip():
        return True

    # 2. Clean tree — committed work? Unborn HEAD means zero commits AND a clean
    #    tree: a genuine no-op (still fabrication), not an unverifiable state.
    head = _git(["rev-parse", "--verify", "HEAD"])
    if head is None:
        return None
    if head.returncode != 0:
        return False

    # 3. Tree-diff against the merge-base with base_ref. TREE comparison, not
    #    commit count: an empty commit or revert-to-base yields an identical
    #    tree and therefore counts as NO work.
    merge_base = _git(["merge-base", base_ref, "HEAD"])
    if merge_base is None or merge_base.returncode != 0 or not merge_base.stdout.strip():
        logger.warning(
            "kimi_spawn: fabrication-invariant could not resolve merge-base of %s and HEAD for %s "
            "(skipping check): %s",
            base_ref, wt, ((merge_base.stderr or "")[:200] if merge_base is not None else "git error"),
        )
        return None
    diff = _git(["diff", "--quiet", merge_base.stdout.strip(), "HEAD"])
    if diff is None:
        return None
    if diff.returncode == 0:
        return False  # trees identical → no real work
    if diff.returncode == 1:
        return True  # tree differs → real committed work
    logger.warning(
        "kimi_spawn: fabrication-invariant git diff exited %d for %s (skipping check): %s",
        diff.returncode, wt, (diff.stderr or "")[:200],
    )
    return None


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


# OI-1044: the worker-heartbeat silence detector (#1387, OI-944/OI-1007) is wired
# into the tmux-spawn lane (FileProgressHeartbeat on the pipe-pane log) and the
# subprocess lane (EventStreamHeartbeat on the EventStore stream) but not the
# provider lane — kimi, the fleet-default build-worker, had no external liveness
# signal at all. `~/.kimi/logs/kimi.log` is a single file shared by every
# concurrent kimi dispatch, so it cannot be the signal: two dispatches running at
# once would mask each other's death (one keeps writing while the other is gone).
# The tee below gives each dispatch its own file (keyed by dispatch_id, the one
# identifier guaranteed unique across concurrent dispatches — terminal_id is not,
# since provider-lane dispatches are not necessarily terminal-pinned).
_DISPATCH_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# How often the monitor thread polls the tee file for growth. Matches the
# subprocess lane's _silence_heartbeat_loop default (delivery_runtime.py).
_HEARTBEAT_POLL_INTERVAL_SECONDS = 30.0


def _resolve_heartbeat_log_path(dispatch_id: str) -> Optional[Path]:
    """Per-dispatch raw-stdout tee path for the FileProgressHeartbeat (OI-1044).

    Same directory convention as the tmux lane's pipe-pane capture
    (``logs/conversations/{dispatch_id}.log`` — see
    ``tmux_interactive_dispatch.py::_start_pipe_pane``) so a dispatch's raw
    activity log lives in one predictable place regardless of which lane ran
    it. Returns None (heartbeat disabled, fail-open) when dispatch_id fails
    the same path-safety check the tmux lane applies, or when VNX_DATA_DIR
    cannot be resolved — a missing heartbeat must never break the dispatch
    itself, only leave it unmonitored.
    """
    if not dispatch_id or not _DISPATCH_ID_SAFE_RE.match(dispatch_id):
        logger.warning(
            "kimi_spawn: heartbeat disabled — unsafe dispatch_id %r", dispatch_id,
        )
        return None
    try:
        from vnx_paths import resolve_paths  # noqa: PLC0415
        data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
    except Exception as exc:  # noqa: BLE001 — heartbeat setup must never break the dispatch
        logger.warning(
            "kimi_spawn: heartbeat disabled — VNX_DATA_DIR resolution failed: %s", exc,
        )
        return None
    log_dir = data_dir / "logs" / "conversations"
    raw_log = (log_dir / f"{dispatch_id}.log").resolve()
    try:
        raw_log.relative_to(log_dir.resolve())
    except ValueError:
        logger.warning(
            "kimi_spawn: heartbeat disabled — log path %s escaped %s", raw_log, log_dir,
        )
        return None
    return raw_log


def _write_heartbeat_report(dispatch_id: str, report_text: str) -> None:
    """Atomically write the heartbeat failure report to unified_reports.

    The stuck worker never gets to write its own report, so the heartbeat
    writes a substitute directly — same responsibility the tmux lane
    (``tmux_interactive_dispatch.py::_wait_for_receipt``) and the subprocess
    lane (``delivery_runtime.py::_silence_heartbeat_loop``) already carry.
    ``emit_unified_report`` is idempotent (it never overwrites an existing
    contract-valid report), so this write is safe even if it races a governance
    emit downstream — write-tmp-then-replace so a concurrent reader never sees
    a partial file.
    """
    from vnx_paths import resolve_paths  # noqa: PLC0415

    data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{dispatch_id}.md"
    tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    tmp_path.write_text(report_text, encoding="utf-8")
    os.replace(tmp_path, report_path)


def _heartbeat_monitor_loop(
    proc: subprocess.Popen,
    heartbeat_log_path: Path,
    dispatch_id: str,
    terminal_id: str,
    model: str,
    stop_event: threading.Event,
    killed_event: threading.Event,
    poll_interval: float = _HEARTBEAT_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll the per-dispatch tee file for silence; kill + report if stuck.

    Same module (``worker_heartbeat.FileProgressHeartbeat``), same threshold
    (``VNX_WORKER_HEARTBEAT_SILENCE_SECONDS``, default 600s), same failure-report
    shape (``build_heartbeat_failure_report``) as the tmux-spawn and subprocess
    lanes, so the audit trail is uniform regardless of which lane ran the
    dispatch (OI-944, OI-1007, OI-1044).

    The 600s default tolerates kimi's measured cadence: a healthy worker can go
    22 minutes between *file writes* while doing nothing but Read/Grep, but its
    stdout stream (which this tee mirrors) carries a tool_call/tool_result pair
    for every one of those reads — measured gaps between kimi's own LLM steps
    topped out at 209s, well under the threshold. FileProgressHeartbeat watches
    that raw stdout tee, not worktree file mutations, precisely so read-only
    activity keeps counting as progress.

    Liveness is verified on the real OS process throughout: the kill path
    (``_kill_process``) uses ``os.getpgid``/``os.killpg``/``proc.wait()``, never
    a pidfile (OI-1044 finding: a stale pidfile reads as "running" forever;
    only the kernel's own view of the pid is trustworthy).
    """
    from worker_heartbeat import (  # noqa: PLC0415
        FileProgressHeartbeat,
        build_heartbeat_failure_report,
    )

    hb = FileProgressHeartbeat(heartbeat_log_path, dispatch_id)
    while not stop_event.wait(timeout=poll_interval):
        verdict = hb.check()
        if not verdict.is_silent:
            continue
        logger.warning(
            "kimi_spawn: heartbeat silence detected for %s (%.0fs silent, "
            "threshold=%.0fs) — killing worker",
            dispatch_id, verdict.silence_seconds, verdict.threshold_seconds,
        )
        # OI-1082: flag the kill DECISION before executing it. _kill_process
        # blocks worst-case ~10s (SIGTERM wait + SIGKILL wait) and the report
        # write adds more, while spawn_kimi joins this thread with a 5s cap —
        # setting the event last meant the reader could see False after a real
        # kill and downgrade the receipt's failure_reason to a generic
        # "kimi exited with code -9". The event marks the decision, not the
        # completion; the kill below is unconditional once we reach here.
        killed_event.set()
        _kill_process(proc)
        try:
            report = build_heartbeat_failure_report(
                dispatch_id=dispatch_id,
                verdict=verdict,
                model=model,
                provider="kimi",
                terminal_id=terminal_id,
            )
            _write_heartbeat_report(dispatch_id, report)
            logger.info(
                "kimi_spawn: heartbeat failure report written for %s", dispatch_id,
            )
        except Exception as exc:  # noqa: BLE001 — report write must never raise past this thread
            logger.warning(
                "kimi_spawn: heartbeat failure report write failed for %s: %s",
                dispatch_id, exc,
            )
        return


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
    heartbeat_log_path: Optional[Path] = None,
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
        raw_tee_path=heartbeat_log_path,
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
    task_class: Optional[str] = None,
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

    OI-1087: ``task_class`` exempts POSITIVELY-KNOWN read-only classes (the
    phantom_guard REVIEW_TASK_CLASSES SSOT: review/analysis/research_structured/
    ...) from that invariant — for a read-only dispatch an unchanged worktree IS
    the intended outcome (the report is the deliverable; the dispatch forbids
    commits). Fail-safe direction: an empty or unknown ``task_class`` keeps the
    guard armed exactly as before; only a recognized read-only class suppresses
    it.
    """
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    rc = proc.returncode if proc.returncode is not None else 1

    empty_extraction = not (completion_text or "").strip()

    _normalized_class = (task_class or "").strip().lower()
    read_only_class = bool(_normalized_class) and _normalized_class in REVIEW_TASK_CLASSES

    worktree_unchanged = False
    if saw_tool_calls and worktree is not None and not read_only_class:
        worktree_unchanged = _worktree_has_changes(worktree) is False
    elif saw_tool_calls and worktree is not None and read_only_class:
        logger.info(
            "kimi_spawn: fabrication guard not applied for read-only task_class=%r "
            "— an unchanged worktree is the expected outcome for this class",
            task_class,
        )

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
    chunk_timeout: float = 1200.0,
    total_deadline: float = 900.0,
    event_store: Optional[Any] = None,
    task_class: Optional[str] = None,
    **kwargs: Any,
) -> KimiSpawnResult:
    """Spawn ``kimi --print --output-format stream-json --yolo -p <prompt>``.

    Returns KimiSpawnResult on completion (success OR controlled failure).
    Returns KimiSpawnResult(returncode=127) when the kimi binary is absent.
    Caller is responsible for lease/manifest/receipt/event-archive/retry.

    The per-chunk stall default is 1200s (overridable via VNX_KIMI_STALL_THRESHOLD):
    Kimi is a reasoning model whose 1.46.0 content-block output is end-loaded, so
    the first token can arrive only after a long reasoning gap (a 300s default
    spuriously killed adversarial-review dispatches mid-think). A FAILURE is
    returned (never a silent empty
    success) when the CLI emits output but no answer text is extracted.

    Token accounting: stream-json never carries it (measured on 1.46.0). On a
    clean run the session is exported post-run and the StatusUpdate token_usage
    read from its ``wire.jsonl`` (``_harvest_session_token_usage``); result.token_usage
    is filled when that succeeds, stays None otherwise — fail-open, no estimates.

    event_writer signature: ``(terminal_id, event_dict, dispatch_id=...)`` called
    per normalized event. Failures are counted in result.event_writer_failures.

    Auth: OAuth via ``kimi login`` (operator-managed). No API key required.

    DUPLICATE-WRITE CONTRACT: pass either ``event_writer`` OR ``event_store``, not
    both. ``event_store`` is forwarded to drain_stream (writes via drainer);
    ``event_writer`` is called per-event in _consume_kimi_stream. Passing both
    causes every event to be written twice.

    ``task_class`` (OI-1087) is an EXPLICIT keyword — deliberately not silently
    absorbed by ``**kwargs``: the four review-dispatch false-failures of
    2026-08-07 happened precisely because this signal never left the adapter.
    A visible, typed parameter is greppable, typo-safe (a misspelled kwarg in
    **kwargs vanishes silently), and shows up in the signature for every future
    caller. Forwarded to _finalize_kimi_result, where a positively-known
    read-only class exempts the dispatch from the worktree-changed fabrication
    guard. Unknown/empty keeps the guard armed.
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
    # OI-903: scale the stall timeout with the total deadline so a long spec
    # deadline is the binding constraint, not a fixed 1200s silence. Skipped when
    # VNX_KIMI_STALL_THRESHOLD is set explicitly — env overrides retain precedence.
    if "VNX_KIMI_STALL_THRESHOLD" not in os.environ:
        chunk_timeout = coerce_chunk_stall(chunk_timeout, total_deadline)

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

    # OI-1044: external worker heartbeat — the provider lane had no liveness
    # signal a supervisor could observe from outside the process. The monitor
    # thread watches the per-dispatch stdout tee (written by drain_stream below)
    # and kills+reports a stuck worker independently of the internal
    # chunk_timeout/total_deadline stall guard (which only fires on a fixed,
    # deadline-scaled window and never writes the standard heartbeat report).
    heartbeat_log_path = _resolve_heartbeat_log_path(dispatch_id)
    _hb_stop = threading.Event()
    _hb_killed = threading.Event()
    _hb_thread: Optional[threading.Thread] = None
    if heartbeat_log_path is not None:
        _hb_thread = threading.Thread(
            target=_heartbeat_monitor_loop,
            args=(
                proc, heartbeat_log_path, dispatch_id, terminal_id,
                model or "unknown", _hb_stop, _hb_killed,
            ),
            daemon=True,
            name=f"kimi-heartbeat-{dispatch_id}",
        )
        _hb_thread.start()

    host = _KimiNormalizerHost(terminal_id=terminal_id, dispatch_id=dispatch_id)
    try:
        (
            completion_text, events_written, token_usage, timed_out, stopped_early,
            _event_writer_failures, errors_captured, saw_stream_output, raw_samples,
            saw_tool_calls,
        ) = _consume_kimi_stream(
            proc=proc, host=host, on_event=on_event,
            health_monitor=health_monitor, event_writer=event_writer,
            terminal_id=terminal_id, dispatch_id=dispatch_id,
            event_store=event_store, chunk_timeout=chunk_timeout,
            total_deadline=total_deadline, heartbeat_log_path=heartbeat_log_path,
        )
    finally:
        _hb_stop.set()
        if _hb_thread is not None:
            _hb_thread.join(timeout=5.0)

    result = _finalize_kimi_result(
        proc=proc, completion_text=completion_text,
        events_written=events_written, token_usage=token_usage,
        timed_out=timed_out, stopped_early=stopped_early,
        event_writer_failures=_event_writer_failures,
        errors_captured=errors_captured,
        saw_stream_output=saw_stream_output,
        raw_samples=raw_samples,
        saw_tool_calls=saw_tool_calls,
        worktree=cwd,
        task_class=task_class,
    )
    if _hb_killed.is_set():
        # The heartbeat already wrote the terminal failure report — replace the
        # generic "kimi exited with code N" message with the real reason so the
        # receipt's failure_reason is diagnosable without cross-referencing logs.
        result.error = (
            f"worker heartbeat: killed due to silence exceeding threshold "
            f"(dispatch={dispatch_id}); see unified_reports/{dispatch_id}.md"
        )
        if result.returncode == 0:
            result.returncode = 1

    # Post-run token harvest. stream-json carries no token accounting (measured
    # on kimi-cli 1.46.0), so on a clean run the session is exported and the
    # StatusUpdate token_usage the CLI recorded is read from its wire.jsonl.
    # Fail-open: no session id, export failure, or missing StatusUpdate leaves
    # token_usage None — the receipt stays honestly unavailable, never broken.
    if result.error is None and result.returncode == 0 and result.token_usage is None:
        try:
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        except Exception:  # noqa: BLE001 — harvest must never break the dispatch
            stderr_text = ""
        session_id = _extract_session_id(stderr_text)
        if session_id:
            harvested = _harvest_session_token_usage(session_id, env, cwd_str)
            if harvested:
                result.token_usage = harvested
            # OI-812: one-shot dispatches are never resumed, so the session dir
            # (wire.jsonl + context.jsonl + state.json) is dead weight after the
            # harvest (which already exported the token data). Reap it. Runs
            # regardless of whether the harvest yielded tokens — a failed export
            # still leaves a session dir behind. Best-effort: a reap failure
            # must never break an otherwise-clean dispatch.
            _reap_kimi_session(session_id, env)
    return result
