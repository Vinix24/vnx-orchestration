#!/usr/bin/env python3
"""Review gate orchestration for Gemini, Codex, and optional Claude GitHub review.

This module is the public facade. Implementation is split across mixin modules:
- gate_executor.py: request orchestration and execution
- gate_result_parser.py: finding classification and result recording
- gate_report_generator.py: audit trail and result persistence
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt, utc_now_iso

from gate_executor import GateExecutorMixin
from gate_request_handler import GateRequestHandlerMixin
from gate_result_parser import GateResultParserMixin
from gate_report_generator import GateReportGeneratorMixin


DEFAULT_REVIEW_STACK = ["gemini_review", "codex_gate", "claude_github_optional"]


def _utc_now() -> str:
    return utc_now_iso()


class ReviewGateManager(
    GateExecutorMixin,
    GateRequestHandlerMixin,
    GateResultParserMixin,
    GateReportGeneratorMixin,
):
    """Unified review gate manager composing executor, parser, and report mixins."""

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


def _parse_changed_files(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands."""
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

    return parser


def _handle_request_and_execute(manager: ReviewGateManager, args: argparse.Namespace) -> int:
    """Handle request-and-execute subcommand."""
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


def _handle_record_result(manager: ReviewGateManager, args: argparse.Namespace) -> int:
    """Handle record-result subcommand."""
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
    print(json.dumps(result, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
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
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "record-result":
        return _handle_record_result(manager, args)

    if args.command == "request-and-execute":
        return _handle_request_and_execute(manager, args)

    if args.command == "execute":
        result = manager.execute_gate(gate=args.gate, pr_number=args.pr, pr_id=args.pr_id)
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "status":
        print(json.dumps(manager.status(args.pr), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
