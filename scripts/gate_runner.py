#!/usr/bin/env python3
"""Gate execution runner with subprocess management, stall detection, and atomic artifacts.

Implements GATE-1/3/6/7/8/9/11/12 from the Gate Execution Lifecycle Contract
(docs/core/180_GATE_EXECUTION_LIFECYCLE_CONTRACT.md).

Entry point: GateRunner.run() — called from ReviewGateManager.execute_gate().
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from governance_receipts import utc_now_iso
from headless_adapter import gate_timeout, gate_stall_threshold
import gate_recorder as _rec
import gate_artifacts as _art
import vertex_ai_runner as _vtx
from prompt_assembler import PromptAssembler, format_for_provider

_REVIEWER_VERDICT_TEMPLATE = (
    "Respond with a structured JSON verdict only:\n"
    "```json\n"
    "{\n"
    '  "verdict": "pass|fail|blocked",\n'
    '  "findings": [{"severity": "error|warning|info", "message": "...", "out_of_scope": false, "introduced_by_prior_fix": false}],\n'
    '  "residual_risk": "description of remaining risks or null",\n'
    '  "rerun_required": false,\n'
    '  "rerun_reason": null\n'
    "}\n"
    "```\n"
)

# Minimum deleted-file count before injecting a Net-Deletion Alert into the reviewer prompt.
# Mirrors DELETION_FILE_WARN in codex_final_gate.py and pre_merge_gate.py (both use 5).
_GATE_DELETION_FILE_WARN = 5

# Gate type → CLI binary mapping
GATE_BINARIES: Dict[str, str] = {
    "gemini_review": "gemini",
    "codex_gate": "codex",
    "claude_github_optional": "gh",
}

# Gate type → CLI args for review execution
GATE_CLI_ARGS: Dict[str, List[str]] = {
    "gemini_review": ["--output-format", "json"],
    "codex_gate": ["exec", "--json"],
    "claude_github_optional": [],
}


class GateRunner:
    """Subprocess-based gate execution with timeout and stall detection."""

    def __init__(self, state_dir: Path, reports_dir: Path) -> None:
        self._state_dir = state_dir
        self._reports_dir = reports_dir
        self._requests_dir = state_dir / "review_gates" / "requests"
        self._results_dir = state_dir / "review_gates" / "results"

    def run(
        self,
        *,
        gate: str,
        request_payload: Dict[str, Any],
        pr_number: Optional[int] = None,
        pr_id: str = "",
    ) -> Dict[str, Any]:
        """Execute a gate through its full lifecycle (GATE-1).

        requested -> executing -> completed|failed
        """
        binary = GATE_BINARIES.get(gate)
        using_vertex = gate == "gemini_review" and os.environ.get("VNX_GEMINI_ROUTING", "oauth") == "vertex"

        if not using_vertex:
            if not binary or shutil.which(binary) is None:
                return _rec.record_not_executable(
                    gate=gate, pr_number=pr_number, pr_id=pr_id,
                    reason="provider_not_installed",
                    reason_detail=f"{binary or gate} binary not found in PATH",
                    request_payload=request_payload,
                    requests_dir=self._requests_dir,
                    results_dir=self._results_dir,
                    state_dir=self._state_dir,
                )

        prompt = self._resolve_prompt(gate, request_payload, using_vertex)
        if prompt and "prompt" not in request_payload:
            request_payload["prompt"] = prompt

        self._mark_executing(gate, request_payload, pr_number=pr_number, pr_id=pr_id)

        if using_vertex:
            return self._run_vertex_path(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                prompt=prompt, request_payload=request_payload, pid=os.getpid(),
            )

        return self._run_subprocess_path(
            gate=gate, binary=binary, prompt=prompt,
            pr_number=pr_number, pr_id=pr_id, request_payload=request_payload,
        )

    def _resolve_prompt(
        self, gate: str, request_payload: Dict[str, Any], using_vertex: bool,
    ) -> str:
        """Build or enrich the prompt for the given gate type.

        build_gemini_prompt / build_codex_prompt already inline file contents.
        When we build the prompt here we must NOT also append
        collect_file_contents, or each ``--- FILE:`` section will be duplicated
        in the Vertex prompt. We only enrich with file contents when the caller
        supplied the prompt externally (e.g. a contract prompt) and the prompt
        therefore does not yet carry the file bodies.
        """
        external_prompt = request_payload.get("prompt", "")
        prompt = external_prompt
        if not prompt and gate == "gemini_review":
            prompt = self._build_gemini_prompt(request_payload)
        elif not prompt and gate == "codex_gate":
            prompt = self._build_codex_prompt(request_payload)
        if (
            using_vertex
            and gate == "gemini_review"
            and prompt
            and external_prompt
        ):
            file_contents = _vtx.collect_file_contents(
                request_payload, subprocess_run=subprocess.run,
            )
            if file_contents:
                prompt = prompt + "\n\n" + file_contents
        return prompt

    def _mark_executing(
        self, gate: str, request_payload: Dict[str, Any], *,
        pr_number: Optional[int], pr_id: str,
    ) -> None:
        """GATE-3: Mark request as executing and persist to disk."""
        request_payload["status"] = "executing"
        request_payload["started_at"] = utc_now_iso()
        request_payload["runner_pid"] = os.getpid()
        _rec.persist_request(
            self._requests_dir, gate, request_payload,
            pr_number=pr_number, pr_id=pr_id,
        )

    def _run_subprocess_path(
        self, *, gate: str, binary: str, prompt: str,
        pr_number: Optional[int], pr_id: str, request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute gate via subprocess with stall detection, then record result."""
        result = self._run_with_stall_detection(
            gate=gate, binary=binary, prompt=prompt,
            timeout=gate_timeout(gate), stall_threshold=gate_stall_threshold(gate),
            request_payload=request_payload,
        )
        if result["status"] == "failed":
            return _rec.record_failure(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                result=result, request_payload=request_payload,
                requests_dir=self._requests_dir, results_dir=self._results_dir,
            )
        return _art.materialize_artifacts(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            stdout=result["stdout"], request_payload=request_payload,
            duration_seconds=result["duration_seconds"],
            requests_dir=self._requests_dir, results_dir=self._results_dir,
            reports_dir=self._reports_dir,
        )

    def _run_vertex_path(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        prompt: str,
        request_payload: Dict[str, Any],
        pid: int,
    ) -> Dict[str, Any]:
        """Run Vertex AI REST path and feed output into artifact pipeline."""
        _start = time.monotonic()
        try:
            raw_text = self._run_vertex_ai(prompt)
        except Exception as exc:
            duration = time.monotonic() - _start
            return _rec.record_failure(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                result={
                    "reason": "vertex_api_error",
                    "reason_detail": str(exc),
                    "duration_seconds": duration,
                    "partial_output_lines": 0,
                    "runner_pid": pid,
                },
                request_payload=request_payload,
                requests_dir=self._requests_dir,
                results_dir=self._results_dir,
            )
        return _art.materialize_artifacts(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            stdout=raw_text, request_payload=request_payload,
            duration_seconds=time.monotonic() - _start,
            requests_dir=self._requests_dir, results_dir=self._results_dir,
            reports_dir=self._reports_dir,
        )

    # Vertex AI wrappers — stay here so tests can patch gate_runner.subprocess.run

    def _run_vertex_ai(self, prompt: str) -> str:
        """Call Vertex AI REST API. Delegates to vertex_ai_runner."""
        return _vtx.run_vertex_ai(
            prompt,
            subprocess_run=subprocess.run,
            urlopen=urllib.request.urlopen,
        )

    @staticmethod
    def _extract_deleted_files_from_diff(diff_content: str) -> List[str]:
        """Parse unified diff output and return paths of fully deleted files.

        A file is considered deleted when a ``deleted file mode`` header immediately
        follows its ``diff --git a/path b/path`` line. Modified files have an index
        line instead and are not included.

        Uses subprocess.run so tests can patch gate_runner.subprocess.run.
        """
        deleted: List[str] = []
        current_file: Optional[str] = None
        for line in diff_content.splitlines():
            if line.startswith("diff --git "):
                parts = line.split(" ", 3)
                current_file = (
                    parts[3][2:] if len(parts) == 4 and parts[3].startswith("b/") else None
                )
            elif line.startswith("deleted file mode") and current_file is not None:
                deleted.append(current_file)
                current_file = None
        return deleted

    @staticmethod
    def _build_deletion_alert_section(diff_content: str) -> str:
        """Return a formatted Net-Deletion Alert block for reviewer prompts.

        Returns an empty string when the deleted-file count is below
        ``_GATE_DELETION_FILE_WARN``. When at or above the threshold, returns a
        markdown block listing all deleted file paths so the reviewer notices the
        scope reduction and can flag accidental deletions.
        """
        deleted_files = GateRunner._extract_deleted_files_from_diff(diff_content)
        if len(deleted_files) < _GATE_DELETION_FILE_WARN:
            return ""
        file_list = "\n".join(f"- `{f}`" for f in deleted_files)
        return (
            f"\n\n## Net-Deletion Alert ({len(deleted_files)} file(s) deleted)\n\n"
            "> **Review required**: these files are fully deleted in this PR. "
            "Confirm each deletion is intentional and within declared scope.\n\n"
            f"{file_list}"
        )

    @staticmethod
    def _fetch_gh_pr_diff(pr_number: Optional[int]) -> str:
        """Fetch authoritative PR diff via gh pr diff.

        Uses subprocess.run so tests can patch gate_runner.subprocess.run.
        Raises ValueError when pr_number is missing.
        Raises RuntimeError when gh pr diff exits non-zero.
        Never returns empty on failure — callers get a loud error, not silent empty.
        """
        if not pr_number:
            raise ValueError(
                "pr_number is required for reviewer gate; "
                "cannot fetch diff without a PR number"
            )
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh pr diff {pr_number} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    @staticmethod
    def _build_codex_prompt(request_payload: Dict[str, Any]) -> str:
        """Build reviewer prompt for codex gate using gh pr diff as authoritative diff source.

        Uses subprocess.run so tests can patch gate_runner.subprocess.run.
        Raises on missing pr_number or gh pr diff failure — no silent empty-diff fallback.
        Injects a Net-Deletion Alert section when >= _GATE_DELETION_FILE_WARN files are deleted.
        """
        branch = request_payload.get("branch", "")
        risk = (request_payload.get("risk_class") or "medium")
        pr_number = request_payload.get("pr_number")
        diff_content = GateRunner._fetch_gh_pr_diff(pr_number)
        deletion_alert = GateRunner._build_deletion_alert_section(diff_content)
        l3 = (
            f"Review the PR diff below on branch {branch} (risk: {risk}). "
            "Findings MUST cite specific NEW lines from this diff — "
            "do not flag pre-existing code.\n\n"
            f"{diff_content}{deletion_alert}\n\n{_REVIEWER_VERDICT_TEMPLATE}"
        )
        assembled = PromptAssembler().assemble(
            dispatch_metadata={"role": "reviewer"},
            instruction=l3,
        )
        return format_for_provider(assembled, "codex")["pipe_input"]

    @staticmethod
    def _build_gemini_prompt(request_payload: Dict[str, Any]) -> str:
        """Build reviewer prompt for gemini gate using gh pr diff as authoritative diff source.

        Uses subprocess.run so tests can patch gate_runner.subprocess.run.
        Raises on missing pr_number or gh pr diff failure — no silent empty-diff fallback.
        Injects a Net-Deletion Alert section when >= _GATE_DELETION_FILE_WARN files are deleted.
        """
        branch = request_payload.get("branch", "")
        risk = (request_payload.get("risk_class") or "medium")
        pr_number = request_payload.get("pr_number")
        diff_content = GateRunner._fetch_gh_pr_diff(pr_number)
        deletion_alert = GateRunner._build_deletion_alert_section(diff_content)
        l3 = (
            f"Review the PR diff below on branch {branch} (risk: {risk}). "
            "Findings MUST cite specific NEW lines from this diff — "
            "do not flag pre-existing code.\n\n"
            f"{diff_content}{deletion_alert}\n\n{_REVIEWER_VERDICT_TEMPLATE}"
        )
        assembled = PromptAssembler().assemble(
            dispatch_metadata={"role": "reviewer"},
            instruction=l3,
        )
        formatted = format_for_provider(assembled, "gemini")
        return f"{formatted['system_instruction']}\n\n---\n\n{formatted['prompt']}"

    # Subprocess execution — stays here so tests can patch gate_runner.subprocess.Popen,
    # gate_runner.os.read, gate_runner.select.select, gate_runner.os.getpgid

    def _build_gate_cmd(self, gate: str, binary: str, request_payload: Dict[str, Any]) -> List[str]:
        """Build CLI command list with model selection for the given gate."""
        cli_args = list(GATE_CLI_ARGS.get(gate, []))
        if gate == "gemini_review":
            model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
            cli_args = ["--model", model] + cli_args
        elif gate == "codex_gate":
            # Model selection: only override if explicitly requested via env/payload.
            # Default path: let codex use ~/.codex/config.toml (currently gpt-5.3-codex).
            # 2026-04-19: gpt-5.2-codex deprecated via Codex CLI model-migration mapping;
            # ChatGPT-account auth rejects older explicit model flags, causing gate
            # failures with "model not supported when using Codex with a ChatGPT account".
            model = (
                os.environ.get("VNX_CODEX_HEADLESS_MODEL")
                or os.environ.get("VNX_CODEX_MODEL")
                or request_payload.get("model")
            )
            if model:
                cli_args = cli_args + ["-c", f'model="{model}"']
        return [binary] + cli_args

    def _drain_remaining(self, fd_map: Dict[int, str], raw_fds: List[int],
                          stdout_parts: List[bytes], stderr_parts: List[bytes],
                          line_count: int) -> int:
        """Drain all remaining output after process exits; returns updated line count."""
        for fd_num in raw_fds:
            try:
                while True:
                    remaining = os.read(fd_num, 4096)
                    if not remaining:
                        break
                    if fd_map.get(fd_num) == "stdout":
                        stdout_parts.append(remaining)
                        line_count += remaining.count(b"\n")
                    else:
                        stderr_parts.append(remaining)
            except OSError:
                pass
        return line_count

    def _poll_io(self, proc: subprocess.Popen, fd_map: Dict[int, str],
                 raw_fds: List[int], stdout_parts: List[bytes], stderr_parts: List[bytes],
                 timeout: int, stall_threshold: int, start: float,
                 last_output_time: float, line_count: int) -> tuple:
        """One poll iteration: check deadlines, read readable FDs.

        Returns (status_or_None, elapsed, line_count, last_output_time).
        status is 'timeout', 'stall', or None (continue).
        """
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            self._kill_process(proc)
            return "timeout", elapsed, line_count, last_output_time
        stall_elapsed = time.monotonic() - last_output_time
        if stall_elapsed >= stall_threshold:
            self._kill_process(proc)
            return "stall", elapsed, line_count, last_output_time
        poll_timeout = max(
            min(timeout - elapsed, stall_threshold - stall_elapsed, 1.0), 0.1
        )
        readable: List[int] = []
        try:
            readable, _, _ = select.select(raw_fds, [], [], poll_timeout)
        except (ValueError, OSError):
            pass
        for fd_num in readable:
            try:
                chunk = os.read(fd_num, 4096)
            except OSError:
                chunk = b""
            if chunk:
                last_output_time = time.monotonic()
                if fd_map.get(fd_num) == "stdout":
                    stdout_parts.append(chunk)
                    line_count += chunk.count(b"\n")
                else:
                    stderr_parts.append(chunk)
        return None, elapsed, line_count, last_output_time

    def _subprocess_io_loop(self, proc: subprocess.Popen, fd_map: Dict[int, str],
                             raw_fds: List[int], timeout: int, stall_threshold: int,
                             start: float) -> tuple:
        """Monitor subprocess I/O with timeout/stall detection (GATE-6/7/8).

        Returns (status, elapsed, stdout_parts, stderr_parts, line_count).
        """
        stdout_parts: List[bytes] = []
        stderr_parts: List[bytes] = []
        last_output_time = start
        line_count = 0
        while True:
            status, elapsed, line_count, last_output_time = self._poll_io(
                proc, fd_map, raw_fds, stdout_parts, stderr_parts,
                timeout, stall_threshold, start, last_output_time, line_count,
            )
            if status:
                return status, elapsed, stdout_parts, stderr_parts, line_count
            if proc.poll() is not None:
                line_count = self._drain_remaining(
                    fd_map, raw_fds, stdout_parts, stderr_parts, line_count
                )
                break
        return "ok", time.monotonic() - start, stdout_parts, stderr_parts, line_count

    def _run_with_stall_detection(
        self,
        *,
        gate: str,
        binary: str,
        prompt: str,
        timeout: int,
        stall_threshold: int,
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Spawn subprocess and monitor for timeout/stall (GATE-6/7/8)."""
        cmd = self._build_gate_cmd(gate, binary, request_payload)
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return {
                "status": "failed", "reason": "subprocess_error",
                "reason_detail": str(exc), "stdout": "", "stderr": str(exc),
                "duration_seconds": 0.0, "partial_output_lines": 0,
                "runner_pid": os.getpid(),
            }
        if proc.stdin:
            if prompt:
                proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        stdout_fd = proc.stdout.fileno() if proc.stdout else -1
        stderr_fd = proc.stderr.fileno() if proc.stderr else -1
        fd_map = {fd: k for fd, k in [(stdout_fd, "stdout"), (stderr_fd, "stderr")] if fd >= 0}
        try:
            status, elapsed, stdout_parts, stderr_parts, lcount = self._subprocess_io_loop(
                proc, fd_map, list(fd_map), timeout, stall_threshold, start
            )
        except Exception as exc:
            self._kill_process(proc)
            return {
                "status": "failed", "reason": "subprocess_error",
                "reason_detail": str(exc), "stdout": "", "stderr": "",
                "duration_seconds": time.monotonic() - start,
                "partial_output_lines": 0, "runner_pid": proc.pid,
            }
        stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
        _base = {"stdout": stdout, "stderr": stderr, "duration_seconds": elapsed,
                 "partial_output_lines": lcount, "runner_pid": proc.pid}
        if status == "timeout":
            return {"status": "failed", "reason": "timeout",
                    "reason_detail": f"Subprocess exceeded {timeout}s timeout", **_base}
        if status == "stall":
            return {"status": "failed", "reason": "stall",
                    "reason_detail": f"No output for {stall_threshold}s (stall threshold exceeded)",
                    **_base}
        if proc.returncode != 0:
            return {"status": "failed", "reason": "exit_nonzero",
                    "reason_detail": f"Subprocess exited with code {proc.returncode}", **_base}
        return {"status": "completed", **_base, "exit_code": proc.returncode}

    @staticmethod
    def _kill_process(proc: subprocess.Popen) -> None:
        """Kill subprocess and its entire process group (SIGTERM then SIGKILL)."""
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pass
        if pgid is not None and pgid != os.getpgrp():
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def verify_artifact_consistency(
        result_path: Path,
        contract_content: str = "",
    ) -> bool:
        """Verify artifact consistency (GATE-12/13). Returns True if all checks pass."""
        return _art.verify_artifact_consistency(result_path, contract_content)
