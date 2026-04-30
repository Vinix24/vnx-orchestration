"""Gate result recording and finding classification (GateResultParserMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle finding classification (advisory/blocking) and result persistence.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from gemini_prompt_renderer import GeminiReviewReceipt
from claude_github_receipt import ClaudeGitHubReviewReceipt


class GateResultParserMixin:
    """Mixin providing result recording and finding classification for ReviewGateManager."""

    def record_claude_github_result(
        self,
        *,
        pr_id: str,
        branch: str,
        status: str,
        summary: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        contract_hash: str = "",
        completed_at: str = "",
        pr_number: Optional[int] = None,
        report_path: str = "",
        required_reruns: Optional[List[str]] = None,
    ) -> ClaudeGitHubReviewReceipt:
        """Record a Claude GitHub review result linked to a ReviewContract.

        Classifies findings into advisory/blocking and persists the result
        with the contract_hash so T0 can correlate with the original contract.

        Per the headless review evidence contract, ``report_path`` and
        ``required_reruns`` are persisted so the closure verifier can validate
        the full evidence chain.
        """
        from review_gate_manager import _utc_now, emit_governance_receipt

        raw_findings = findings or []
        request_payload = self._load_contract_request_payload("claude_github_optional", pr_id)
        effective_contract_hash = contract_hash or str(request_payload.get("contract_hash", ""))
        effective_report_path = report_path or str(request_payload.get("report_path", ""))
        result_payload: Dict[str, Any] = {
            "gate": "claude_github_optional",
            "pr_id": pr_id,
            "pr_number": pr_number,
            "branch": branch,
            "status": status,
            "summary": summary,
            "findings": raw_findings,
            "contract_hash": effective_contract_hash,
            "requested_at": "",
            "completed_at": completed_at or _utc_now(),
        }
        receipt = ClaudeGitHubReviewReceipt.from_result_payload(result_payload)
        canonical_report_path = self._canonical_report_path(effective_report_path)
        if status in {"pass", "fail"}:
            if not effective_contract_hash:
                raise ValueError("contract_hash is required for pass/fail gate results")
            if not canonical_report_path:
                raise ValueError("report_path is required for pass/fail gate results")

        full_payload = receipt.to_dict()
        full_payload["findings"] = raw_findings
        full_payload["report_path"] = canonical_report_path
        full_payload["required_reruns"] = list(required_reruns or [])
        full_payload["residual_risk"] = full_payload.get("residual_risk", "")

        result_file = self._contract_result_path("claude_github_optional", pr_id)
        result_file.write_text(json.dumps(full_payload, indent=2), encoding="utf-8")

        self._emit_claude_github_receipt(
            receipt, status=status, pr_id=pr_id, branch=branch,
            summary=summary, contract_hash=effective_contract_hash,
        )
        return receipt

    def _emit_claude_github_receipt(
        self, receipt: "ClaudeGitHubReviewReceipt", *,
        status: str, pr_id: str, branch: str, summary: str, contract_hash: str,
    ) -> None:
        """Emit governance receipt for a Claude GitHub review result."""
        from review_gate_manager import emit_governance_receipt
        emit_governance_receipt(
            "review_gate_result",
            status=status, terminal="T0", pr_id=pr_id, branch=branch,
            gate="claude_github_optional", summary=summary,
            advisory_findings=[f.to_dict() for f in receipt.advisory_findings],
            blocking_findings=[f.to_dict() for f in receipt.blocking_findings],
            advisory_count=receipt.advisory_count,
            blocking_count=receipt.blocking_count,
            contract_hash=contract_hash,
            contributed_evidence=receipt.contributed_evidence(),
        )

    def record_result(
        self,
        *,
        gate: str,
        pr_number: int,
        branch: str,
        status: str,
        summary: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        residual_risk: Optional[str] = None,
        contract_hash: str = "",
        pr_id: str = "",
        report_path: str = "",
        required_reruns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record a review gate result with explicit advisory/blocking finding classification.

        Findings are classified by their ``severity`` field:
        - ``"blocking"`` or ``"error"`` severity -> blocking_findings
        - all other values -> advisory_findings

        Both lists are always present in the payload so downstream consumers can
        act on the classification without re-parsing the raw findings list.

        Per the headless review evidence contract, ``report_path`` and
        ``required_reruns`` are persisted in every gate result so the closure
        verifier can validate the full evidence chain.
        """
        from review_gate_manager import _utc_now, emit_governance_receipt

        raw_findings = findings or []
        request_payload = self._load_request_payload(gate, pr_number)
        effective_contract_hash = contract_hash or str(request_payload.get("contract_hash", ""))
        effective_report_path = report_path or str(request_payload.get("report_path", ""))
        receipt = GeminiReviewReceipt.from_raw_findings(
            pr_id=pr_id or str(pr_number),
            raw_findings=raw_findings,
            contract_hash=effective_contract_hash,
            reviewed_at=_utc_now(),
        )

        payload = self._build_result_payload(
            gate=gate, pr_number=pr_number, pr_id=pr_id, branch=branch,
            status=status, summary=summary, raw_findings=raw_findings,
            receipt=receipt, residual_risk=residual_risk or "",
            effective_contract_hash=effective_contract_hash,
            effective_report_path=effective_report_path,
            required_reruns=required_reruns,
        )
        self._validate_and_persist_result(payload, status, gate, pr_number)
        self._emit_result_receipt(
            payload, receipt, status=status, pr_number=pr_number,
            pr_id=pr_id or str(pr_number), branch=branch, gate=gate, summary=summary,
        )
        return payload

    def _build_result_payload(
        self, *, gate: str, pr_number: int, pr_id: str, branch: str,
        status: str, summary: str, raw_findings: List[Dict[str, Any]],
        receipt: "GeminiReviewReceipt", residual_risk: str,
        effective_contract_hash: str, effective_report_path: str,
        required_reruns: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Build the result payload dict from receipt and parameters."""
        payload: Dict[str, Any] = {
            "gate": gate, "pr_number": pr_number,
            "pr_id": pr_id or str(pr_number), "branch": branch,
            "status": status, "summary": summary, "findings": raw_findings,
            "advisory_findings": [f.to_dict() for f in receipt.advisory_findings],
            "blocking_findings": [f.to_dict() for f in receipt.blocking_findings],
            "advisory_count": receipt.advisory_count,
            "blocking_count": receipt.blocking_count,
            "residual_risk": residual_risk,
            "contract_hash": effective_contract_hash,
            "report_path": effective_report_path,
            "required_reruns": list(required_reruns or []),
            "recorded_at": receipt.reviewed_at,
        }
        payload["report_path"] = self._canonical_report_path(payload["report_path"])
        return payload

    def _validate_and_persist_result(
        self, payload: Dict[str, Any], status: str, gate: str, pr_number: int,
    ) -> None:
        """Validate pass/fail constraints and write result file to disk."""
        if status in {"pass", "fail"}:
            if not payload["contract_hash"]:
                raise ValueError("contract_hash is required for pass/fail gate results")
            if not payload["report_path"]:
                raise ValueError("report_path is required for pass/fail gate results")
            report_file = Path(payload["report_path"])
            if not report_file.exists():
                raise ValueError(f"report_path file does not exist: {payload['report_path']}")
        self._result_path(gate, pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _emit_result_receipt(
        self, payload: Dict[str, Any], receipt: "GeminiReviewReceipt", *,
        status: str, pr_number: int, pr_id: str, branch: str, gate: str, summary: str,
    ) -> None:
        """Emit governance receipt for the recorded result."""
        from review_gate_manager import emit_governance_receipt
        emit_governance_receipt(
            "review_gate_result",
            status=status, terminal="T0", pr_number=pr_number,
            pr_id=pr_id, branch=branch, gate=gate, summary=summary,
            advisory_findings=payload["advisory_findings"],
            blocking_findings=payload["blocking_findings"],
            advisory_count=receipt.advisory_count,
            blocking_count=receipt.blocking_count,
            residual_risk=payload["residual_risk"],
            contract_hash=payload["contract_hash"],
            report_path=payload["report_path"],
            required_reruns=payload["required_reruns"],
        )

    def _classify_unavailable(self, gate: str, binary_name: str) -> tuple:
        """Return (reason_code, reason_detail) for an unavailable gate provider (GATE-4)."""
        env_flags = {
            "gemini_review": ("VNX_GEMINI_REVIEW_ENABLED", "1"),
            "codex_gate": ("VNX_CODEX_HEADLESS_ENABLED", "1"),
            "claude_github_optional": ("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0"),
            "ci_gate": ("VNX_CI_GATE_REQUIRED", "0"),
        }
        env_var, default = env_flags.get(gate, ("", "0"))
        disabled = env_var and os.environ.get(env_var, default) != "1" if gate == "ci_gate" else env_var and os.environ.get(env_var, default) == "0"
        binary_found = shutil.which(binary_name) is not None

        if disabled and not binary_found:
            return ("provider_disabled", f"{binary_name} binary not found in PATH and {env_var}=0")
        if disabled:
            return ("provider_disabled", f"{env_var} is set to 0")
        if not binary_found:
            return ("provider_not_installed", f"{binary_name} binary not found in PATH")
        return ("provider_not_configured", f"{gate} provider is not configured")
