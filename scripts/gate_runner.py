#!/usr/bin/env python3
"""Gate execution runner with subprocess management, stall detection, and atomic artifacts.

Implements GATE-1/3/6/7/8/9/11/12 from the Gate Execution Lifecycle Contract
(docs/core/180_GATE_EXECUTION_LIFECYCLE_CONTRACT.md).

Entry point: GateRunner.run() — called from ReviewGateManager.execute_gate().
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from governance_receipts import utc_now_iso
from headless_adapter import gate_timeout, gate_stall_threshold

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

    def __init__(
        self,
        state_dir: Path,
        reports_dir: Path,
    ) -> None:
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

        requested → executing → completed|failed
        """
        timeout = gate_timeout(gate)
        stall_threshold = gate_stall_threshold(gate)
        binary = GATE_BINARIES.get(gate)

        if not binary or shutil.which(binary) is None:
            return self._record_not_executable(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="provider_not_installed",
                reason_detail=f"{binary or gate} binary not found in PATH",
                request_payload=request_payload,
            )

        prompt = request_payload.get("prompt", "")
        if not prompt and gate == "gemini_review":
            prompt = self._build_gemini_prompt(request_payload)

        # Ensure prompt is in request_payload for contract_hash fallback
        if prompt and "prompt" not in request_payload:
            request_payload["prompt"] = prompt

        # GATE-3: Mark as executing with started_at and runner_pid
        pid = os.getpid()
        started_at = utc_now_iso()
        request_payload["status"] = "executing"
        request_payload["started_at"] = started_at
        request_payload["runner_pid"] = pid
        self._persist_request(gate, request_payload, pr_number=pr_number, pr_id=pr_id)

        # Run subprocess with stall detection (GATE-6/7/8)
        result = self._run_with_stall_detection(
            gate=gate,
            binary=binary,
            prompt=prompt,
            timeout=timeout,
            stall_threshold=stall_threshold,
            request_payload=request_payload,
        )

        if result["status"] == "failed":
            return self._record_failure(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                result=result, request_payload=request_payload,
            )

        # GATE-11/12: Atomic artifact materialization
        return self._materialize_artifacts(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            stdout=result["stdout"], request_payload=request_payload,
            duration_seconds=result["duration_seconds"],
        )

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
        """Spawn subprocess and monitor for timeout/stall (GATE-6/7/8).

        Uses binary-mode I/O with os.read() for non-blocking reads that
        cannot be held up by TextIOWrapper buffering. Runs the subprocess
        in its own session so process-group kill reaches child processes.
        """
        cli_args = list(GATE_CLI_ARGS.get(gate, []))

        # Model selection — configurable via env vars
        if gate == "gemini_review":
            model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            cli_args = ["--model", model] + cli_args
        elif gate == "codex_gate":
            model = (
                os.environ.get("VNX_CODEX_HEADLESS_MODEL")
                or os.environ.get("VNX_CODEX_MODEL")
                or request_payload.get("model")
                or "gpt-5.4"
            )
            cli_args = cli_args + ["-c", f'model="{model}"']

        cmd = [binary] + cli_args

        start = time.monotonic()
        stdout_parts: List[bytes] = []
        stderr_parts: List[bytes] = []
        last_output_time = start
        output_line_count = 0

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return {
                "status": "failed",
                "reason": "subprocess_error",
                "reason_detail": str(exc),
                "stdout": "",
                "stderr": str(exc),
                "duration_seconds": 0.0,
                "partial_output_lines": 0,
                "runner_pid": os.getpid(),
            }

        try:
            if prompt and proc.stdin:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()

            stdout_fd = proc.stdout.fileno() if proc.stdout else -1
            stderr_fd = proc.stderr.fileno() if proc.stderr else -1
            fd_map = {}
            if stdout_fd >= 0:
                fd_map[stdout_fd] = "stdout"
            if stderr_fd >= 0:
                fd_map[stderr_fd] = "stderr"
            raw_fds = list(fd_map.keys())

            while True:
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    self._kill_process(proc)
                    stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
                    stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
                    return {
                        "status": "failed",
                        "reason": "timeout",
                        "reason_detail": f"Subprocess exceeded {timeout}s timeout",
                        "stdout": stdout,
                        "stderr": stderr,
                        "duration_seconds": elapsed,
                        "partial_output_lines": output_line_count,
                        "runner_pid": proc.pid,
                    }

                stall_elapsed = time.monotonic() - last_output_time
                if stall_elapsed >= stall_threshold:
                    self._kill_process(proc)
                    stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
                    stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
                    return {
                        "status": "failed",
                        "reason": "stall",
                        "reason_detail": f"No output for {stall_threshold}s (stall threshold exceeded)",
                        "stdout": stdout,
                        "stderr": stderr,
                        "duration_seconds": elapsed,
                        "partial_output_lines": output_line_count,
                        "runner_pid": proc.pid,
                    }

                poll_timeout = min(
                    timeout - elapsed,
                    stall_threshold - stall_elapsed,
                    1.0,
                )
                if poll_timeout <= 0:
                    poll_timeout = 0.1

                readable = []
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
                            output_line_count += chunk.count(b"\n")
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
                                    output_line_count += remaining.count(b"\n")
                                else:
                                    stderr_parts.append(remaining)
                        except OSError:
                            pass
                    break

        except Exception as exc:
            self._kill_process(proc)
            stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
            stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
            return {
                "status": "failed",
                "reason": "subprocess_error",
                "reason_detail": str(exc),
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - start,
                "partial_output_lines": output_line_count,
                "runner_pid": proc.pid,
            }

        duration = time.monotonic() - start
        exit_code = proc.returncode
        stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")

        if exit_code != 0:
            return {
                "status": "failed",
                "reason": "exit_nonzero",
                "reason_detail": f"Subprocess exited with code {exit_code}",
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": duration,
                "partial_output_lines": output_line_count,
                "runner_pid": proc.pid,
            }

        return {
            "status": "completed",
            "stdout": stdout,
            "stderr": stderr,
            "duration_seconds": duration,
            "partial_output_lines": output_line_count,
            "runner_pid": proc.pid,
            "exit_code": exit_code,
        }

    @staticmethod
    def _kill_process(proc: subprocess.Popen) -> None:
        """Kill subprocess and its entire process group.

        Uses SIGTERM on the process group first, then SIGKILL if the
        process does not exit within 3 seconds. Falls back to direct
        proc.kill() if process-group operations fail.
        """
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

    def _materialize_artifacts(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        stdout: str,
        request_payload: Dict[str, Any],
        duration_seconds: float,
    ) -> Dict[str, Any]:
        """Atomic artifact materialization (GATE-11/12).

        Sequence: write report → verify → compute hash → write result → verify.
        On any failure, roll back and transition to failed.
        """
        report_path = request_payload.get("report_path", "")
        contract_hash = request_payload.get("contract_hash", "")

        if not contract_hash and "prompt" in request_payload:
            contract_hash = hashlib.sha256(
                request_payload["prompt"].encode("utf-8")
            ).hexdigest()[:16]

        if not contract_hash:
            fallback_input = json.dumps({
                "gate": gate,
                "branch": request_payload.get("branch", ""),
                "changed_files": sorted(request_payload.get("changed_files", [])),
            }, sort_keys=True)
            contract_hash = hashlib.sha256(
                fallback_input.encode("utf-8")
            ).hexdigest()[:16]

        # Step 1: Write normalized report
        if not report_path:
            return self._record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="artifact_materialization_failed",
                reason_detail="No report_path in request payload",
                request_payload=request_payload,
            )

        report_file = Path(report_path)
        try:
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_content = self._format_report(gate, stdout, request_payload)
            report_file.write_text(report_content, encoding="utf-8")
        except OSError as exc:
            return self._record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="artifact_materialization_failed",
                reason_detail=f"Failed to write report: {exc}",
                request_payload=request_payload,
            )

        # Step 2: Verify report exists and is non-empty (GATE-12)
        if not report_file.exists() or report_file.stat().st_size == 0:
            return self._record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="artifact_materialization_failed",
                reason_detail="Report file is empty or missing after write",
                request_payload=request_payload,
            )

        # Step 2b: Validate report contains substantive content (OI-273)
        stripped = stdout.strip()
        content_lines = [ln for ln in stripped.splitlines() if ln.strip()] if stripped else []
        if len(content_lines) < 3 and stripped != "(no output)":
            return self._record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="empty_review_content",
                reason_detail=f"Gate output has only {len(content_lines)} substantive line(s); expected review content",
                request_payload=request_payload,
            )

        # Step 3-4: Write result record
        now = utc_now_iso()
        result_payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "completed",
            "summary": f"{gate} execution completed successfully",
            "contract_hash": contract_hash,
            "report_path": str(report_file),
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [],
            "residual_risk": "",
            "duration_seconds": duration_seconds,
            "recorded_at": now,
        }

        try:
            if pr_id:
                result_file = self._results_dir / f"{pr_id.lower().replace('-', '')}-{gate}-contract.json"
            elif pr_number is not None:
                result_file = self._results_dir / f"pr-{pr_number}-{gate}.json"
            else:
                raise ValueError("pr_number or pr_id required")

            result_json = json.dumps(result_payload, indent=2)
            json.loads(result_json)  # Step 5: verify valid JSON
            result_file.write_text(result_json, encoding="utf-8")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # Roll back: remove report if result write failed (GATE-11)
            try:
                report_file.unlink(missing_ok=True)
            except OSError:
                pass
            return self._record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="artifact_materialization_failed",
                reason_detail=f"Failed to write result record: {exc}",
                request_payload=request_payload,
            )

        # Step 5 continued: Verify consistency (GATE-12)
        if not result_file.exists():
            try:
                report_file.unlink(missing_ok=True)
            except OSError:
                pass
            return self._record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="artifact_materialization_failed",
                reason_detail="Result file missing after write",
                request_payload=request_payload,
            )

        # Update request to completed
        request_payload["status"] = "completed"
        request_payload["completed_at"] = now
        self._persist_request(gate, request_payload, pr_number=pr_number, pr_id=pr_id)

        return result_payload

    def _format_report(self, gate: str, stdout: str, request_payload: Dict[str, Any]) -> str:
        """Format gate output as a normalized headless report."""
        pr_ref = request_payload.get("pr_id") or str(request_payload.get("pr_number", ""))
        branch = request_payload.get("branch", "")
        lines = [
            f"# {gate} — Headless Gate Report",
            "",
            f"**PR**: {pr_ref}",
            f"**Branch**: {branch}",
            f"**Gate**: {gate}",
            f"**Generated**: {utc_now_iso()}",
            "",
            "---",
            "",
            "## Gate Output",
            "",
            stdout.strip() if stdout.strip() else "(no output)",
            "",
        ]
        return "\n".join(lines) + "\n"

    def _record_not_executable(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        reason: str,
        reason_detail: str,
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record not_executable and write skip-rationale (GATE-4/9)."""
        now = utc_now_iso()
        request_payload["status"] = "not_executable"
        request_payload["reason"] = reason
        request_payload["reason_detail"] = reason_detail
        request_payload["resolved_at"] = now
        self._persist_request(gate, request_payload, pr_number=pr_number, pr_id=pr_id)

        result_payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "not_executable",
            "reason": reason,
            "reason_detail": reason_detail,
            "summary": f"{gate} not executable: {reason_detail}",
            "contract_hash": request_payload.get("contract_hash", ""),
            "report_path": "",
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [],
            "residual_risk": "Gate evidence not available. Compensating evidence required.",
            "recorded_at": now,
        }

        result_file = self._result_file_path(gate, pr_number=pr_number, pr_id=pr_id)
        if result_file:
            result_file.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

        self._write_skip_rationale(gate=gate, pr_id=pr_id or str(pr_number or ""), reason=reason, reason_detail=reason_detail)
        return result_payload

    def _record_failure(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        result: Dict[str, Any],
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record a failed gate execution (timeout/stall/error)."""
        now = utc_now_iso()
        request_payload["status"] = "failed"
        request_payload["failed_at"] = now
        self._persist_request(gate, request_payload, pr_number=pr_number, pr_id=pr_id)

        failure_payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "failed",
            "reason": result["reason"],
            "reason_detail": result["reason_detail"],
            "duration_seconds": result["duration_seconds"],
            "partial_output_lines": result["partial_output_lines"],
            "runner_pid": result["runner_pid"],
            "killed_at": now,
            "summary": f"Gate execution {result['reason']}: {result['reason_detail']}",
            "contract_hash": request_payload.get("contract_hash", ""),
            "report_path": "",
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [gate],
            "residual_risk": f"Gate {result['reason']}. Re-run required.",
            "recorded_at": now,
        }

        result_file = self._result_file_path(gate, pr_number=pr_number, pr_id=pr_id)
        if result_file:
            result_file.write_text(json.dumps(failure_payload, indent=2), encoding="utf-8")
        return failure_payload

    def _record_failure_simple(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        reason: str,
        reason_detail: str,
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record a simple failure (artifact materialization errors)."""
        return self._record_failure(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            result={
                "reason": reason,
                "reason_detail": reason_detail,
                "duration_seconds": 0.0,
                "partial_output_lines": 0,
                "runner_pid": os.getpid(),
            },
            request_payload=request_payload,
        )

    def _persist_request(
        self,
        gate: str,
        payload: Dict[str, Any],
        *,
        pr_number: Optional[int],
        pr_id: str,
    ) -> None:
        """Write request payload to disk."""
        if pr_id:
            slug = pr_id.lower().replace("-", "")
            path = self._requests_dir / f"{slug}-{gate}-contract.json"
        elif pr_number is not None:
            path = self._requests_dir / f"pr-{pr_number}-{gate}.json"
        else:
            return
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _result_file_path(
        self,
        gate: str,
        *,
        pr_number: Optional[int],
        pr_id: str,
    ) -> Optional[Path]:
        if pr_id:
            slug = pr_id.lower().replace("-", "")
            return self._results_dir / f"{slug}-{gate}-contract.json"
        if pr_number is not None:
            return self._results_dir / f"pr-{pr_number}-{gate}.json"
        return None

    def _write_skip_rationale(
        self,
        *,
        gate: str,
        pr_id: str,
        reason: str,
        reason_detail: str,
    ) -> None:
        """Append skip-rationale record to NDJSON audit trail (GATE-9)."""
        binary = GATE_BINARIES.get(gate, gate)
        env_flags = {
            "gemini_review": "VNX_GEMINI_REVIEW_ENABLED",
            "codex_gate": "VNX_CODEX_HEADLESS_ENABLED",
            "claude_github_optional": "VNX_CLAUDE_GITHUB_REVIEW_ENABLED",
        }
        env_var = env_flags.get(gate, "")
        record = {
            "event_type": "gate_skip_rationale",
            "gate": gate,
            "pr_id": pr_id,
            "reason": reason,
            "reason_detail": reason_detail,
            "provider_check": {
                "binary_name": binary,
                "binary_found": shutil.which(binary) is not None,
                "env_flag": env_var,
                "env_value": os.environ.get(env_var, ""),
            },
            "compensating_action": "Manual review or operator override required.",
            "timestamp": utc_now_iso(),
        }
        audit_path = self._state_dir / "gate_execution_audit.ndjson"
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    @staticmethod
    def _build_gemini_prompt(request_payload: Dict[str, Any]) -> str:
        """Build a minimal prompt from request payload when no prompt is present."""
        files = request_payload.get("changed_files", [])
        branch = request_payload.get("branch", "")
        risk = request_payload.get("risk_class", "medium")
        return f"Review the following changes on branch {branch} (risk: {risk}):\nFiles: {', '.join(files)}\n"

    @staticmethod
    def verify_artifact_consistency(
        result_path: Path,
        contract_content: str = "",
    ) -> bool:
        """Verify artifact consistency (GATE-12/13).

        Returns True if all checks pass.
        """
        if not result_path.exists():
            return False

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        report_path = result.get("report_path", "")
        if report_path:
            rp = Path(report_path)
            if not rp.exists() or rp.stat().st_size == 0:
                return False

        if contract_content and result.get("contract_hash"):
            expected = hashlib.sha256(contract_content.encode("utf-8")).hexdigest()[:16]
            if result["contract_hash"] != expected:
                return False

        required_fields = ["gate", "status", "recorded_at"]
        for field in required_fields:
            if field not in result:
                return False

        return True
