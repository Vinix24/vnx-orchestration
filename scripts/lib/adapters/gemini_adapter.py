#!/usr/bin/env python3
"""adapters/gemini_adapter.py — GeminiAdapter for review and digest tasks.

Executes code review via the `gemini` CLI (not the Vertex AI REST path).
Review-only: no CODE capability, no file writes, no git commits.

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
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_adapter import AdapterResult, Capability, ProviderAdapter
from vertex_ai_runner import collect_file_contents

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"
_DEFAULT_TIMEOUT = 300


class GeminiAdapter(ProviderAdapter):
    """Provider adapter for the Gemini CLI (review and digest only).

    Streams the prompt via stdin to `gemini --model <model> --output-format json`
    and parses the JSON response into an AdapterResult.
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id

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

        Builds prompt from instruction + inline file contents from
        context["changed_files"], then invokes the gemini CLI.
        """
        model = os.environ.get("VNX_GEMINI_MODEL", _DEFAULT_MODEL)
        timeout = int(os.environ.get("VNX_GEMINI_TIMEOUT", str(_DEFAULT_TIMEOUT)))

        changed_files = context.get("changed_files", [])
        prompt = self._build_prompt(instruction, changed_files)

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
        return AdapterResult(
            status="done",
            output=parsed,
            events=[{"type": "result", "data": parsed}],
            event_count=1,
            duration_seconds=duration,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="gemini",
            model=model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """Gemini CLI does not support streaming; yields a single result event."""
        result = self.execute(instruction, context)
        yield {"type": "result", "data": result.output, "status": result.status}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        # gemini --output-format json may wrap the response in a JSON object
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                # Common response shapes
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
