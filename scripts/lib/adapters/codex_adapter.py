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
from provider_spawns.codex_spawn import (  # noqa: E402
    normalize_codex_event,
    spawn_codex,
    _extract_token_count_payload as _spawn_extract_token_count_payload,
    _normalize_token_count as _spawn_normalize_token_count,
)
from vertex_ai_runner import collect_file_contents

logger = logging.getLogger(__name__)

# Model: empty string = use codex CLI config.toml default (currently gpt-5.5).
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

        Delegates spawn+stream to spawn_codex() (Wave 4.6 PR-4.6.3).
        Behavior is byte-identical to the pre-refactor inline path.
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

        role = context.get("role")
        dispatch_meta = context.get("dispatch_metadata", {})
        prompt = self._build_prompt(instruction, changed_files, role=role, dispatch_metadata=dispatch_meta)

        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        t0 = time.monotonic()

        collected_events: list[dict] = []

        def _collect_event(tid: str, event_dict: dict, dispatch_id: str = dispatch_id) -> None:
            collected_events.append(event_dict)

        spawn_result = spawn_codex(
            prompt=prompt,
            model=model,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            event_writer=_collect_event,
            event_store=event_store,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        )

        duration = time.monotonic() - t0

        if spawn_result.token_usage:
            self._write_token_cache(spawn_result.token_usage)

        status = "done" if spawn_result.returncode == 0 else "failed"

        return AdapterResult(
            status=status,
            output=spawn_result.completion_text,
            events=collected_events,
            event_count=len(collected_events),
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
        role = context.get("role")
        dispatch_meta = context.get("dispatch_metadata", {})

        prompt = self._build_prompt(instruction, changed_files, role=role, dispatch_metadata=dispatch_meta)
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
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except BrokenPipeError:
                yield {"type": "error", "data": {"reason": "stdin write failed (BrokenPipeError)"}}
                return

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
        """Delegate to normalize_codex_event (single implementation in codex_spawn)."""
        return normalize_codex_event(raw, self._current_terminal_id, self._current_dispatch_id)

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
        """Shim: delegates to codex_spawn._extract_token_count_payload."""
        return _spawn_extract_token_count_payload(event)

    @staticmethod
    def _normalize_token_count(payload: dict) -> Optional[dict]:
        """Shim: delegates to codex_spawn._normalize_token_count."""
        return _spawn_normalize_token_count(payload)

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
        except OSError as e:
            logger.debug("Failed to write token cache for %s: %s", self._terminal_id, e)

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
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("Failed to read token cache for %s: %s", terminal_id, e)
        return None

    def _build_prompt(
        self,
        instruction: str,
        changed_files: list[str],
        role: Optional[str] = None,
        dispatch_metadata: Optional[dict] = None,
    ) -> str:
        """Build prompt from instruction + inline file contents.

        When role or dispatch_metadata is provided, routes through PromptAssembler
        (L1 base rules + L2 role context + L3 instruction). Falls back to raw
        instruction+files when neither is given (backward compat).

        When dispatch_id is present in dispatch_metadata, injects per-provider
        intelligence context (antipatterns, success patterns). Skipped silently
        when dispatch_id is empty — no audit rows written.
        """
        payload = {"changed_files": changed_files}
        file_contents = collect_file_contents(payload, subprocess_run=subprocess.run)
        full_instruction = f"{instruction}\n\n{file_contents}" if file_contents else instruction

        dispatch_id = (dispatch_metadata or {}).get("dispatch_id", "")
        try:
            from intelligence_selector import build_intelligence_context
            intel_ctx = build_intelligence_context(
                dispatch_id=dispatch_id or "",
                role=role or "",
                pr_id=(dispatch_metadata or {}).get("pr_id"),
                dispatch_paths=(dispatch_metadata or {}).get("dispatch_paths"),
            )
            intel_markdown = intel_ctx.serialize_for("codex") if intel_ctx else ""
        except Exception:
            intel_markdown = ""

        if intel_markdown:
            full_instruction = f"{full_instruction}\n\n{intel_markdown}"

        if role or dispatch_metadata:
            from prompt_assembler import PromptAssembler, format_for_provider
            assembled = PromptAssembler().assemble(
                dispatch_metadata={"role": role, **(dispatch_metadata or {})},
                instruction=full_instruction,
            )
            return format_for_provider(assembled, "codex")["pipe_input"]

        return full_instruction

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
