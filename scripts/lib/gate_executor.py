"""Gate execution orchestration (GateExecutorMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle gate execution, contract-driven execution, and status queries.
Request creation methods are in gate_request_handler.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _get_pr_head_sha(pr_number: Optional[int]) -> str:
    """Fetch the HEAD commit SHA for a PR via gh CLI. Returns empty string on failure."""
    if pr_number is None:
        return ""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefOid"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            return data.get("headRefOid", "")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        pass
    return ""


def _format_ci_gate_report(
    *,
    pr_number: Optional[int],
    branch: str,
    checks: List[Dict[str, Any]],
    passed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    status: str,
    generated_at: str,
) -> str:
    """Format ci_gate results as a normalized headless report."""
    verdict_icon = {"pass": "PASS", "fail": "FAIL", "running": "RUNNING"}.get(status, status.upper())
    lines = [
        "# ci_gate — Headless Gate Report",
        "",
        f"**PR**: {pr_number or 'unknown'}",
        f"**Branch**: {branch}",
        f"**Gate**: ci_gate",
        f"**Generated**: {generated_at}",
        f"**Verdict**: {verdict_icon}",
        "",
        "---",
        "",
        "## CI Check Results",
        "",
    ]
    if not checks:
        lines.append("_(no checks found — vacuously passing)_")
    else:
        if passed:
            lines.append("### Passed")
            for c in passed:
                lines.append(f"- [OK] {c.get('name', '?')} — {c.get('conclusion', 'SUCCESS')}")
            lines.append("")
        if failed:
            lines.append("### Failed [BLOCKING]")
            for c in failed:
                lines.append(f"- [BLOCKING] {c.get('name', '?')} — {c.get('conclusion', 'FAILURE')}")
                lines.append(f"  - **Severity**: blocking")
            lines.append("")
        if skipped:
            lines.append("### Skipped / Cancelled")
            for c in skipped:
                lines.append(f"- [ADVISORY] {c.get('name', '?')} — {c.get('conclusion', 'SKIPPED')}")
            lines.append("")
    lines.append(f"**Status**: {'FAIL' if failed else 'PASS' if status == 'pass' else status.upper()}")
    lines.append(f"Passed: {len(passed)} | Failed: {len(failed)} | Skipped: {len(skipped)}")
    lines.append("")
    return "\n".join(lines) + "\n"


class GateExecutorMixin:
    """Mixin providing gate execution and status methods for ReviewGateManager."""

    def execute_gate(
        self,
        *,
        gate: str,
        pr_number: Optional[int] = None,
        pr_id: str = "",
    ) -> Dict[str, Any]:
        """Execute a gate: transition requested->executing->completed|failed (GATE-1).

        Loads the request record, starts the gate subprocess with bounded timeout
        and stall detection, then writes result records atomically (GATE-11/12).
        ci_gate is executed inline via gh CLI (no stall-detection subprocess loop).
        """
        from gate_runner import GateRunner

        if pr_id:
            request_payload = self._load_contract_request_payload(gate, pr_id)
        elif pr_number is not None:
            request_payload = self._load_request_payload(gate, pr_number)
        else:
            raise ValueError("pr_number or pr_id is required")

        if not request_payload:
            raise ValueError(f"No request record found for gate={gate}")

        status = request_payload.get("status", "")
        if status in ("not_executable", "completed", "failed"):
            return request_payload

        if gate == "ci_gate":
            return self._execute_ci_gate(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                request_payload=request_payload,
            )

        runner = GateRunner(
            state_dir=self.state_dir,
            reports_dir=self.reports_dir,
        )
        return runner.run(
            gate=gate,
            request_payload=request_payload,
            pr_number=pr_number,
            pr_id=pr_id,
        )

    def _execute_ci_gate(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute ci_gate: call gh pr checks, parse results, write report and result JSON.

        Maps GitHub Actions check conclusions to blocking/advisory findings:
        - FAILURE → blocking_finding
        - SKIPPED / CANCELLED → advisory_finding
        - SUCCESS → passed_check
        Vacuously passes when there are no checks at all (Case D).
        """
        import gate_recorder as _rec
        from governance_receipts import utc_now_iso

        # Mark as executing
        request_payload["status"] = "executing"
        request_payload["started_at"] = utc_now_iso()
        request_payload["runner_pid"] = os.getpid()
        _rec.persist_request(
            self.requests_dir, gate, request_payload,
            pr_number=pr_number, pr_id=pr_id,
        )

        if not shutil.which("gh"):
            return _rec.record_not_executable(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="provider_not_installed",
                reason_detail="gh binary not found in PATH",
                request_payload=request_payload,
                requests_dir=self.requests_dir,
                results_dir=self.results_dir,
                state_dir=self.state_dir,
            )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["gh", "pr", "checks", str(pr_number), "--json", "name,status,conclusion"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except subprocess.TimeoutExpired:
            return _rec.record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="timeout",
                reason_detail="gh pr checks exceeded 60s timeout",
                request_payload=request_payload,
                requests_dir=self.requests_dir,
                results_dir=self.results_dir,
            )
        except OSError as exc:
            return _rec.record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="subprocess_error",
                reason_detail=str(exc),
                request_payload=request_payload,
                requests_dir=self.requests_dir,
                results_dir=self.results_dir,
            )

        duration = time.monotonic() - start

        # Parse gh output — treat "no checks" (empty or returncode!=0 with that message) as vacuous pass
        checks_list: List[Dict[str, Any]] = []
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                checks_list = json.loads(proc.stdout)
            except json.JSONDecodeError:
                pass
        elif proc.returncode != 0:
            stderr_lower = (proc.stderr or "").lower()
            if "no checks" not in stderr_lower and "has no checks" not in stderr_lower:
                return _rec.record_failure_simple(
                    gate=gate, pr_number=pr_number, pr_id=pr_id,
                    reason="exit_nonzero",
                    reason_detail=f"gh pr checks exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}",
                    request_payload=request_payload,
                    requests_dir=self.requests_dir,
                    results_dir=self.results_dir,
                )
            # no checks → vacuous pass; checks_list stays empty

        passed = [c for c in checks_list if (c.get("conclusion") or "").upper() == "SUCCESS"]
        failed = [c for c in checks_list if (c.get("conclusion") or "").upper() == "FAILURE"]
        skipped = [c for c in checks_list if (c.get("conclusion") or "").upper() in ("SKIPPED", "CANCELLED")]
        all_complete = all(
            (c.get("status") or "").upper() == "COMPLETED" for c in checks_list
        ) if checks_list else True  # vacuously complete when no checks

        # Determine terminal verdict
        if not all_complete:
            verdict = "running"
        elif failed:
            verdict = "fail"
        else:
            verdict = "pass"

        now = utc_now_iso()

        # Compute contract hash: sha256({gate_name, head_sha, pr_number})[:16]
        head_sha = _get_pr_head_sha(pr_number)
        hash_input = json.dumps(
            {"gate_name": gate, "head_sha": head_sha, "pr_number": pr_number},
            sort_keys=True,
        )
        contract_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        blocking_findings = [
            {
                "severity": "blocking",
                "title": c.get("name", "unknown"),
                "description": f"CI check '{c.get('name', '?')}' concluded with {c.get('conclusion', 'FAILURE')}",
            }
            for c in failed
        ]
        advisory_findings = [
            {
                "severity": "advisory",
                "title": c.get("name", "unknown"),
                "description": f"CI check '{c.get('name', '?')}' was {c.get('conclusion', 'SKIPPED')}",
            }
            for c in skipped
        ]

        # Write report for terminal verdicts
        report_path = request_payload.get("report_path", "")
        report_written = False
        if report_path and verdict in ("pass", "fail"):
            report_content = _format_ci_gate_report(
                pr_number=pr_number,
                branch=request_payload.get("branch", ""),
                checks=checks_list,
                passed=passed,
                failed=failed,
                skipped=skipped,
                status=verdict,
                generated_at=now,
            )
            try:
                Path(report_path).parent.mkdir(parents=True, exist_ok=True)
                Path(report_path).write_text(report_content, encoding="utf-8")
                report_written = True
            except OSError:
                pass

        result_payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number is not None else ""),
            "pr_number": pr_number,
            "status": verdict,
            "summary": _ci_gate_summary(verdict, passed, failed, skipped),
            "passed_checks": [c.get("name", "") for c in passed],
            "failed_checks": [c.get("name", "") for c in failed],
            "blocking_findings": blocking_findings,
            "advisory_findings": advisory_findings,
            "blocking_count": len(failed),
            "advisory_count": len(skipped),
            "contract_hash": contract_hash if verdict in ("pass", "fail") else "",
            "report_path": report_path if (report_written and verdict in ("pass", "fail")) else "",
            "required_reruns": [],
            "residual_risk": (
                "" if verdict == "pass"
                else f"CI gate {verdict}: {len(failed)} failed check(s)"
            ),
            "duration_seconds": duration,
            "recorded_at": now,
        }

        rf = _rec.result_file_path(
            self.results_dir, gate, pr_number=pr_number, pr_id=pr_id,
        )
        if rf:
            rf.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

        request_payload["status"] = "completed"
        request_payload["completed_at"] = now
        _rec.persist_request(
            self.requests_dir, gate, request_payload,
            pr_number=pr_number, pr_id=pr_id,
        )

        return result_payload

    def _execute_requested_gates(
        self,
        request_result: Dict[str, Any],
        pr_number: int,
    ) -> tuple:
        """Execute all requested gates and classify results.

        Returns (gates_list, has_required_failure) tuple.
        """
        gates: List[Dict[str, Any]] = []
        has_required_failure = False

        for req in request_result.get("requested", []):
            gate_name = req.get("gate", "")
            req_status = req.get("status", "")

            if req_status == "requested":
                exec_result = self.execute_gate(
                    gate=gate_name,
                    pr_number=pr_number,
                )
                gates.append({
                    "gate": gate_name,
                    "request_status": req_status,
                    "execution_status": exec_result.get("status", "unknown"),
                    "report_path": exec_result.get("report_path", ""),
                    "contract_hash": exec_result.get("contract_hash", ""),
                    "detail": exec_result,
                })
            else:
                gates.append({
                    "gate": gate_name,
                    "request_status": req_status,
                    "execution_status": req_status,
                    "reason": req.get("reason", ""),
                    "reason_detail": req.get("reason_detail", ""),
                    "detail": req,
                })
                if req_status in ("not_executable", "not_configured"):
                    required = req.get("required", True)
                    if gate_name != "claude_github_optional" and required:
                        has_required_failure = True

        return gates, has_required_failure

    def request_and_execute(
        self,
        *,
        pr_number: int,
        branch: str,
        review_stack: Optional[Iterable[str]] = None,
        risk_class: str,
        changed_files: Iterable[str],
        mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """Request and immediately execute all gates atomically.

        Sets ``VNX_CODEX_HEADLESS_ENABLED=1`` in the process environment before
        checking availability so codex is never silently disabled during
        enforcement.
        """
        os.environ["VNX_CODEX_HEADLESS_ENABLED"] = "1"

        request_result = self.request_reviews(
            pr_number=pr_number,
            branch=branch,
            review_stack=review_stack,
            risk_class=risk_class,
            changed_files=changed_files,
            mode=mode,
            dispatch_id=dispatch_id,
        )

        gates, has_required_failure = self._execute_requested_gates(
            request_result, pr_number,
        )

        return {
            "pr_number": pr_number,
            "branch": branch,
            "gates": gates,
            "has_required_failure": has_required_failure,
        }

    def status(self, pr_number: int) -> Dict[str, Any]:
        results = []
        for path in sorted(self.results_dir.glob(f"pr-{pr_number}-*.json")):
            results.append(json.loads(path.read_text(encoding="utf-8")))
        requests = []
        for path in sorted(self.requests_dir.glob(f"pr-{pr_number}-*.json")):
            requests.append(json.loads(path.read_text(encoding="utf-8")))
        return {"pr_number": pr_number, "requests": requests, "results": results}


def _ci_gate_summary(
    verdict: str,
    passed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
) -> str:
    if verdict == "running":
        return "CI checks still running — verdict pending"
    if verdict == "fail":
        names = ", ".join(c.get("name", "?") for c in failed[:3])
        tail = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
        return f"CI gate FAIL: {len(failed)} failed check(s): {names}{tail}"
    if not passed and not failed and not skipped:
        return "CI gate PASS: no checks configured (vacuous pass)"
    return f"CI gate PASS: {len(passed)} check(s) passed, {len(skipped)} advisory"
