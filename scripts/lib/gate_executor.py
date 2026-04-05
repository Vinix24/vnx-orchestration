"""Gate execution orchestration (GateExecutorMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle gate execution, contract-driven execution, and status queries.
Request creation methods are in gate_request_handler.py.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional


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
