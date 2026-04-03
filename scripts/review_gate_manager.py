#!/usr/bin/env python3
"""Review gate orchestration for Gemini, Codex, and optional Claude GitHub review."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt, utc_now_iso
from auto_merge_policy import codex_final_gate_required
from review_contract import ReviewContract
from headless_adapter import gate_timeout, gate_stall_threshold
from gemini_prompt_renderer import (
    GeminiReviewReceipt,
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


DEFAULT_REVIEW_STACK = ["gemini_review", "codex_gate", "claude_github_optional"]


def _utc_now() -> str:
    from governance_receipts import utc_now_iso
    return utc_now_iso()


class ReviewGateManager:
    def __init__(self) -> None:
        self.paths = ensure_env()
        self.state_dir = Path(self.paths["VNX_STATE_DIR"])
        self.reports_dir = Path(self.paths["VNX_REPORTS_DIR"])
        self.requests_dir = self.state_dir / "review_gates" / "requests"
        self.results_dir = self.state_dir / "review_gates" / "results"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _canonical_report_path(self, report_path: str) -> str:
        if not report_path:
            return ""

        path = Path(report_path)
        if path.is_absolute():
            return str(path)

        if path.parts and path.parts[0] == ".vnx-data":
            data_root = Path(self.paths["VNX_DATA_DIR"]).resolve().parent
            return str((data_root / path).resolve())

        project_root = Path(self.paths["PROJECT_ROOT"])
        return str((project_root / path).resolve())

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.results_dir / f"pr-{pr_number}-{gate}.json"

    def _contract_slug(self, pr_id: str) -> str:
        return pr_id.lower().replace("-", "")

    def _contract_request_path(self, gate: str, pr_id: str) -> Path:
        return self.requests_dir / f"{self._contract_slug(pr_id)}-{gate}-contract.json"

    def _contract_result_path(self, gate: str, pr_id: str) -> Path:
        return self.results_dir / f"{self._contract_slug(pr_id)}-{gate}-contract.json"

    def _report_timestamp_slug(self, value: str) -> str:
        digits = re.sub(r"[^0-9]", "", value or "")
        if len(digits) >= 14:
            return f"{digits[:8]}-{digits[8:14]}"
        return digits or "undated"

    def _report_pr_slug(self, *, pr_number: Optional[int] = None, pr_id: str = "") -> str:
        if pr_id:
            return pr_id.lower().replace("_", "-")
        if pr_number is None:
            raise ValueError("pr_number or pr_id is required to build report path")
        return f"pr-{pr_number}"

    def _build_report_path(
        self,
        *,
        gate: str,
        requested_at: str,
        pr_number: Optional[int] = None,
        pr_id: str = "",
    ) -> str:
        ts = self._report_timestamp_slug(requested_at)
        pr_slug = self._report_pr_slug(pr_number=pr_number, pr_id=pr_id)
        filename = f"{ts}-HEADLESS-{gate}-{pr_slug}.md"
        return str((self.reports_dir / filename).resolve())

    def _load_request_payload(self, gate: str, pr_number: int) -> Dict[str, Any]:
        path = self._request_path(gate, pr_number)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_contract_request_payload(self, gate: str, pr_id: str) -> Dict[str, Any]:
        path = self._contract_request_path(gate, pr_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

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

    def _request_gemini(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
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
            reason, detail = self._classify_unavailable("gemini_review", "gemini")
            payload["reason"] = reason
            payload["reason_detail"] = detail
            payload["resolved_at"] = requested_at
            self._write_not_executable_result(
                gate="gemini_review", pr_number=pr_number, pr_id="",
                reason=reason, reason_detail=detail,
            )
            self._write_skip_rationale(
                gate="gemini_review", pr_id=str(pr_number),
                reason=reason, reason_detail=detail,
                binary_name="gemini",
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
            reason, detail = self._classify_unavailable("gemini_review", "gemini")
            payload["reason"] = reason
            payload["reason_detail"] = detail
            payload["resolved_at"] = requested_at
            self._write_not_executable_result(
                gate="gemini_review", pr_number=None, pr_id=contract.pr_id,
                reason=reason, reason_detail=detail,
                contract_hash=contract.content_hash,
            )
            self._write_skip_rationale(
                gate="gemini_review", pr_id=contract.pr_id,
                reason=reason, reason_detail=detail,
                binary_name="gemini",
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

    def request_claude_github_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
    ) -> ClaudeGitHubReviewReceipt:
        """Request a Claude GitHub review driven by a canonical ReviewContract.

        Determines the explicit review state from environment configuration and
        persists the request payload linked to the contract hash. The returned
        receipt makes the state auditable so T0 can see whether the GitHub
        review contributed evidence or was intentionally absent.

        State semantics:
          - ``not_configured``     — gh CLI missing or env var not set
          - ``configured_dry_run`` — env configured; trigger env var not set
          - ``requested``          — gh pr comment successfully posted
          - ``blocked``            — trigger attempted but gh CLI call failed

        The receipt is linked to the ReviewContract via ``contract_hash``.
        """
        configured = self._claude_github_configured()
        requested_at = _utc_now()
        comment_body = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")

        if not configured:
            state = STATE_NOT_CONFIGURED
            reason: Optional[str] = "claude_github_not_configured"
            stderr_detail: Optional[str] = None
        elif os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") != "1":
            state = STATE_CONFIGURED_DRY_RUN
            reason = None
            stderr_detail = None
        else:
            proc = subprocess.run(
                ["gh", "pr", "comment", str(contract.pr_id), "--body", comment_body],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                state = STATE_REQUESTED
                reason = None
                stderr_detail = None
            else:
                state = STATE_BLOCKED
                reason = "claude_github_trigger_failed"
                stderr_detail = proc.stderr.strip()

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

        emit_governance_receipt(
            "review_gate_result",
            status=status,
            terminal="T0",
            pr_id=pr_id,
            branch=branch,
            gate="claude_github_optional",
            summary=summary,
            advisory_findings=[f.to_dict() for f in receipt.advisory_findings],
            blocking_findings=[f.to_dict() for f in receipt.blocking_findings],
            advisory_count=receipt.advisory_count,
            blocking_count=receipt.blocking_count,
            contract_hash=contract_hash,
            contributed_evidence=receipt.contributed_evidence(),
        )
        return receipt

    def _request_codex(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
        required = mode == "final" or codex_final_gate_required(changed_files)
        available = self._codex_headless_available()
        model = os.environ.get("VNX_CODEX_HEADLESS_MODEL") or os.environ.get("VNX_CODEX_MODEL") or "gpt-5.2-codex"
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
            reason, detail = self._classify_unavailable("codex_gate", "codex")
            payload["reason"] = reason
            payload["reason_detail"] = detail
            payload["resolved_at"] = requested_at
            self._write_not_executable_result(
                gate="codex_gate", pr_number=pr_number, pr_id="",
                reason=reason, reason_detail=detail,
            )
            self._write_skip_rationale(
                gate="codex_gate", pr_id=str(pr_number),
                reason=reason, reason_detail=detail,
                binary_name="codex",
            )
        self._request_path("codex_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_claude_github(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
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
        - ``"blocking"`` or ``"error"`` severity → blocking_findings
        - all other values → advisory_findings

        Both lists are always present in the payload so downstream consumers can
        act on the classification without re-parsing the raw findings list.

        Per the headless review evidence contract, ``report_path`` and
        ``required_reruns`` are persisted in every gate result so the closure
        verifier can validate the full evidence chain.
        """
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

        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_number": pr_number,
            "pr_id": pr_id or str(pr_number),
            "branch": branch,
            "status": status,
            "summary": summary,
            "findings": raw_findings,
            "advisory_findings": [f.to_dict() for f in receipt.advisory_findings],
            "blocking_findings": [f.to_dict() for f in receipt.blocking_findings],
            "advisory_count": receipt.advisory_count,
            "blocking_count": receipt.blocking_count,
            "residual_risk": residual_risk or "",
            "contract_hash": effective_contract_hash,
            "report_path": effective_report_path,
            "required_reruns": list(required_reruns or []),
            "recorded_at": receipt.reviewed_at,
        }
        payload["report_path"] = self._canonical_report_path(payload["report_path"])
        if status in {"pass", "fail"}:
            if not payload["contract_hash"]:
                raise ValueError("contract_hash is required for pass/fail gate results")
            if not payload["report_path"]:
                raise ValueError("report_path is required for pass/fail gate results")
            report_file = Path(payload["report_path"])
            if not report_file.exists():
                raise ValueError(f"report_path file does not exist: {payload['report_path']}")
        self._result_path(gate, pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        emit_governance_receipt(
            "review_gate_result",
            status=status,
            terminal="T0",
            pr_number=pr_number,
            pr_id=pr_id or str(pr_number),
            branch=branch,
            gate=gate,
            summary=summary,
            advisory_findings=payload["advisory_findings"],
            blocking_findings=payload["blocking_findings"],
            advisory_count=receipt.advisory_count,
            blocking_count=receipt.blocking_count,
            residual_risk=payload["residual_risk"],
            contract_hash=payload["contract_hash"],
            report_path=payload["report_path"],
            required_reruns=payload["required_reruns"],
        )
        return payload

    # ------------------------------------------------------------------
    # Gate execution helpers (GATE-1 through GATE-12)
    # ------------------------------------------------------------------

    def _classify_unavailable(self, gate: str, binary_name: str) -> tuple:
        """Return (reason_code, reason_detail) for an unavailable gate provider (GATE-4)."""
        env_flags = {
            "gemini_review": ("VNX_GEMINI_REVIEW_ENABLED", "1"),
            "codex_gate": ("VNX_CODEX_HEADLESS_ENABLED", "1"),
            "claude_github_optional": ("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0"),
        }
        env_var, default = env_flags.get(gate, ("", "0"))
        disabled = env_var and os.environ.get(env_var, default) == "0"
        binary_found = shutil.which(binary_name) is not None

        if disabled and not binary_found:
            return ("provider_disabled", f"{binary_name} binary not found in PATH and {env_var}=0")
        if disabled:
            return ("provider_disabled", f"{env_var} is set to 0")
        if not binary_found:
            return ("provider_not_installed", f"{binary_name} binary not found in PATH")
        return ("provider_not_configured", f"{gate} provider is not configured")

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

    def execute_gate(
        self,
        *,
        gate: str,
        pr_number: Optional[int] = None,
        pr_id: str = "",
    ) -> Dict[str, Any]:
        """Execute a gate: transition requested→executing→completed|failed (GATE-1).

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

        Ensures gates cannot be requested without execution — a single call
        does both so that T0 enforcement never leaves a gate in ``requested``
        state without a subsequent execution attempt.

        Sets ``VNX_CODEX_HEADLESS_ENABLED=1`` in the process environment before
        checking availability so codex is never silently disabled during
        enforcement.

        Returns a dict with ``pr_number``, ``branch``, and ``gates`` list where
        each gate entry contains its final status after request + execution.
        Exits with code 1 (via the CLI layer) if any required gate ends in
        ``not_executable`` or ``not_configured``.
        """
        # Ensure codex is never silently disabled during enforcement
        os.environ["VNX_CODEX_HEADLESS_ENABLED"] = "1"

        request_result = self.request_reviews(
            pr_number=pr_number,
            branch=branch,
            review_stack=review_stack,
            risk_class=risk_class,
            changed_files=changed_files,
            mode=mode,
        )

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


def _parse_changed_files(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX review gate manager")
    sub = parser.add_subparsers(dest="command", required=True)

    request_parser = sub.add_parser("request")
    request_parser.add_argument("--pr", type=int, required=True)
    request_parser.add_argument("--branch", required=True)
    request_parser.add_argument("--review-stack", default=",".join(DEFAULT_REVIEW_STACK))
    request_parser.add_argument("--risk-class", default="medium")
    request_parser.add_argument("--changed-files", default="")
    request_parser.add_argument("--mode", choices=("per_pr", "final"), default="per_pr")
    request_parser.add_argument("--json", action="store_true")

    result_parser = sub.add_parser("record-result")
    result_parser.add_argument("--gate", required=True)
    result_parser.add_argument("--pr", type=int, required=True)
    result_parser.add_argument("--branch", required=True)
    result_parser.add_argument("--status", required=True)
    result_parser.add_argument("--summary", required=True)
    result_parser.add_argument("--findings-file", default=None)
    result_parser.add_argument("--residual-risk", default=None)
    result_parser.add_argument("--contract-hash", default="")
    result_parser.add_argument("--pr-id", default="")
    result_parser.add_argument("--report-path", default="")
    result_parser.add_argument("--json", action="store_true")

    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--gate", required=True)
    execute_parser.add_argument("--pr", type=int, default=None)
    execute_parser.add_argument("--pr-id", default="")
    execute_parser.add_argument("--json", action="store_true")

    rexec_parser = sub.add_parser("request-and-execute")
    rexec_parser.add_argument("--pr", type=int, required=True)
    rexec_parser.add_argument("--branch", required=True)
    rexec_parser.add_argument("--review-stack", default=",".join(DEFAULT_REVIEW_STACK))
    rexec_parser.add_argument("--risk-class", default="medium")
    rexec_parser.add_argument("--changed-files", default="")
    rexec_parser.add_argument("--mode", choices=("per_pr", "final"), default="per_pr")
    rexec_parser.add_argument("--json", action="store_true")

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--pr", type=int, required=True)
    status_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    manager = ReviewGateManager()

    if args.command == "request":
        result = manager.request_reviews(
            pr_number=args.pr,
            branch=args.branch,
            review_stack=[item.strip() for item in args.review_stack.split(",") if item.strip()],
            risk_class=args.risk_class,
            changed_files=_parse_changed_files(args.changed_files),
            mode=args.mode,
        )
        print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
        return 0

    if args.command == "record-result":
        findings = []
        if args.findings_file:
            findings = json.loads(Path(args.findings_file).read_text(encoding="utf-8"))
        result = manager.record_result(
            gate=args.gate,
            pr_number=args.pr,
            branch=args.branch,
            status=args.status,
            summary=args.summary,
            findings=findings,
            residual_risk=args.residual_risk,
            contract_hash=args.contract_hash,
            pr_id=args.pr_id,
            report_path=args.report_path,
        )
        print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
        return 0

    if args.command == "request-and-execute":
        result = manager.request_and_execute(
            pr_number=args.pr,
            branch=args.branch,
            review_stack=[item.strip() for item in args.review_stack.split(",") if item.strip()],
            risk_class=args.risk_class,
            changed_files=_parse_changed_files(args.changed_files),
            mode=args.mode,
        )
        print(json.dumps(result, indent=2))
        if result.get("has_required_failure"):
            failed_gates = [
                g["gate"] for g in result.get("gates", [])
                if g.get("execution_status") in ("not_executable", "not_configured")
                and g.get("gate") != "claude_github_optional"
            ]
            print(
                f"ERROR: required gates not executable: {', '.join(failed_gates)}",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.command == "execute":
        result = manager.execute_gate(
            gate=args.gate,
            pr_number=args.pr,
            pr_id=args.pr_id,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "status":
        result = manager.status(args.pr)
        print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
