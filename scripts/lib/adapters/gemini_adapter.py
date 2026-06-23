#!/usr/bin/env python3
"""adapters/gemini_adapter.py — GeminiAdapter for review and digest tasks.

Executes code review via the `gemini` CLI (not the Vertex AI REST path).
Review-only: no CODE capability, no file writes, no git commits.

Streaming mode: set VNX_GEMINI_STREAM=1 to switch to --output-format stream-json
and drain events via StreamingDrainerMixin (Tier-1). Default (unset/0) keeps the
legacy --output-format json path (Tier-3, single synthetic result event).

BILLING SAFETY: No Anthropic SDK. CLI-only subprocess calls.
"""

from __future__ import annotations

import json
import logging
import os
import select
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
from provider_spawns.gemini_spawn import (  # noqa: E402
    normalize_gemini_event,
    spawn_gemini,
    _extract_gemini_usage_metadata as _spawn_extract_usage_metadata,
    _extract_gemini_token_count as _spawn_extract_token_count,
)
from vertex_ai_runner import collect_file_contents

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-pro"
_DEFAULT_TIMEOUT = 300
_DEFAULT_STALL_THRESHOLD = 60

# Observability tiers
_TIER_STREAMING = 1   # VNX_GEMINI_STREAM=1: live per-event
_TIER_LEGACY = 3      # VNX_GEMINI_STREAM=0: final-only synthetic result

# Public adapter-level tier constant: effective tier under streaming config.
# When VNX_GEMINI_STREAM=0 (legacy), effective tier is _TIER_LEGACY (3).
OBSERVABILITY_TIER = _TIER_STREAMING


def _gemini_stream_enabled() -> bool:
    """Return True when VNX_GEMINI_STREAM=1 is set in the environment."""
    return os.environ.get("VNX_GEMINI_STREAM", "0").strip() == "1"


