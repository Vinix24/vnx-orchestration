"""Gate report generation and audit trail (GateReportGeneratorMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle writing result records and NDJSON audit entries.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, Optional


class GateReportGeneratorMixin:
    """Mixin providing report writing and audit trail methods for ReviewGateManager."""

    def _write_not_executable_result(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        reason: str,
        reason_detail: str,
        contract_hash: str = "",
    ) -> Dict[str, Any]:
        """Write a not_executable result record (GATE-4)."""
        from review_gate_manager import _utc_now

        now = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "not_executable",
            "reason": reason,
            "reason_detail": reason_detail,
            "summary": f"{gate} not executable: {reason_detail}",
            "contract_hash": contract_hash,
            "report_path": "",
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [],
            "residual_risk": "Gate evidence not available. Compensating evidence required.",
            "recorded_at": now,
        }
        if pr_id:
            result_file = self._contract_result_path(gate, pr_id)
        elif pr_number is not None:
            result_file = self._result_path(gate, pr_number)
        else:
            return payload
        result_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _write_skip_rationale(
        self,
        *,
        gate: str,
        pr_id: str,
        reason: str,
        reason_detail: str,
        binary_name: str,
    ) -> None:
        """Append a skip-rationale record to the NDJSON audit trail (GATE-9)."""
        from review_gate_manager import _utc_now

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
                "binary_name": binary_name,
                "binary_found": shutil.which(binary_name) is not None,
                "env_flag": env_var,
                "env_value": os.environ.get(env_var, ""),
            },
            "compensating_action": "Manual review or operator override required.",
            "timestamp": _utc_now(),
        }
        audit_path = self.state_dir / "gate_execution_audit.ndjson"
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _write_failure_result(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        reason: str,
        reason_detail: str,
        duration_seconds: float,
        partial_output_lines: int,
        runner_pid: int,
        contract_hash: str = "",
    ) -> Dict[str, Any]:
        """Write a failed result record for timeout/stall (GATE-6/7)."""
        from review_gate_manager import _utc_now

        now = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "failed",
            "reason": reason,
            "reason_detail": reason_detail,
            "duration_seconds": duration_seconds,
            "partial_output_lines": partial_output_lines,
            "runner_pid": runner_pid,
            "killed_at": now,
            "summary": f"Gate execution {reason}: {reason_detail}",
            "contract_hash": contract_hash,
            "report_path": "",
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [gate],
            "residual_risk": f"Gate {reason}. Re-run required.",
            "recorded_at": now,
        }
        if pr_id:
            result_file = self._contract_result_path(gate, pr_id)
        elif pr_number is not None:
            result_file = self._result_path(gate, pr_number)
        else:
            return payload
        result_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
