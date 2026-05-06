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
from vertex_ai_runner import collect_file_contents

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"
_DEFAULT_TIMEOUT = 300
_DEFAULT_STALL_THRESHOLD = 60

# Observability tiers
_TIER_STREAMING = 1   # VNX_GEMINI_STREAM=1: live per-event
_TIER_LEGACY = 3      # VNX_GEMINI_STREAM=0: final-only synthetic result


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
        """Run a Gemini review and return structured findings.

        Routes through streaming drainer when VNX_GEMINI_STREAM=1,
        otherwise uses the legacy buffered path.
        """
        model = os.environ.get("VNX_GEMINI_MODEL", _DEFAULT_MODEL)
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        changed_files = context.get("changed_files", [])

        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        prompt = self._build_prompt(instruction, changed_files)

        if _gemini_stream_enabled():
            return self._execute_streaming(
                prompt=prompt,
                model=model,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                context=context,
            )
        return self._execute_legacy(prompt=prompt, model=model)

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

        self._current_terminal_id = terminal_id
        self._current_dispatch_id = dispatch_id

        prompt = self._build_prompt(instruction, changed_files)

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
        """Map a raw Gemini stream-json event to a CanonicalEvent (Tier-1).

        Gemini --output-format stream-json emits NDJSON where each line has a
        "type" field. Mapping:

          session_start / init      → init
          message / text            → text (content in "text" or "content")
          tool_call / tool_use      → tool_use (function name + args)
          tool_result / tool_response → tool_result (output)
          result / done / complete  → complete (with optional token_count)
          error                     → error

        Unknown types fall through to error with raw_type preserved.
        """
        terminal_id = self._current_terminal_id
        dispatch_id = self._current_dispatch_id

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

        # ── init ──────────────────────────────────────────────────────────
        if etype in ("session_start", "init"):
            return make("init", {"raw_type": etype})

        # ── text / message ────────────────────────────────────────────────
        if etype in ("message", "text", "content"):
            text = raw.get("text") or raw.get("content") or raw.get("message") or ""
            return make("text", {"text": str(text)})

        # ── tool_use / tool_call ──────────────────────────────────────────
        if etype in ("tool_use", "tool_call", "function_call"):
            name = raw.get("name") or raw.get("function_name") or raw.get("tool", "")
            args = raw.get("args") or raw.get("input") or raw.get("arguments") or {}
            if not isinstance(args, dict):
                args = {"raw": str(args)}
            return make("tool_use", {"name": str(name), "args": args})

        # ── tool_result / tool_response ───────────────────────────────────
        if etype in ("tool_result", "tool_response", "function_response"):
            output = raw.get("output") or raw.get("result") or raw.get("content") or ""
            return make("tool_result", {"output": str(output)})

        # ── result / done / complete ──────────────────────────────────────
        if etype in ("result", "done", "complete", "finish"):
            data: dict = {}
            text = raw.get("text") or raw.get("content") or raw.get("output") or ""
            if text:
                data["text"] = str(text)
            token_count = self._extract_gemini_token_count(raw)
            if token_count:
                data["token_count"] = token_count
            return make("complete", data)

        # ── error ─────────────────────────────────────────────────────────
        if etype == "error":
            msg = raw.get("message") or raw.get("error") or raw.get("text") or ""
            return make("error", {"message": str(msg) if msg else str(raw)[:200]})

        # ── usageMetadata (token telemetry emitted mid-stream) ────────────
        if "usageMetadata" in raw:
            token_count = self._extract_gemini_token_count(raw)
            if token_count:
                return make("text", {"text": "", "token_count": token_count})

        # ── unknown → error ───────────────────────────────────────────────
        return make("error", {
            "reason": f"unrecognized gemini event type: {etype!r}",
            "raw_type": etype,
            "raw": str(raw)[:300],
        })

    # ------------------------------------------------------------------
    # Private: streaming execute path
    # ------------------------------------------------------------------

    def _execute_streaming(
        self,
        *,
        prompt: str,
        model: str,
        terminal_id: str,
        dispatch_id: str,
        context: dict,
    ) -> AdapterResult:
        """Spawn gemini with --output-format stream-json, drain via mixin."""
        chunk_timeout = float(
            os.environ.get("VNX_GEMINI_STALL_THRESHOLD",
                           context.get("chunk_timeout", _DEFAULT_STALL_THRESHOLD))
        )
        total_deadline = float(
            os.environ.get("VNX_GEMINI_TIMEOUT",
                           context.get("total_deadline", _DEFAULT_TIMEOUT))
        )
        event_store = context.get("event_store")

        cmd = ["gemini", "--model", model, "--output-format", "stream-json"]
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
            provider="gemini",
            model=model,
        )

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
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()

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
        """Extract and normalize token counts from a Gemini stream event dict."""
        usage_meta = raw.get("usageMetadata")
        if isinstance(usage_meta, dict):
            result = GeminiAdapter._extract_usage_metadata(usage_meta)
            if result:
                return result
        # Also check top-level promptTokenCount (some stream shapes)
        if "promptTokenCount" in raw or "candidatesTokenCount" in raw:
            result = GeminiAdapter._extract_usage_metadata(raw)
            if result:
                return result
        return None

    @staticmethod
    def _extract_usage_metadata(data: dict) -> Optional[dict]:
        """Extract usageMetadata from a parsed Gemini response dict.

        Handles both top-level and nested usageMetadata. Field names follow the
        Gemini REST API: promptTokenCount (input) and candidatesTokenCount (output).
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
        """Combine instruction with inline file contents."""
        payload = {"changed_files": changed_files}
        file_contents = collect_file_contents(payload, subprocess_run=subprocess.run)
        if file_contents:
            return f"{instruction}\n\n{file_contents}"
        return instruction

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
