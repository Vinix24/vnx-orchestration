"""Gate request creation and orchestration (GateRequestHandlerMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle creating gate request payloads for Gemini, Codex, and Claude GitHub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from auto_merge_policy import codex_final_gate_required
from review_contract import ReviewContract
from gemini_prompt_renderer import (
    MissingContractFieldError,
    render_gemini_prompt,
)
from claude_github_receipt import (
    ClaudeGitHubReviewReceipt,
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_DRY_RUN,
    STATE_REQUESTED,
    STATE_BLOCKED,
    STATE_COMPLETED,
)


class GateRequestHandlerMixin:
    """Mixin providing gate request creation methods for ReviewGateManager."""

    def _gemini_available(self) -> bool:
        return os.environ.get("VNX_GEMINI_REVIEW_ENABLED", "1") != "0" and shutil.which("gemini") is not None

    def _codex_headless_available(self) -> bool:
        return os.environ.get("VNX_CODEX_HEADLESS_ENABLED", "1") != "0" and shutil.which("codex") is not None

    def _claude_github_configured(self) -> bool:
        return os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0") == "1" and shutil.which("gh") is not None

    def request_reviews(
        self,
        *,
        pr_number: int,
        branch: str,
        review_stack: Optional[Iterable[str]] = None,
        risk_class: str,
        changed_files: Iterable[str],
        mode: str,
    ) -> Dict[str, Any]:
        from review_gate_manager import DEFAULT_REVIEW_STACK, _utc_now, emit_governance_receipt

        review_stack_list = [item.strip() for item in (review_stack or DEFAULT_REVIEW_STACK) if str(item).strip()]
        changed_files = [str(path).strip() for path in changed_files if str(path).strip()]
        requested: List[Dict[str, Any]] = []

        for gate in review_stack_list:
            if gate == "gemini_review":
                payload = self._request_gemini(pr_number, branch, risk_class, changed_files, mode)
            elif gate == "codex_gate":
                payload = self._request_codex(pr_number, branch, risk_class, changed_files, mode)
            elif gate == "claude_github_optional":
                payload = self._request_claude_github(pr_number, branch, risk_class, changed_files, mode)
            else:
                payload = {
                    "gate": gate,
                    "status": "blocked",
                    "reason": "unknown_review_gate",
                }

            requested.append(payload)
            emit_governance_receipt(
                "review_gate_request",
                status=payload["status"],
                terminal="T0",
                pr_number=pr_number,
                branch=branch,
                gate=payload["gate"],
                review_mode=mode,
                risk_class=risk_class,
                changed_files=changed_files,
                request=payload,
            )

        return {
            "pr_number": pr_number,
            "branch": branch,
            "requested": requested,
        }

    def _mark_gate_unavailable(
        self,
        payload: Dict[str, Any],
        *,
        gate: str,
        binary_name: str,
        pr_number: Optional[int],
        pr_id: str,
        contract_hash: str = "",
    ) -> None:
        """Record unavailability in payload and write skip/result records."""
        reason, detail = self._classify_unavailable(gate, binary_name)
        payload["reason"] = reason
        payload["reason_detail"] = detail
        payload["resolved_at"] = payload["requested_at"]
        self._write_not_executable_result(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            reason=reason, reason_detail=detail,
            contract_hash=contract_hash,
        )
        self._write_skip_rationale(
            gate=gate, pr_id=pr_id or str(pr_number),
            reason=reason, reason_detail=detail,
            binary_name=binary_name,
        )

    def _request_gemini(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._gemini_available()
        requested_at = _utc_now()
        payload = {
            "gate": "gemini_review",
            "status": "requested" if available else "not_executable",
            "provider": "gemini_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review", binary_name="gemini",
                pr_number=pr_number, pr_id="",
            )
        self._request_path("gemini_review", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def request_gemini_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
    ) -> Dict[str, Any]:
        """Request a Gemini review driven by a canonical ReviewContract.

        Renders a deliverable-aware prompt from the contract and persists the
        request payload including the rendered prompt text and contract hash.

        Raises:
            MissingContractFieldError: when the contract is missing required fields.
        """
        from review_gate_manager import _utc_now, emit_governance_receipt

        prompt = render_gemini_prompt(contract)
        available = self._gemini_available()
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "gemini_review",
            "status": "requested" if available else "not_executable",
            "provider": "gemini_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": None,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "prompt": prompt,
            "requested_at": requested_at,
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_id=contract.pr_id,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review", binary_name="gemini",
                pr_number=None, pr_id=contract.pr_id,
                contract_hash=contract.content_hash,
            )

        request_file = self._contract_request_path("gemini_review", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        emit_governance_receipt(
            "review_gate_request",
            status=payload["status"],
            terminal="T0",
            pr_id=contract.pr_id,
            branch=contract.branch,
            gate="gemini_review",
            review_mode=mode,
            risk_class=contract.risk_class,
            contract_hash=contract.content_hash,
            changed_files=contract.changed_files,
        )
        return payload

    def _determine_claude_github_state(
        self, configured: bool, contract_pr_id: str, comment_body: str
    ) -> tuple:
        """Determine Claude GitHub review state from environment configuration.

        Returns (state, reason, stderr_detail) tuple.
        """
        if not configured:
            return (STATE_NOT_CONFIGURED, "claude_github_not_configured", None)

        if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") != "1":
            return (STATE_CONFIGURED_DRY_RUN, None, None)

        proc = subprocess.run(
            ["gh", "pr", "comment", str(contract_pr_id), "--body", comment_body],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return (STATE_REQUESTED, None, None)
        return (STATE_BLOCKED, "claude_github_trigger_failed", proc.stderr.strip())

    def request_claude_github_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
    ) -> ClaudeGitHubReviewReceipt:
        """Request a Claude GitHub review driven by a canonical ReviewContract.

        Determines the explicit review state from environment configuration and
        persists the request payload linked to the contract hash.
        """
        from review_gate_manager import _utc_now, emit_governance_receipt

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        comment_body = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")

        state, reason, stderr_detail = self._determine_claude_github_state(
            configured, contract.pr_id, comment_body,
        )

        receipt = ClaudeGitHubReviewReceipt(
            pr_id=contract.pr_id,
            state=state,
            contract_hash=contract.content_hash,
            branch=contract.branch,
            pr_number=None,
            gh_comment_body=comment_body if state == STATE_REQUESTED else "",
            reason=reason,
            requested_at=requested_at,
        )

        payload = receipt.to_dict()
        if stderr_detail:
            payload["stderr"] = stderr_detail
        payload["review_mode"] = mode
        payload["risk_class"] = contract.risk_class
        payload["changed_files"] = contract.changed_files
        payload["report_path"] = self._build_report_path(
            gate="claude_github_optional",
            requested_at=requested_at,
            pr_id=contract.pr_id,
        )

        request_file = self._contract_request_path("claude_github_optional", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        emit_governance_receipt(
            "review_gate_request",
            status=state,
            terminal="T0",
            pr_id=contract.pr_id,
            branch=contract.branch,
            gate="claude_github_optional",
            review_mode=mode,
            risk_class=contract.risk_class,
            contract_hash=contract.content_hash,
            changed_files=contract.changed_files,
            contributed_evidence=receipt.contributed_evidence(),
            was_intentionally_absent=receipt.was_intentionally_absent(),
        )
        return receipt

    def _request_codex(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        required = mode == "final" or codex_final_gate_required(changed_files)
        available = self._codex_headless_available()
        # Model from env only; empty string means "use codex config.toml default".
        # See gate_runner._build_gate_cmd and ~/.codex/config.toml for defaults.
        model = os.environ.get("VNX_CODEX_HEADLESS_MODEL") or os.environ.get("VNX_CODEX_MODEL") or ""
        requested_at = _utc_now()
        payload = {
            "gate": "codex_gate",
            "status": "requested" if available else "not_executable",
            "provider": "codex_headless",
            "model": model,
            "required": required,
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "report_path": self._build_report_path(
                gate="codex_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="codex_gate", binary_name="codex",
                pr_number=pr_number, pr_id="",
            )
        self._request_path("codex_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_claude_github(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        payload = {
            "gate": "claude_github_optional",
            "status": "not_configured",
            "provider": "claude_github",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "report_path": self._build_report_path(
                gate="claude_github_optional",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if configured:
            payload["status"] = "queued"
            if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") == "1":
                comment = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")
                proc = subprocess.run(
                    ["gh", "pr", "comment", str(pr_number), "--body", comment],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    payload["status"] = "requested"
                else:
                    payload["status"] = "blocked"
                    payload["reason"] = "claude_github_trigger_failed"
                    payload["stderr"] = proc.stderr.strip()
            else:
                payload["status"] = "configured_dry_run"
        self._request_path("claude_github_optional", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
