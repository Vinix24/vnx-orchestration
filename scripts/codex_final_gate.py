#!/usr/bin/env python3
"""Codex final gate prompt renderer, enforcement, and receipt processing.

Consumes a ReviewContract to:
1. Render a deliverable-aware Codex final gate prompt
2. Enforce that high-risk/runtime/governance PRs cannot bypass the Codex gate
3. Produce structured final-gate receipts with residual risk and rerun requirements
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from review_contract import ReviewContract
from auto_merge_policy import (
    GOVERNANCE_PATH_MARKERS,
    codex_final_gate_required,
    is_governance_path,
)
from governance_receipts import emit_governance_receipt, utc_now_iso
from result_contract import EXIT_IO, EXIT_OK, EXIT_VALIDATION


# ---------------------------------------------------------------------------
# Gate enforcement
# ---------------------------------------------------------------------------

RUNTIME_PATH_MARKERS = (
    "scripts/lib/runtime_coordination",
    "scripts/lib/dispatch_broker",
    "scripts/commands/start.sh",
    "scripts/commands/stop.sh",
)


def _touches_governance_paths(changed_files: List[str]) -> bool:
    return is_governance_path(changed_files)


def _touches_runtime_paths(changed_files: List[str]) -> bool:
    for path in changed_files:
        p = str(path).strip()
        if any(marker in p for marker in RUNTIME_PATH_MARKERS):
            return True
    return False


@dataclass(frozen=True)
class CodexGateEnforcementResult:
    """Result of evaluating whether a Codex final gate is required."""

    required: bool
    reasons: List[str]
    risk_class: str
    merge_policy: str
    touches_governance: bool
    touches_runtime: bool
    high_risk_by_path: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def enforce_codex_gate(contract: ReviewContract) -> CodexGateEnforcementResult:
    """Determine whether a Codex final gate is required for this contract.

    Required when any of:
    - risk_class is "high"
    - review_stack includes "codex_gate" AND risk_class != "low"
    - changed files touch governance or runtime paths
    - auto_merge_policy says codex_final_gate_required
    """
    reasons: List[str] = []
    risk = (contract.risk_class or "").strip().lower()
    touches_gov = _touches_governance_paths(contract.changed_files)
    touches_rt = _touches_runtime_paths(contract.changed_files)
    high_risk_path = codex_final_gate_required(contract.changed_files)

    if risk == "high":
        reasons.append("risk_class_high")
    if touches_gov:
        reasons.append("touches_governance_paths")
    if touches_rt:
        reasons.append("touches_runtime_paths")
    if high_risk_path and "high_risk_change_scope" not in reasons:
        reasons.append("high_risk_change_scope")
    if "codex_gate" in contract.review_stack and risk != "low":
        reasons.append("codex_gate_in_review_stack")

    return CodexGateEnforcementResult(
        required=len(reasons) > 0,
        reasons=reasons,
        risk_class=risk,
        merge_policy=contract.merge_policy,
        touches_governance=touches_gov,
        touches_runtime=touches_rt,
        high_risk_by_path=high_risk_path,
    )


# ---------------------------------------------------------------------------
# Prompt renderer
# ---------------------------------------------------------------------------

def render_codex_prompt(contract: ReviewContract) -> str:
    """Render a structured Codex final gate review prompt from a ReviewContract.

    The prompt includes all contract sections that Codex needs to evaluate:
    deliverables, non-goals, tests, changed files, deterministic findings,
    closure stage, and quality gate checks.

    Raises ValueError if required contract fields are missing.
    """
    errors: List[str] = []
    if not contract.pr_id:
        errors.append("pr_id")
    if not contract.pr_title:
        errors.append("pr_title")
    if not contract.deliverables:
        errors.append("deliverables")
    if not contract.review_stack:
        errors.append("review_stack")
    if errors:
        raise ValueError(f"Cannot render Codex prompt: missing required fields: {', '.join(errors)}")

    sections: List[str] = []

    # Header
    sections.append(f"# Codex Final Gate Review: {contract.pr_id}")
    sections.append("")
    sections.append(f"**PR**: {contract.pr_id} — {contract.pr_title}")
    sections.append(f"**Feature**: {contract.feature_title}")
    sections.append(f"**Branch**: {contract.branch}")
    sections.append(f"**Track**: {contract.track}")
    sections.append(f"**Risk Class**: {contract.risk_class}")
    sections.append(f"**Merge Policy**: {contract.merge_policy}")
    sections.append(f"**Closure Stage**: {contract.closure_stage}")
    if contract.dispatch_id:
        sections.append(f"**Dispatch ID**: {contract.dispatch_id}")
    sections.append(f"**Content Hash**: {contract.content_hash}")
    sections.append("")

    # Deliverables
    sections.append("## Deliverables")
    sections.append("")
    for i, d in enumerate(contract.deliverables, 1):
        sections.append(f"{i}. [{d.category}] {d.description}")
    sections.append("")

    # Non-goals
    if contract.non_goals:
        sections.append("## Non-Goals (Explicitly Out of Scope)")
        sections.append("")
        for ng in contract.non_goals:
            sections.append(f"- {ng}")
        sections.append("")

    # Quality gate
    if contract.quality_gate:
        sections.append(f"## Quality Gate: `{contract.quality_gate.gate_id}`")
        sections.append("")
        for check in contract.quality_gate.checks:
            sections.append(f"- [ ] {check}")
        sections.append("")

    # Changed files
    if contract.changed_files:
        sections.append(f"## Changed Files ({len(contract.changed_files)})")
        sections.append("")
        for f in contract.changed_files:
            sections.append(f"- `{f}`")
        sections.append("")

    # Scope files
    if contract.scope_files:
        sections.append(f"## Declared Scope Files ({len(contract.scope_files)})")
        sections.append("")
        for f in contract.scope_files:
            sections.append(f"- `{f}`")
        sections.append("")

    # Test evidence
    if contract.test_evidence:
        sections.append("## Test Evidence")
        sections.append("")
        if contract.test_evidence.test_files:
            sections.append("**Test files**:")
            for tf in contract.test_evidence.test_files:
                sections.append(f"- `{tf}`")
        if contract.test_evidence.test_command:
            sections.append(f"**Test command**: `{contract.test_evidence.test_command}`")
        if contract.test_evidence.expected_assertions:
            sections.append(f"**Expected assertions**: {contract.test_evidence.expected_assertions}")
        sections.append("")

    # Deterministic findings
    if contract.deterministic_findings:
        sections.append(f"## Deterministic Findings ({len(contract.deterministic_findings)})")
        sections.append("")
        for finding in contract.deterministic_findings:
            loc = f" ({finding.file_path}:{finding.line})" if finding.file_path else ""
            sections.append(f"- **[{finding.severity}]** [{finding.source}]{loc}: {finding.message}")
        sections.append("")

    # Dependencies
    if contract.dependencies:
        sections.append(f"## Dependencies: {', '.join(contract.dependencies)}")
        sections.append("")

    # Review instructions
    sections.append("## Review Instructions")
    sections.append("")
    sections.append("You are the Codex final gate reviewer. Evaluate this PR against its contract:")
    sections.append("")
    sections.append("1. **Deliverable completeness**: Are all listed deliverables addressed by the changed files?")
    sections.append("2. **Scope discipline**: Do the changes stay within scope and not violate non-goals?")
    sections.append("3. **Quality gate checks**: Can each quality gate check be verified from the evidence?")
    sections.append("4. **Test coverage**: Are declared tests present and do they cover the deliverables?")
    sections.append("5. **Deterministic findings**: Are all error-severity findings resolved?")
    sections.append("6. **Residual risk**: What risks remain after this PR merges?")
    sections.append("")
    sections.append("## Severity rules (strict)")
    sections.append("")
    sections.append("Default `severity` is `warning`. Promote to `error` ONLY when the finding's impact includes one of:")
    sections.append("- Data loss or corruption (database, files, append-only logs)")
    sections.append("- False-positive PR closure (closure_verifier passing when it should block)")
    sections.append("- False-negative PR rejection (closure_verifier blocking when it should pass)")
    sections.append("- Security boundary breach (auth bypass, secret leak, privilege escalation)")
    sections.append("- Cross-dispatch state corruption (one dispatch's data leaking into another's audit trail)")
    sections.append("")
    sections.append("Use `info` for advisory-only observations.")
    sections.append("")
    sections.append("Findings about the following are NOT `error`-severity by default:")
    sections.append("- Style, formatting, log shape (stderr vs stdout, plain vs JSON)")
    sections.append("- Truncated-but-named hash fields (unless a caller compares to a real full SHA)")
    sections.append("- Hardcoded test fixtures (only when tests run elsewhere, mark out-of-scope)")
    sections.append("- Operator-toggled surfaces (when toggling resolves the issue)")
    sections.append("")
    sections.append("Mark findings about lines NOT in this PR's diff as `severity: info` AND include `\"out_of_scope\": true` field.")
    sections.append("Mark findings introduced by a previous fix-round commit as `severity: warning` AND include `\"introduced_by_prior_fix\": true` field.")
    sections.append("")
    sections.append("Respond with a structured JSON verdict:")
    sections.append("```json")
    sections.append('{')
    sections.append('  "verdict": "pass|fail|blocked",')
    sections.append('  "findings": [{"severity": "error|warning|info", "message": "...", "out_of_scope": false, "introduced_by_prior_fix": false}],')
    sections.append('  "residual_risk": "description of remaining risks or null",')
    sections.append('  "rerun_required": false,')
    sections.append('  "rerun_reason": null')
    sections.append('}')
    sections.append("```")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Final gate receipt
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CodexFinalGateReceipt:
    """Structured receipt from a Codex final gate evaluation."""

    pr_id: str
    gate: str = "codex_final_gate"
    verdict: str = "pending"  # pass | fail | blocked | pending
    required: bool = False
    enforcement_reasons: List[str] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    residual_risk: Optional[str] = None
    rerun_required: bool = False
    rerun_reason: Optional[str] = None
    content_hash: str = ""
    prompt_rendered: bool = False
    recorded_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CodexFinalGateReceipt":
        return cls(
            pr_id=d.get("pr_id", ""),
            gate=d.get("gate", "codex_final_gate"),
            verdict=d.get("verdict", "pending"),
            required=d.get("required", False),
            enforcement_reasons=list(d.get("enforcement_reasons") or []),
            findings=list(d.get("findings") or []),
            residual_risk=d.get("residual_risk"),
            rerun_required=d.get("rerun_required", False),
            rerun_reason=d.get("rerun_reason"),
            content_hash=d.get("content_hash", ""),
            prompt_rendered=d.get("prompt_rendered", False),
            recorded_at=d.get("recorded_at", ""),
        )

    @classmethod
    def from_json(cls, text: str) -> "CodexFinalGateReceipt":
        return cls.from_dict(json.loads(text))


def evaluate_and_record(
    contract: ReviewContract,
    *,
    codex_verdict: Optional[Dict[str, Any]] = None,
    output_path: Optional[Path] = None,
) -> CodexFinalGateReceipt:
    """Evaluate enforcement, render prompt, and produce a final gate receipt.

    If codex_verdict is provided, it contains the Codex response parsed as JSON
    with keys: verdict, findings, residual_risk, rerun_required, rerun_reason.

    If codex_verdict is None, the receipt is created in "pending" state with the
    prompt rendered but no verdict yet.
    """
    enforcement = enforce_codex_gate(contract)

    prompt_rendered = False
    try:
        render_codex_prompt(contract)
        prompt_rendered = True
    except ValueError:
        prompt_rendered = False

    if codex_verdict is not None:
        verdict = codex_verdict.get("verdict", "fail")
        findings = codex_verdict.get("findings") or []
        residual_risk = codex_verdict.get("residual_risk")
        rerun_required = codex_verdict.get("rerun_required", False)
        rerun_reason = codex_verdict.get("rerun_reason")
    elif enforcement.required and not prompt_rendered:
        verdict = "blocked"
        findings = [{"severity": "error", "message": "Cannot render prompt: missing required contract fields"}]
        residual_risk = "Codex gate cannot evaluate — contract is incomplete"
        rerun_required = True
        rerun_reason = "contract_incomplete"
    else:
        verdict = "pending"
        findings = []
        residual_risk = None
        rerun_required = False
        rerun_reason = None

    receipt = CodexFinalGateReceipt(
        pr_id=contract.pr_id,
        verdict=verdict,
        required=enforcement.required,
        enforcement_reasons=enforcement.reasons,
        findings=findings,
        residual_risk=residual_risk,
        rerun_required=rerun_required,
        rerun_reason=rerun_reason,
        content_hash=contract.content_hash,
        prompt_rendered=prompt_rendered,
        recorded_at=utc_now_iso(),
    )

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(receipt.to_json() + "\n", encoding="utf-8")

    emit_governance_receipt(
        "codex_final_gate",
        status=verdict,
        terminal="T0",
        pr_id=contract.pr_id,
        gate="codex_final_gate",
        required=enforcement.required,
        enforcement_reasons=enforcement.reasons,
        residual_risk=residual_risk,
        rerun_required=rerun_required,
        rerun_reason=rerun_reason,
        content_hash=contract.content_hash,
    )

    return receipt


def check_gate_clearance(
    contract: ReviewContract,
    receipt: Optional[CodexFinalGateReceipt],
) -> Dict[str, Any]:
    """Check whether a PR can clear the Codex final gate.

    Returns a dict with:
      cleared: bool — whether the gate allows merge
      reason: str — explanation
      blockers: list — specific blockers if not cleared
    """
    enforcement = enforce_codex_gate(contract)

    if not enforcement.required:
        return {
            "cleared": True,
            "reason": "codex_gate_not_required",
            "blockers": [],
        }

    if receipt is None:
        return {
            "cleared": False,
            "reason": "codex_gate_required_no_receipt",
            "blockers": ["missing_codex_gate_receipt"],
        }

    blockers: List[str] = []

    if receipt.verdict == "blocked":
        blockers.append("codex_gate_blocked")
    elif receipt.verdict == "fail":
        blockers.append("codex_gate_failed")
    elif receipt.verdict == "pending":
        blockers.append("codex_gate_pending")
    elif receipt.verdict != "pass":
        blockers.append(f"codex_gate_unknown_verdict_{receipt.verdict}")

    if receipt.rerun_required:
        blockers.append("codex_gate_rerun_required")

    if not receipt.content_hash or not contract.content_hash:
        blockers.append("codex_gate_stale_receipt")
    elif receipt.content_hash != contract.content_hash:
        blockers.append("codex_gate_stale_receipt")

    error_findings = [
        f for f in receipt.findings
        if str(f.get("severity") or "").strip().lower() in {"error", "blocker"}
    ]
    if error_findings:
        blockers.append(f"codex_gate_unresolved_errors_{len(error_findings)}")

    if blockers:
        return {
            "cleared": False,
            "reason": "codex_gate_not_cleared",
            "blockers": blockers,
        }

    return {
        "cleared": True,
        "reason": "codex_gate_passed",
        "blockers": [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_contract(path: str) -> ReviewContract:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Contract file not found: {p}")
    return ReviewContract.from_json(p.read_text(encoding="utf-8"))


def _cmd_render_prompt(args: argparse.Namespace) -> int:
    try:
        contract = _load_contract(args.contract)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_IO

    try:
        prompt = render_codex_prompt(contract)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_VALIDATION

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(prompt + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "path": str(out), "pr_id": contract.pr_id}))
    else:
        print(prompt)

    return EXIT_OK


def _cmd_enforce(args: argparse.Namespace) -> int:
    try:
        contract = _load_contract(args.contract)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_IO

    result = enforce_codex_gate(contract)
    print(json.dumps({"ok": True, **result.to_dict()}, indent=2))
    return EXIT_OK


def _cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        contract = _load_contract(args.contract)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_IO

    codex_verdict = None
    if args.verdict_file:
        vp = Path(args.verdict_file)
        if not vp.exists():
            print(json.dumps({"ok": False, "error": f"Verdict file not found: {vp}"}))
            return EXIT_IO
        codex_verdict = json.loads(vp.read_text(encoding="utf-8"))

    output_path = Path(args.output) if args.output else None

    receipt = evaluate_and_record(
        contract,
        codex_verdict=codex_verdict,
        output_path=output_path,
    )

    print(json.dumps({"ok": True, **receipt.to_dict()}, indent=2))
    return EXIT_OK


def _cmd_check_clearance(args: argparse.Namespace) -> int:
    try:
        contract = _load_contract(args.contract)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_IO

    receipt = None
    if args.receipt:
        rp = Path(args.receipt)
        if not rp.exists():
            print(json.dumps({"ok": False, "error": f"Receipt file not found: {rp}"}))
            return EXIT_IO
        receipt = CodexFinalGateReceipt.from_json(rp.read_text(encoding="utf-8"))

    result = check_gate_clearance(contract, receipt)
    print(json.dumps({"ok": True, **result}, indent=2))
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX Codex final gate prompt renderer and enforcement")
    sub = parser.add_subparsers(dest="command", required=True)

    render_p = sub.add_parser("render-prompt", help="Render Codex final gate prompt from review contract")
    render_p.add_argument("--contract", required=True, help="Path to review contract JSON")
    render_p.add_argument("--output", default="", help="Output file (prints to stdout if omitted)")

    enforce_p = sub.add_parser("enforce", help="Check whether Codex gate is required")
    enforce_p.add_argument("--contract", required=True, help="Path to review contract JSON")

    eval_p = sub.add_parser("evaluate", help="Evaluate and record final gate receipt")
    eval_p.add_argument("--contract", required=True, help="Path to review contract JSON")
    eval_p.add_argument("--verdict-file", default="", help="Path to Codex verdict JSON (omit for pending)")
    eval_p.add_argument("--output", default="", help="Output receipt file path")

    check_p = sub.add_parser("check-clearance", help="Check whether PR clears the Codex gate")
    check_p.add_argument("--contract", required=True, help="Path to review contract JSON")
    check_p.add_argument("--receipt", default="", help="Path to final gate receipt JSON")

    args = parser.parse_args(argv)

    if args.command == "render-prompt":
        return _cmd_render_prompt(args)
    if args.command == "enforce":
        return _cmd_enforce(args)
    if args.command == "evaluate":
        return _cmd_evaluate(args)
    if args.command == "check-clearance":
        return _cmd_check_clearance(args)

    return EXIT_VALIDATION


if __name__ == "__main__":
    raise SystemExit(main())
