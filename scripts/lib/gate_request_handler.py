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
from gemini_prompt_renderer import render_gemini_prompt
from claude_github_receipt import (
    ClaudeGitHubReviewReceipt,
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_DRY_RUN,
    STATE_REQUESTED,
    STATE_BLOCKED,
    STATE_COMPLETED,
)


def _get_head_commit_sha() -> str:
    """Return the HEAD commit SHA via git rev-parse. Returns empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


class GateRequestHandlerMixin:
    """Mixin providing gate request creation methods for ReviewGateManager."""

    def _gemini_available(self) -> bool:
        return os.environ.get("VNX_GEMINI_REVIEW_ENABLED", "1") != "0" and shutil.which("gemini") is not None

    def _codex_headless_available(self) -> bool:
        return os.environ.get("VNX_CODEX_HEADLESS_ENABLED", "1") != "0" and shutil.which("codex") is not None

    def _claude_github_configured(self) -> bool:
        return os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0") == "1" and shutil.which("gh") is not None

    def _ci_gate_available(self) -> bool:
        return os.environ.get("VNX_CI_GATE_REQUIRED", "0") == "1" and shutil.which("gh") is not None

    def _dispatch_one_review(
        self,
        gate: str,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
    ) -> Dict[str, Any]:
        if gate == "gemini_review":
            return self._request_gemini(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "codex_gate":
            return self._request_codex(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "claude_github_optional":
            return self._request_claude_github(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "ci_gate":
            return self._request_ci_gate(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        return {"gate": gate, "status": "blocked", "reason": "unknown_review_gate"}

    def request_reviews(
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
        from review_gate_manager import DEFAULT_REVIEW_STACK, _utc_now, emit_governance_receipt

        review_stack_list = [item.strip() for item in (review_stack or DEFAULT_REVIEW_STACK) if str(item).strip()]
        changed_files = [str(path).strip() for path in changed_files if str(path).strip()]
        requested: List[Dict[str, Any]] = []

        for gate in review_stack_list:
            payload = self._dispatch_one_review(gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id)
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
                dispatch_id=dispatch_id,
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
        dispatch_id: str = "",
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
            dispatch_id=dispatch_id,
        )
        self._write_skip_rationale(
            gate=gate, pr_id=pr_id or str(pr_number),
            reason=reason, reason_detail=detail,
            binary_name=binary_name,
        )

    def _request_gemini(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
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
            "commit_sha": _get_head_commit_sha(),
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review", binary_name="gemini",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("gemini_review", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _build_gemini_contract_payload(
        self,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        available: bool,
        requested_at: str,
        prompt: str,
    ) -> Dict[str, Any]:
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
            "commit_sha": _get_head_commit_sha(),
            "dispatch_id": dispatch_id,
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
                dispatch_id=dispatch_id,
            )
        return payload

    def request_gemini_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """Request a Gemini review driven by a canonical ReviewContract.

        Renders a deliverable-aware prompt from the contract and persists the
        request payload including the rendered prompt text and contract hash.

        Raises:
            gemini_prompt_renderer.MissingContractFieldError: when the contract is missing required fields.
        """
        from review_gate_manager import _utc_now, emit_governance_receipt

        prompt = render_gemini_prompt(contract)
        available = self._gemini_available()
        requested_at = _utc_now()
        payload = self._build_gemini_contract_payload(contract, mode, dispatch_id, available, requested_at, prompt)

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
            dispatch_id=dispatch_id,
        )
        return payload

    def _validate_pr_number_for_github(
        self,
        pr_number: Optional[int],
        contract_pr_id: str,
    ) -> Optional[tuple]:
        if pr_number is None:
            return (
                STATE_BLOCKED,
                "missing_github_pr_number",
                "gh pr comment requires a real GitHub PR number; "
                f"governance pr_id {contract_pr_id!r} is not a valid PR ref",
            )
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            return (
                STATE_BLOCKED,
                "invalid_github_pr_number",
                f"pr_number must be a positive int (got {pr_number!r}); "
                f"the governance pr_id {contract_pr_id!r} is not a valid PR ref",
            )
        return None

    def _trigger_github_comment(self, pr_number: int, comment_body: str) -> tuple:
        proc = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", comment_body],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return (STATE_REQUESTED, None, None)
        return (STATE_BLOCKED, "claude_github_trigger_failed", proc.stderr.strip())

    def _determine_claude_github_state(
        self,
        configured: bool,
        contract_pr_id: str,
        comment_body: str,
        pr_number: Optional[int] = None,
    ) -> tuple:
        """Determine Claude GitHub review state from environment configuration.

        ``pr_number`` is the real GitHub PR number used to target ``gh pr comment``.
        ``contract_pr_id`` is the governance ID (e.g. "PR-4") and is *not* a valid
        GitHub PR reference. If we attempted to trigger a comment without a real
        ``pr_number``, ``gh`` would either fail outright or — worse, with a numeric
        contract id — target the wrong PR. Treat that as BLOCKED rather than
        silently issuing a bogus call.

        ``pr_number`` must be a positive ``int``. Any other value (including
        strings that look numeric, ``0``, or negative values) is rejected as a
        misuse — passing ``contract.pr_id`` here would silently target the
        wrong PR.

        Returns (state, reason, stderr_detail) tuple.
        """
        if not configured:
            return (STATE_NOT_CONFIGURED, "claude_github_not_configured", None)
        if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") != "1":
            return (STATE_CONFIGURED_DRY_RUN, None, None)
        invalid = self._validate_pr_number_for_github(pr_number, contract_pr_id)
        if invalid is not None:
            return invalid
        return self._trigger_github_comment(pr_number, comment_body)

    def _build_claude_github_payload(
        self,
        receipt: ClaudeGitHubReviewReceipt,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        requested_at: str,
        stderr_detail: Optional[str],
    ) -> Dict[str, Any]:
        payload = receipt.to_dict()
        if stderr_detail:
            payload["stderr"] = stderr_detail
        payload["review_mode"] = mode
        payload["risk_class"] = contract.risk_class
        payload["changed_files"] = contract.changed_files
        payload["commit_sha"] = _get_head_commit_sha()
        payload["dispatch_id"] = dispatch_id
        payload["report_path"] = self._build_report_path(
            gate="claude_github_optional",
            requested_at=requested_at,
            pr_id=contract.pr_id,
        )
        return payload

    def _persist_claude_github_files(
        self,
        payload: Dict[str, Any],
        contract: ReviewContract,
        requested_at: str,
    ) -> None:
        request_file = self._contract_request_path("claude_github_optional", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        # Persist the explicit state as a result record so closure_verifier can
        # observe optional-gate state via review_gates/results/ — without this
        # mirror, no_op / dry_run / requested / blocked configurations are
        # invisible to the verifier and break closure for normal optional-gate
        # paths.
        result_file = self._contract_result_path("claude_github_optional", contract.pr_id)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_payload = dict(payload)
        result_payload["gate"] = "claude_github_optional"
        result_payload["recorded_at"] = requested_at
        result_file.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

    def _build_claude_github_receipt(
        self,
        contract: ReviewContract,
        state: str,
        reason: Optional[str],
        requested_at: str,
        pr_number: Optional[int],
        comment_body: str,
    ) -> ClaudeGitHubReviewReceipt:
        return ClaudeGitHubReviewReceipt(
            pr_id=contract.pr_id,
            state=state,
            contract_hash=contract.content_hash,
            branch=contract.branch,
            pr_number=pr_number,
            gh_comment_body=comment_body if state == STATE_REQUESTED else "",
            reason=reason,
            requested_at=requested_at,
        )

    def _emit_claude_github_request_receipt(
        self,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        state: str,
        receipt: ClaudeGitHubReviewReceipt,
    ) -> None:
        from review_gate_manager import emit_governance_receipt

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
            dispatch_id=dispatch_id,
        )

    def request_claude_github_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
        dispatch_id: str = "",
        pr_number: Optional[int] = None,
    ) -> ClaudeGitHubReviewReceipt:
        """Request a Claude GitHub review driven by a canonical ReviewContract.

        Determines the explicit review state from environment configuration and
        persists the request payload linked to the contract hash.

        ``pr_number`` is the real GitHub PR number; required only when the
        environment opts in to actually triggering ``gh pr comment``. The
        closure verifier requires the resulting state to be visible in the
        review_gates ``results/`` directory regardless of the state value, so
        the state is always materialised as a result record (not just a
        request) to keep the optional-gate evidence loop closed.
        """
        from review_gate_manager import _utc_now

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        comment_body = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")

        state, reason, stderr_detail = self._determine_claude_github_state(
            configured, contract.pr_id, comment_body, pr_number=pr_number,
        )

        receipt = self._build_claude_github_receipt(contract, state, reason, requested_at, pr_number, comment_body)
        payload = self._build_claude_github_payload(receipt, contract, mode, dispatch_id, requested_at, stderr_detail)
        self._persist_claude_github_files(payload, contract, requested_at)
        self._emit_claude_github_request_receipt(contract, mode, dispatch_id, state, receipt)
        return receipt

    def _request_codex(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
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
            "commit_sha": _get_head_commit_sha(),
            "report_path": self._build_report_path(
                gate="codex_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="codex_gate", binary_name="codex",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("codex_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _apply_claude_github_configured_state(
        self,
        payload: Dict[str, Any],
        pr_number: int,
    ) -> None:
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

    def _request_claude_github(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
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
            "commit_sha": _get_head_commit_sha(),
            "report_path": self._build_report_path(
                gate="claude_github_optional",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if configured:
            self._apply_claude_github_configured_state(payload, pr_number)
        self._request_path("claude_github_optional", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_ci_gate(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._ci_gate_available()
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "ci_gate",
            "status": "requested" if available else "not_executable",
            "provider": "gh_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": _get_head_commit_sha(),
            "report_path": self._build_report_path(
                gate="ci_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="ci_gate", binary_name="gh",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("ci_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _build_ci_gate_contract_payload(
        self,
        contract: "ReviewContract",
        pr_number: int,
        mode: str,
        dispatch_id: str,
        available: bool,
        requested_at: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "gate": "ci_gate",
            "status": "requested" if available else "not_executable",
            "provider": "gh_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "requested_at": requested_at,
            "commit_sha": _get_head_commit_sha(),
            "dispatch_id": dispatch_id,
            "report_path": self._build_report_path(
                gate="ci_gate",
                requested_at=requested_at,
                pr_id=contract.pr_id,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="ci_gate", binary_name="gh",
                pr_number=pr_number, pr_id=contract.pr_id,
                contract_hash=contract.content_hash,
                dispatch_id=dispatch_id,
            )
        return payload

    def _emit_ci_gate_contract_receipt(
        self,
        contract: "ReviewContract",
        mode: str,
        dispatch_id: str,
        status: str,
    ) -> None:
        from review_gate_manager import emit_governance_receipt

        emit_governance_receipt(
            "review_gate_request",
            status=status,
            terminal="T0",
            pr_id=contract.pr_id,
            branch=contract.branch,
            gate="ci_gate",
            review_mode=mode,
            risk_class=contract.risk_class,
            contract_hash=contract.content_hash,
            changed_files=contract.changed_files,
            dispatch_id=dispatch_id,
        )

    def request_ci_gate_with_contract(
        self,
        *,
        contract: "ReviewContract",
        pr_number: int,
        mode: str = "per_pr",
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """Request a ci_gate execution driven by a canonical ReviewContract.

        Writes a contract-scoped request file ({pr_slug}-ci_gate-contract.json)
        with the canonical pr_id and the contract's content_hash.  This enables
        closure_verifier._find_gate_result to locate the result via the contract
        path and ensures the result's contract_hash matches ReviewContract.content_hash.

        ``pr_number`` is the real GitHub PR number used by ``gh pr checks``.
        """
        from review_gate_manager import _utc_now

        if not contract.pr_id:
            raise ValueError("contract.pr_id is required for ci_gate contract request")

        available = self._ci_gate_available()
        requested_at = _utc_now()
        payload = self._build_ci_gate_contract_payload(contract, pr_number, mode, dispatch_id, available, requested_at)

        request_file = self._contract_request_path("ci_gate", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        self._emit_ci_gate_contract_receipt(contract, mode, dispatch_id, payload["status"])
        return payload