class GeminiAdapter(StreamingDrainerMixin, ProviderAdapter):
    """Provider adapter for the Gemini CLI (review and digest only).

    When VNX_GEMINI_STREAM=1: spawns `gemini --output-format stream-json`,
    drains NDJSON events live via StreamingDrainerMixin (Tier-1 observability).

    When VNX_GEMINI_STREAM=0 (default): spawns `gemini --output-format json`,
    collects buffered response as a single synthetic result event (Tier-3).
    """

    provider_name = "gemini"
    provider_observability_tier = OBSERVABILITY_TIER

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id
        self._current_terminal_id: str = terminal_id
        self._current_dispatch_id: str = "unknown"

    # ------------------------------------------------------------------
    # ProviderAdapter interface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "gemini"

    def capabilities(self) -> set[Capability]:
        return {Capability.REVIEW, Capability.DIGEST}

    def is_available(self) -> bool:
        """Return True when the `gemini` binary is found on PATH."""
        return shutil.which("gemini") is not None

    def execute(self, instruction: str, context: dict) -> AdapterResult:
        """Run a Gemini review. Delegates spawn+stream to spawn_gemini() (Wave 4.6 PR-4.6.4).

        Behavior is byte-identical to the pre-refactor inline path.
        """
        model = os.environ.get("VNX_GEMINI_MODEL", _DEFAULT_MODEL)
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        changed_files = context.get("changed_files", [])
        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        role = context.get("role")
        dispatch_meta = context.get("dispatch_metadata", {})
        prompt = self._build_prompt(instruction, changed_files, role=role, dispatch_metadata=dispatch_meta)

        chunk_timeout = float(os.environ.get(
            "VNX_GEMINI_STALL_THRESHOLD", context.get("chunk_timeout", _DEFAULT_STALL_THRESHOLD)
        ))
        total_deadline = float(os.environ.get(
            "VNX_GEMINI_TIMEOUT", context.get("total_deadline", _DEFAULT_TIMEOUT)
        ))
        event_store = context.get("event_store")

        collected_events: list[dict] = []
        findings_parts: list[str] = []

        def _collect(tid: str, ev_dict: dict, dispatch_id: str = dispatch_id) -> None:
            collected_events.append(ev_dict)
            ev_type = ev_dict.get("event_type") or ev_dict.get("type", "")
            if ev_type in ("text", "complete", "result"):
                data = ev_dict.get("data")
                text = data if isinstance(data, str) else (data or {}).get("text", "")
                if text:
                    findings_parts.append(text)

        t0 = time.monotonic()
        spawn_result = spawn_gemini(
            prompt=prompt, model=model,
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            event_writer=_collect,
            chunk_timeout=chunk_timeout, total_deadline=total_deadline,
            event_store=event_store,
        )
        duration = time.monotonic() - t0

        if spawn_result.timed_out:
            return AdapterResult(
                status="timeout",
                output=f"Gemini CLI exceeded {int(total_deadline)}s timeout",
                events=collected_events, event_count=len(collected_events),
                duration_seconds=duration, committed=False, commit_hash=None,
                report_path=None, provider="gemini", model=model,
            )

        if spawn_result.token_usage:
            self._write_token_cache(spawn_result.token_usage)

        findings = "\n\n".join(findings_parts) if findings_parts else spawn_result.completion_text
        status = "done" if spawn_result.returncode == 0 else "failed"
        return AdapterResult(
            status=status, output=findings, events=collected_events,
            event_count=len(collected_events), duration_seconds=duration,
            committed=False, commit_hash=None, report_path=None,
            provider="gemini", model=model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """Stream Gemini events live (VNX_GEMINI_STREAM=1) or yield a single
        result event (legacy, VNX_GEMINI_STREAM=0)."""
        model = os.environ.get("VNX_GEMINI_MODEL", _DEFAULT_MODEL)
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        changed_files = context.get("changed_files", [])
        event_store = context.get("event_store")
        chunk_timeout = float(
            os.environ.get("VNX_GEMINI_STALL_THRESHOLD",
                           context.get("chunk_timeout", _DEFAULT_STALL_THRESHOLD))
        )
        total_deadline = float(
            os.environ.get("VNX_GEMINI_TIMEOUT",
                           context.get("total_deadline", _DEFAULT_TIMEOUT))
        )
        role = context.get("role")
        dispatch_meta = context.get("dispatch_metadata", {})

        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        prompt = self._build_prompt(instruction, changed_files, role=role, dispatch_metadata=dispatch_meta)

        if not _gemini_stream_enabled():
            # Legacy path: execute and yield a single synthetic result event
            result = self._execute_legacy(prompt=prompt, model=model)
            yield {
                "type": "result",
                "data": result.output,
                "status": result.status,
                "observability_tier": _TIER_LEGACY,
            }
            return

        # Streaming path
        cmd = ["gemini", "--model", model, "--output-format", "stream-json"]
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
    # StreamingDrainerMixin: event normalizer — delegates to standalone
    # ------------------------------------------------------------------

    def _normalize(self, raw: dict) -> CanonicalEvent:
        """Delegate to normalize_gemini_event (single canonical implementation)."""
        return normalize_gemini_event(raw, self._current_terminal_id, self._current_dispatch_id)

    # ------------------------------------------------------------------
    # Private: streaming execute path
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Private: legacy (buffered) execute path
    # ------------------------------------------------------------------

    def _execute_legacy(self, *, prompt: str, model: str) -> AdapterResult:
        """Legacy path: --output-format json, single buffered response (Tier-3)."""
        timeout = int(os.environ.get("VNX_GEMINI_TIMEOUT", str(_DEFAULT_TIMEOUT)))
        cmd = ["gemini", "--model", model, "--output-format", "json"]
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
                provider="gemini",
                model=model,
            )

        if proc.stdin:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except BrokenPipeError:
                return AdapterResult(
                    status="failed",
                    output="stdin write failed (BrokenPipeError): gemini process exited early",
                    events=[],
                    event_count=0,
                    duration_seconds=0.0,
                    committed=False,
                    commit_hash=None,
                    report_path=None,
                    provider="gemini",
                    model=model,
                )

        stdout, stderr, status = self._drain_with_timeout(proc, timeout)
        duration = time.monotonic() - t0

        if status == "timeout":
            self._kill(proc)
            return AdapterResult(
                status="timeout",
                output=f"Gemini CLI exceeded {timeout}s timeout",
                events=[],
                event_count=0,
                duration_seconds=duration,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="gemini",
                model=model,
            )

        if proc.returncode != 0:
            return AdapterResult(
                status="failed",
                output=stderr or stdout,
                events=[],
                event_count=0,
                duration_seconds=duration,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="gemini",
                model=model,
            )

        parsed = self._parse_response(stdout)
        token_usage = self._parse_token_usage_from_response(stdout)
        if token_usage:
            self._write_token_cache(token_usage)
        return AdapterResult(
            status="done",
            output=parsed,
            events=[{"type": "result", "data": parsed, "observability_tier": _TIER_LEGACY}],
            event_count=1,
            duration_seconds=duration,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="gemini",
            model=model,
        )

    # ------------------------------------------------------------------
    # Token usage helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_gemini_token_count(raw: dict) -> Optional[dict]:
        """Delegate to standalone _extract_gemini_token_count (gemini_spawn owns logic)."""
        return _spawn_extract_token_count(raw)

    @staticmethod
    def _extract_usage_metadata(data: dict) -> Optional[dict]:
        """Delegate to standalone _extract_gemini_usage_metadata (gemini_spawn owns logic)."""
        return _spawn_extract_usage_metadata(data)

    @staticmethod
    def _parse_token_usage_from_response(raw: str) -> Optional[dict]:
        """Parse token counts from Gemini CLI stdout.

        Gemini CLI (--output-format json) returns a JSON object with a top-level
        `usageMetadata` key, or an NDJSON stream where one of the lines contains it.
        Returns None if no parseable metadata is found.
        """
        stripped = raw.strip()
        if not stripped:
            return None
        # Try top-level JSON object
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                result = GeminiAdapter._extract_usage_metadata(data)
                if result:
                    return result
        except json.JSONDecodeError:
            pass
        # Try NDJSON stream (multiple JSON lines)
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    result = GeminiAdapter._extract_usage_metadata(data)
                    if result:
                        return result
            except json.JSONDecodeError:
                continue
        return None

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
            intel_markdown = intel_ctx.serialize_for("gemini") if intel_ctx else ""
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
            result = format_for_provider(assembled, "gemini")
            return f"{result['system_instruction']}\n\n---\n\n{result['prompt']}"

        return full_instruction

    def _drain_with_timeout(
        self, proc: subprocess.Popen, timeout: int
    ) -> tuple[str, str, str]:
        """Read stdout/stderr with timeout; returns (stdout, stderr, status)."""
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        start = time.monotonic()
        stdout_fd = proc.stdout.fileno() if proc.stdout else -1
        stderr_fd = proc.stderr.fileno() if proc.stderr else -1
        fd_map: dict[int, str] = {}
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
                readable, _, _ = select.select(raw_fds, [], [], min(remaining, 1.0))
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
                # Drain remaining
                for fd_num in raw_fds:
                    try:
                        while True:
                            remaining_bytes = os.read(fd_num, 4096)
                            if not remaining_bytes:
                                break
                            if fd_map.get(fd_num) == "stdout":
                                stdout_parts.append(remaining_bytes)
                            else:
                                stderr_parts.append(remaining_bytes)
                    except OSError:
                        pass
                break

        return (
            b"".join(stdout_parts).decode("utf-8", errors="replace"),
            b"".join(stderr_parts).decode("utf-8", errors="replace"),
            "ok",
        )

    @staticmethod
    def _parse_response(raw: str) -> str:
        """Extract findings text from JSON response; fall back to raw text."""
        stripped = raw.strip()
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                for key in ("response", "text", "content", "output"):
                    if key in data:
                        return str(data[key])
            return stripped
        except (json.JSONDecodeError, ValueError):
            return stripped

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
