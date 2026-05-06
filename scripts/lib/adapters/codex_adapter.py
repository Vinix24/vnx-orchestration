#!/usr/bin/env python3
"""adapters/codex_adapter.py — CodexAdapter with live streaming via StreamingDrainerMixin.

Executes code analysis via the `codex` CLI with inline file contents.
Events are streamed live (Tier-1 observability) via StreamingDrainerMixin.

IMPORTANT: Prompts include inline file contents — no GitHub PR references
are used. This avoids the GitHub app dependency identified in F51-PR1.

BILLING SAFETY: No Anthropic SDK. CLI-only subprocess calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _streaming_drainer import StreamingDrainerMixin
from canonical_event import CanonicalEvent
from provider_adapter import AdapterResult, Capability, ProviderAdapter
from vertex_ai_runner import collect_file_contents

logger = logging.getLogger(__name__)

# Model: empty string = use codex CLI config.toml default (currently gpt-5.3-codex).
# 2026-04-19: gpt-5.2-codex deprecated via Codex CLI model-migration mapping;
# ChatGPT-account auth rejects older explicit model flags.
_DEFAULT_MODEL = ""
_DEFAULT_TIMEOUT = 300
_DEFAULT_STALL_THRESHOLD = 60

# Observability tier: Codex emits live per-event NDJSON via StreamingDrainerMixin.
OBSERVABILITY_TIER = 1


class CodexAdapter(StreamingDrainerMixin, ProviderAdapter):
    """Provider adapter for the Codex CLI (review and decision only).

    Streams the prompt via stdin to `codex exec --json`, normalizes the NDJSON
    output to CanonicalEvent objects via StreamingDrainerMixin (Tier-1), and
    returns an AdapterResult. Inline file contents replace PR references.
    """

    provider_name = "codex"
    provider_observability_tier = OBSERVABILITY_TIER

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id
        # Set before drain_stream so _normalize can construct CanonicalEvents.
        self._current_terminal_id: str = terminal_id
        self._current_dispatch_id: str = "unknown"

    # ------------------------------------------------------------------
    # ProviderAdapter interface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "codex"

    def capabilities(self) -> set[Capability]:
        return {Capability.REVIEW, Capability.DECISION}

    def is_available(self) -> bool:
        """Return True when the `codex` binary is found on PATH."""
        return shutil.which("codex") is not None

    def execute(self, instruction: str, context: dict) -> AdapterResult:
        """Run a Codex review with inline file contents and return findings.

        Spawns `codex exec --json`, drains stdout via StreamingDrainerMixin for
        live Tier-1 events, collects text findings, and returns AdapterResult.
        """
        model = (
            os.environ.get("VNX_CODEX_HEADLESS_MODEL")
            or os.environ.get("VNX_CODEX_MODEL")
            or _DEFAULT_MODEL
        )
        chunk_timeout = float(
            os.environ.get("VNX_CODEX_STALL_THRESHOLD",
                           context.get("chunk_timeout", _DEFAULT_STALL_THRESHOLD))
        )
        total_deadline = float(
            os.environ.get("VNX_CODEX_TIMEOUT",
                           context.get("total_deadline", _DEFAULT_TIMEOUT))
        )
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        event_store = context.get("event_store")
        changed_files = context.get("changed_files", [])

        prompt = self._build_prompt(instruction, changed_files)
        cmd = self._build_cmd(model)

        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return AdapterResult(
                status="failed",
                output=str(exc),
                events=[],
                event_count=0,
                duration_seconds=0.0,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="codex",
                model=model,
            )

        if proc.stdin:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()

        events: list[dict] = []
        findings_parts: list[str] = []
        last_token_usage: Optional[dict] = None

        for canonical_event in self.drain_stream(
            proc,
            terminal_id,
            dispatch_id,
            event_store,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            events.append(canonical_event.to_dict())
            if canonical_event.event_type == "text":
                text = canonical_event.data.get("text", "")
                if text:
                    findings_parts.append(text)
                tc = canonical_event.data.get("token_count")
                if tc:
                    last_token_usage = tc
            elif canonical_event.event_type == "complete":
                tc = canonical_event.data.get("token_count")
                if tc:
                    last_token_usage = tc
                text = canonical_event.data.get("text", "")
                if text:
                    findings_parts.append(text)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        duration = time.monotonic() - t0

        if last_token_usage:
            self._write_token_cache(last_token_usage)

        findings = "\n\n".join(findings_parts) if findings_parts else ""
        rc = proc.returncode
        status = "done" if rc == 0 else "failed"

        return AdapterResult(
            status=status,
            output=findings,
            events=events,
            event_count=len(events),
            duration_seconds=duration,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="codex",
            model=model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """Stream Codex events live; yields CanonicalEvent dicts during execution."""
        model = (
            os.environ.get("VNX_CODEX_HEADLESS_MODEL")
            or os.environ.get("VNX_CODEX_MODEL")
            or _DEFAULT_MODEL
        )
        chunk_timeout = float(
            os.environ.get("VNX_CODEX_STALL_THRESHOLD",
                           context.get("chunk_timeout", _DEFAULT_STALL_THRESHOLD))
        )
        total_deadline = float(
            os.environ.get("VNX_CODEX_TIMEOUT",
                           context.get("total_deadline", _DEFAULT_TIMEOUT))
        )
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        event_store = context.get("event_store")
        changed_files = context.get("changed_files", [])

        prompt = self._build_prompt(instruction, changed_files)
        cmd = self._build_cmd(model)

        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            yield {"type": "error", "data": {"reason": str(exc)}}
            return

        if proc.stdin:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()

        for canonical_event in self.drain_stream(
            proc,
            terminal_id,
            dispatch_id,
            event_store,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            yield canonical_event.to_dict()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # ------------------------------------------------------------------
    # StreamingDrainerMixin: event normalizer
    # ------------------------------------------------------------------

    def _normalize(self, raw: dict) -> CanonicalEvent:
        """Map a raw Codex NDJSON event to a CanonicalEvent (Tier-1).

        Handles three Codex event shapes:
        1. New-style top-level: {"type": "thread.started"|"turn.completed"|"item.*", ...}
        2. Wrapped: {"event_msg": {"payload": {"type": "session_start"|"agent_message"|..., ...}}}
        3. Legacy: {"type": "agent_message"|"result"|"message", "content": "..."}

        Mapping:
          thread.started / session_start             → init
          agent_message (direct or item.completed)   → text
          item.started/updated [command_execution]   → tool_use
          item.completed [command_execution]          → tool_result
          error                                       → error
          turn.completed / result / message           → complete
          token_count (intermediate telemetry)        → text (token_count in data)
          unknown                                     → error
        """
        terminal_id = self._current_terminal_id
        dispatch_id = self._current_dispatch_id

        def make(event_type: str, data: dict) -> CanonicalEvent:
            return CanonicalEvent(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="codex",
                event_type=event_type,
                data=data,
                observability_tier=1,
            )

        # Resolve effective payload: unwrap event_msg.payload when present.
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

        # New-style item.* / thread.* / turn.* events use the top-level type.
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
        item_type = item.get("type", "")

        # ── thread.started / session_start → init ──────────────────────
        if etype in ("thread.started", "session_start"):
            return make("init", {"raw_type": etype})

        # ── agent_message (direct) → text ──────────────────────────────
        if etype == "agent_message":
            content = payload.get("text", payload.get("content", payload.get("message", "")))
            return make("text", {"text": str(content)})

        # ── item.completed [agent_message] → text ──────────────────────
        if etype == "item.completed" and item_type == "agent_message":
            content = item.get("content", "")
            if isinstance(content, list):
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                ]
                content = "\n".join(t for t in texts if t)
            return make("text", {"text": str(content)})

        # ── item.started / item.updated [command_execution] → tool_use ─
        if etype in ("item.started", "item.updated") and item_type == "command_execution":
            cmd_str = item.get("command", item.get("cmd", item.get("args", "")))
            if isinstance(cmd_str, list):
                cmd_str = " ".join(str(a) for a in cmd_str)
            return make("tool_use", {"command": str(cmd_str), "raw_type": etype})

        # ── item.completed [command_execution] → tool_result ───────────
        if etype == "item.completed" and item_type == "command_execution":
            output = item.get("output", item.get("result", ""))
            exit_code = item.get("exit_code", 0)
            return make("tool_result", {"output": str(output), "exit_code": exit_code})

        # ── error → error ───────────────────────────────────────────────
        if etype == "error":
            msg = payload.get("message", payload.get("error", payload.get("text", "")))
            return make("error", {"message": str(msg) if msg else str(payload)[:200]})

        # ── turn.completed → complete (with token_count from event) ─────
        if etype == "turn.completed":
            tc_payload = CodexAdapter._extract_token_count_payload(raw)
            token_count = CodexAdapter._normalize_token_count(tc_payload) if tc_payload else None
            data: dict = {}
            if token_count:
                data["token_count"] = token_count
            return make("complete", data)

        # ── result / message → complete ─────────────────────────────────
        if etype in ("result", "message"):
            content = payload.get("content", payload.get("text", payload.get("output", "")))
            tc_payload = CodexAdapter._extract_token_count_payload(raw)
            token_count = CodexAdapter._normalize_token_count(tc_payload) if tc_payload else None
            # Also check OpenAI-style usage block on result events
            if token_count is None:
                usage = raw.get("usage") or payload.get("usage")
                if isinstance(usage, dict):
                    token_count = CodexAdapter._normalize_token_count({
                        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens", 0),
                    })
            data = {"text": str(content)} if content else {}
            if token_count:
                data["token_count"] = token_count
            return make("complete", data)

        # ── intermediate token_count → text (with token data) ───────────
        tc_payload = CodexAdapter._extract_token_count_payload(raw)
        if tc_payload is not None:
            token_count = CodexAdapter._normalize_token_count(tc_payload)
            if token_count:
                return make("text", {"text": "", "token_count": token_count})

        # ── unknown event type → error ───────────────────────────────────
        return make("error", {
            "reason": f"unrecognized codex event type: {etype!r}",
            "raw_type": etype,
            "raw": str(raw)[:300],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cmd(model: str) -> list[str]:
        cmd = ["codex", "exec", "--json"]
        if model:
            cmd += ["-c", f'model="{model}"']
        return cmd

    # ------------------------------------------------------------------
    # Token usage
    # ------------------------------------------------------------------

    _TOKEN_TEXT_RE = re.compile(
        r"tokens?:\s*(\d+)\s+input\s*/\s*(\d+)\s+output",
        re.IGNORECASE,
    )

    @staticmethod
    def _extract_token_count_payload(event: dict) -> Optional[dict]:
        """Locate a `token_count` payload nested inside a Codex NDJSON event.

        Codex `exec --json` emits records where the actual event body lives under
        one of several wrapper keys, e.g.:

            {"event_msg": {"payload": {"type": "token_count",
                                        "input_tokens": N,
                                        "cached_input_tokens": M,
                                        "output_tokens": K, ...}}}

            {"msg": {"type": "token_count", "input_tokens": N, ...}}

            {"item": {"type": "token_count", ...}}

        Returns the inner payload dict when type == "token_count", else None.
        """
        if not isinstance(event, dict):
            return None
        # event_msg.payload (current `codex exec` shape)
        em = event.get("event_msg")
        if isinstance(em, dict):
            payload = em.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                return payload
            if em.get("type") == "token_count":
                return em
        # msg-wrapped variant
        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "token_count":
            return msg
        # item-wrapped variant
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "token_count":
            return item
        # top-level token_count
        if event.get("type") == "token_count":
            return event
        return None

    @staticmethod
    def _normalize_token_count(payload: dict) -> Optional[dict]:
        """Normalize a Codex token_count payload to the canonical token_usage dict.

        Codex variants we accept:
        - input_tokens / output_tokens (preferred)
        - prompt_tokens / completion_tokens (OpenAI-compat key names)
        Cache breakdown (best-effort, both names supported):
        - cached_input_tokens / cache_read_tokens
        - cache_creation_input_tokens / cache_creation_tokens
        """
        if not isinstance(payload, dict):
            return None
        input_t = payload.get("input_tokens")
        if input_t is None:
            input_t = payload.get("prompt_tokens", 0)
        output_t = payload.get("output_tokens")
        if output_t is None:
            output_t = payload.get("completion_tokens", 0)
        if not isinstance(input_t, int) or not isinstance(output_t, int):
            return None
        if input_t == 0 and output_t == 0:
            return None
        cache_read = payload.get("cached_input_tokens")
        if cache_read is None:
            cache_read = payload.get("cache_read_tokens", 0)
        cache_creation = payload.get("cache_creation_input_tokens")
        if cache_creation is None:
            cache_creation = payload.get("cache_creation_tokens", 0)
        return {
            "input_tokens": int(input_t),
            "output_tokens": int(output_t),
            "cache_creation_tokens": int(cache_creation) if isinstance(cache_creation, int) else 0,
            "cache_read_tokens": int(cache_read) if isinstance(cache_read, int) else 0,
        }

    @staticmethod
    def _parse_token_usage_from_output(raw: str) -> Optional[dict]:
        """Parse token counts from Codex CLI output.

        Handles the formats emitted by `codex exec --json`:
        1. Wrapped token_count event (current shape):
           {"event_msg":{"payload":{"type":"token_count","input_tokens":N,...}}}
           (also matched: top-level msg/item wrappers and direct type=token_count)
        2. Explicit token_usage event:  {"type":"token_usage","input_tokens":N,"output_tokens":M}
        3. OpenAI-compat usage block:   {"usage":{"prompt_tokens":N,"completion_tokens":M}}
           (also input_tokens/output_tokens variants)
        4. Human-readable text line:    "Tokens: 1200 input / 350 output [/ 1550 total]"

        Codex emits multiple token_count events during a run (turn-by-turn).
        We retain the LAST parseable token_count payload because Codex reports
        a running total that updates as the session progresses; the final event
        therefore reflects the complete usage for the run.

        Returns None if no parseable token info is found.
        """
        last_token_count: Optional[dict] = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Try JSON event
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None

            if isinstance(event, dict):
                # Format 1: token_count wrapped under event_msg.payload / msg / item / top-level.
                tc_payload = CodexAdapter._extract_token_count_payload(event)
                if tc_payload is not None:
                    normalized = CodexAdapter._normalize_token_count(tc_payload)
                    if normalized is not None:
                        last_token_count = normalized
                        continue
                # Format 2: explicit token_usage event
                if event.get("type") == "token_usage":
                    input_t = event.get("input_tokens", 0)
                    output_t = event.get("output_tokens", 0)
                    if isinstance(input_t, int) and isinstance(output_t, int):
                        return {
                            "input_tokens": input_t,
                            "output_tokens": output_t,
                            "cache_creation_tokens": 0,
                            "cache_read_tokens": 0,
                        }
                # Format 3: OpenAI-compatible usage block
                usage = event.get("usage")
                if isinstance(usage, dict):
                    input_t = (
                        usage.get("input_tokens")
                        or usage.get("prompt_tokens")
                        or 0
                    )
                    output_t = (
                        usage.get("output_tokens")
                        or usage.get("completion_tokens")
                        or 0
                    )
                    if isinstance(input_t, int) and isinstance(output_t, int) and (input_t or output_t):
                        return {
                            "input_tokens": input_t,
                            "output_tokens": output_t,
                            "cache_creation_tokens": 0,
                            "cache_read_tokens": 0,
                        }
            # Format 4: text line "Tokens: N input / M output"
            m = CodexAdapter._TOKEN_TEXT_RE.search(line)
            if m:
                return {
                    "input_tokens": int(m.group(1)),
                    "output_tokens": int(m.group(2)),
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }
        return last_token_count

    def _write_token_cache(self, usage: dict, state_dir: Optional[Path] = None) -> None:
        """Persist token usage to per-terminal state file (best-effort)."""
        try:
            sd = state_dir or Path(os.environ.get("VNX_STATE_DIR", ""))
            if not sd or str(sd) == ".":
                return
            cache_dir = sd / "token_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"{self._terminal_id}_usage.json").write_text(
                json.dumps(usage), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def get_token_usage(terminal_id: str, state_dir: Optional[Path] = None) -> Optional[dict]:
        """Read last captured token usage for a terminal from the state cache.

        Returns None if no cache file exists or the file cannot be parsed.
        """
        try:
            sd = state_dir or Path(os.environ.get("VNX_STATE_DIR", ""))
            if not sd or str(sd) == ".":
                return None
            cache_file = Path(sd) / "token_cache" / f"{terminal_id}_usage.json"
            if not cache_file.is_file():
                return None
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "input_tokens" in data and "output_tokens" in data:
                return data
        except Exception:
            pass
        return None

    def _build_prompt(self, instruction: str, changed_files: list[str]) -> str:
        """Combine instruction with inline file contents (no PR references)."""
        payload = {"changed_files": changed_files}
        file_contents = collect_file_contents(payload, subprocess_run=subprocess.run)
        if file_contents:
            return f"{instruction}\n\n{file_contents}"
        return instruction

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        """Send SIGTERM then SIGKILL to process group."""
        try:
            import signal as _signal
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, _signal.SIGTERM)
            time.sleep(0.2)
            os.killpg(pgid, _signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
