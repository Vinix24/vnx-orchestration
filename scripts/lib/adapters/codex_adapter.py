#!/usr/bin/env python3
"""adapters/codex_adapter.py — CodexAdapter for review and decision tasks.

Executes code analysis via the `codex` CLI with inline file contents.
Review-only: no CODE capability, no file writes, no git commits.

IMPORTANT: Prompts include inline file contents — no GitHub PR references
are used. This avoids the GitHub app dependency identified in F51-PR1.

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

# Model: empty string = use codex CLI config.toml default (currently gpt-5.3-codex).
# 2026-04-19: gpt-5.2-codex deprecated via Codex CLI model-migration mapping;
# ChatGPT-account auth rejects older explicit model flags.
_DEFAULT_MODEL = ""
_DEFAULT_TIMEOUT = 300
_DEFAULT_STALL_THRESHOLD = 60


class CodexAdapter(ProviderAdapter):
    """Provider adapter for the Codex CLI (review and decision only).

    Streams the prompt via stdin to `codex exec --json -c model="<model>"`,
    parses NDJSON output to extract findings from agent_message events, and
    returns an AdapterResult.  Inline file contents replace PR references.
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id

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

        Builds prompt from instruction + inline file contents from
        context["changed_files"], invokes the codex CLI, and parses NDJSON.
        """
        # Env precedence mirrors gate_runner._build_gate_cmd and
        # GateRequestHandlerMixin._request_codex: HEADLESS_MODEL > MODEL.
        model = (
            os.environ.get("VNX_CODEX_HEADLESS_MODEL")
            or os.environ.get("VNX_CODEX_MODEL")
            or _DEFAULT_MODEL
        )
        timeout = int(os.environ.get("VNX_CODEX_TIMEOUT", str(_DEFAULT_TIMEOUT)))
        stall_threshold = int(
            os.environ.get("VNX_CODEX_STALL_THRESHOLD", str(_DEFAULT_STALL_THRESHOLD))
        )

        changed_files = context.get("changed_files", [])
        prompt = self._build_prompt(instruction, changed_files)

        # Only override model if explicitly set; empty string = let codex use
        # its own config.toml default.
        cmd = ["codex", "exec", "--json"]
        if model:
            cmd += ["-c", f'model="{model}"']
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

        stdout, stderr, status = self._drain_with_stall_detection(
            proc, timeout, stall_threshold
        )
        duration = time.monotonic() - t0

        if status == "timeout":
            self._kill(proc)
            return AdapterResult(
                status="timeout",
                output=f"Codex CLI exceeded {timeout}s timeout",
                events=[],
                event_count=0,
                duration_seconds=duration,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="codex",
                model=model,
            )

        if status == "stall":
            self._kill(proc)
            return AdapterResult(
                status="failed",
                output=f"Codex CLI stalled: no output for {stall_threshold}s",
                events=[],
                event_count=0,
                duration_seconds=duration,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="codex",
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
                provider="codex",
                model=model,
            )

        events, findings = self._parse_ndjson(stdout)
        return AdapterResult(
            status="done",
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
        """Codex CLI emits NDJSON; replays parsed events one by one."""
        result = self.execute(instruction, context)
        for event in result.events:
            yield event
        if not result.events:
            yield {"type": "result", "data": result.output, "status": result.status}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, instruction: str, changed_files: list[str]) -> str:
        """Combine instruction with inline file contents (no PR references)."""
        payload = {"changed_files": changed_files}
        file_contents = collect_file_contents(payload, subprocess_run=subprocess.run)
        if file_contents:
            return f"{instruction}\n\n{file_contents}"
        return instruction

    def _drain_with_stall_detection(
        self, proc: subprocess.Popen, timeout: int, stall_threshold: int
    ) -> tuple[str, str, str]:
        """Read stdout/stderr with timeout and stall detection.

        Returns (stdout, stderr, status) where status is 'ok', 'timeout', or 'stall'.
        """
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        start = time.monotonic()
        last_output_time = start
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
            stall_elapsed = time.monotonic() - last_output_time
            if stall_elapsed >= stall_threshold:
                return (
                    b"".join(stdout_parts).decode("utf-8", errors="replace"),
                    b"".join(stderr_parts).decode("utf-8", errors="replace"),
                    "stall",
                )
            poll_timeout = max(
                min(timeout - elapsed, stall_threshold - stall_elapsed, 1.0), 0.1
            )
            try:
                readable, _, _ = select.select(raw_fds, [], [], poll_timeout)
            except (ValueError, OSError):
                break
            for fd_num in readable:
                try:
                    chunk = os.read(fd_num, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    last_output_time = time.monotonic()
                    if fd_map.get(fd_num) == "stdout":
                        stdout_parts.append(chunk)
                    else:
                        stderr_parts.append(chunk)
            if proc.poll() is not None:
                for fd_num in raw_fds:
                    try:
                        while True:
                            remaining = os.read(fd_num, 4096)
                            if not remaining:
                                break
                            if fd_map.get(fd_num) == "stdout":
                                stdout_parts.append(remaining)
                            else:
                                stderr_parts.append(remaining)
                    except OSError:
                        pass
                break

        return (
            b"".join(stdout_parts).decode("utf-8", errors="replace"),
            b"".join(stderr_parts).decode("utf-8", errors="replace"),
            "ok",
        )

    @staticmethod
    def _parse_ndjson(raw: str) -> tuple[list[dict], str]:
        """Parse NDJSON output; extract agent_message events for findings.

        Returns (events, findings_text).
        """
        events: list[dict] = []
        findings_parts: list[str] = []

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            event_type = event.get("type", "")
            if event_type == "agent_message":
                content = event.get("content", event.get("message", ""))
                if content:
                    findings_parts.append(str(content))
            elif event_type in ("result", "message"):
                content = event.get("content", event.get("text", event.get("output", "")))
                if content:
                    findings_parts.append(str(content))

        findings = "\n\n".join(findings_parts) if findings_parts else raw.strip()
        return events, findings

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
