#!/usr/bin/env python3
"""Review gate orchestration for Gemini, Codex, and optional Claude GitHub review."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt
from auto_merge_policy import codex_final_gate_required
from review_contract import ReviewContract
from gemini_prompt_renderer import (
    GeminiReviewReceipt,
    MissingContractFieldError,
    render_gemini_prompt,
)


DEFAULT_REVIEW_STACK = ["gemini_review", "codex_gate", "claude_github_optional"]


def _utc_now() -> str:
    from governance_receipts import utc_now_iso
    return utc_now_iso()


class ReviewGateManager:
    def __init__(self) -> None:
        self.paths = ensure_env()
        self.state_dir = Path(self.paths["VNX_STATE_DIR"])
        self.requests_dir = self.state_dir / "review_gates" / "requests"
        self.results_dir = self.state_dir / "review_gates" / "results"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.results_dir / f"pr-{pr_number}-{gate}.json"

    def _gemini_available(self) -> bool:
        return os.environ.get("VNX_GEMINI_REVIEW_ENABLED", "1") != "0" and shutil.which("gemini") is not None

    def _codex_headless_available(self) -> bool:
        return os.environ.get("VNX_CODEX_HEADLESS_ENABLED", "0") == "1" and shutil.which("codex") is not None

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
        payload = {
            "gate": "gemini_review",
            "status": "queued" if available else "blocked",
            "provider": "gemini_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": _utc_now(),
        }
        if not available:
            payload["reason"] = "gemini_not_available"
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
        payload: Dict[str, Any] = {
            "gate": "gemini_review",
            "status": "queued" if available else "blocked",
            "provider": "gemini_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": None,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "prompt": prompt,
            "requested_at": _utc_now(),
        }
        if not available:
            payload["reason"] = "gemini_not_available"

        pr_slug = contract.pr_id.lower().replace("-", "")
        request_file = self.requests_dir / f"{pr_slug}-gemini_review-contract.json"
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

    def _request_codex(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
        required = mode == "final" or codex_final_gate_required(changed_files)
        available = self._codex_headless_available()
        model = os.environ.get("VNX_CODEX_HEADLESS_MODEL") or os.environ.get("VNX_CODEX_MODEL") or "gpt-5.2-codex"
        payload = {
            "gate": "codex_gate",
            "status": "queued" if available else ("blocked" if required else "not_configured"),
            "provider": "codex_headless",
            "model": model,
            "required": required,
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": _utc_now(),
        }
        if not available:
            payload["reason"] = "codex_headless_not_available"
        self._request_path("codex_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_claude_github(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str
    ) -> Dict[str, Any]:
        configured = self._claude_github_configured()
        payload = {
            "gate": "claude_github_optional",
            "status": "not_configured",
            "provider": "claude_github",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": _utc_now(),
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
    ) -> Dict[str, Any]:
        """Record a review gate result with explicit advisory/blocking finding classification.

        Findings are classified by their ``severity`` field:
        - ``"blocking"`` or ``"error"`` severity → blocking_findings
        - all other values → advisory_findings

        Both lists are always present in the payload so downstream consumers can
        act on the classification without re-parsing the raw findings list.
        """
        raw_findings = findings or []
        receipt = GeminiReviewReceipt.from_raw_findings(
            pr_id=pr_id or str(pr_number),
            raw_findings=raw_findings,
            contract_hash=contract_hash,
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
            "residual_risk": residual_risk,
            "contract_hash": contract_hash,
            "recorded_at": receipt.reviewed_at,
        }
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
            residual_risk=residual_risk,
            contract_hash=contract_hash,
        )
        return payload

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
    result_parser.add_argument("--json", action="store_true")

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
        )
        print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
        return 0

    if args.command == "status":
        result = manager.status(args.pr)
        print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
