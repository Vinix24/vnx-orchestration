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
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

# Net-deletion thresholds: WARN matches pre_merge_gate (5); HOLD is higher here (20)
# because a full Codex review is expensive — only require it for large-scale deletions.
DELETION_FILE_WARN = 5
DELETION_FILE_HOLD = 20

# Net line deletion thresholds (lines_removed - lines_added across all changed files).
# Catches PRs that gut file content without fully deleting files.
NET_LINE_DELETION_WARN = 200
NET_LINE_DELETION_HOLD = 500


def _touches_governance_paths(changed_files: List[str]) -> bool:
    return is_governance_path(changed_files)


def _touches_runtime_paths(changed_files: List[str]) -> bool:
    for path in changed_files:
        p = str(path).strip()
        if any(marker in p for marker in RUNTIME_PATH_MARKERS):
            return True
    return False


def _get_deleted_files(project_root: Path) -> Optional[List[str]]:
    """Return list of files deleted in current PR vs origin/main. None on git failure."""
    for base_ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--diff-filter=D", "--name-only", f"{base_ref}...HEAD"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [f for f in result.stdout.strip().splitlines() if f.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "diff", "--diff-filter=D", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def _parse_numstat_net(numstat_output: str) -> int:
    """Parse git diff --numstat output, return net line deletion (removed - added).

    Binary files report '-' for both columns and are skipped.
    """
    total_added = 0
    total_removed = 0
    for line in numstat_output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] != "-" and parts[1] != "-":
            try:
                total_added += int(parts[0])
                total_removed += int(parts[1])
            except ValueError:
                pass
    return total_removed - total_added


def _get_net_line_deletion(project_root: Path) -> Optional[int]:
    """Return net lines deleted (removed - added) for current PR vs origin/main.

    Returns None on git failure. Complements _get_deleted_files: catches PRs that gut
    file content without fully deleting files.
    """
    for base_ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return _parse_numstat_net(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return _parse_numstat_net(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


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
    mass_deletion_count: int = 0
    mass_deletion_warn: bool = False
    net_line_deletion: int = 0
    net_line_deletion_warn: bool = False
    warnings: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def enforce_codex_gate(
    contract: ReviewContract,
    project_root: Optional[Path] = None,
) -> CodexGateEnforcementResult:
    """Determine whether a Codex final gate is required for this contract.

    Required when any of:
    - risk_class is "high"
    - review_stack includes "codex_gate" AND risk_class != "low"
    - changed files touch governance or runtime paths
    - auto_merge_policy says codex_final_gate_required
    - PR deletes >= DELETION_FILE_HOLD files (when project_root provided)

    Mass-deletion threshold semantics:
    - WARN level: `mass_deletion_warn=True`, added to `warnings` list. Does NOT promote
      required to True and does NOT add to `reasons`.
    - BLOCK level (>= DELETION_FILE_HOLD): adds "mass_file_deletion" to `reasons` and
      sets required=True.

    Operator visibility: WARN surfaces in receipt findings and PR comment but does not
    block merge. BLOCK level requires a full Codex review (required=True).
    Net line deletion (lines_removed - lines_added) is also checked with the same
    graduated semantics: NET_LINE_DELETION_WARN and NET_LINE_DELETION_HOLD.
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

    deleted_files: List[str] = []
    deleted_count = 0
    mass_deletion_warn = False
    net_line_deletion = 0
    net_line_deletion_warn = False
    warnings_list: List[str] = []
    if project_root is not None:
        raw = _get_deleted_files(project_root)
        if raw is not None:
            deleted_files = raw
            deleted_count = len(raw)
        if deleted_count >= DELETION_FILE_HOLD:
            reasons.append("mass_file_deletion")
        elif deleted_count >= DELETION_FILE_WARN:
            mass_deletion_warn = True
            warnings_list.append("mass_deletion_warning")

        raw_net = _get_net_line_deletion(project_root)
        if raw_net is not None:
            net_line_deletion = raw_net
        if net_line_deletion >= NET_LINE_DELETION_HOLD:
            reasons.append("net_line_deletion")
        elif net_line_deletion >= NET_LINE_DELETION_WARN:
            net_line_deletion_warn = True
            warnings_list.append("net_line_deletion_warning")

    return CodexGateEnforcementResult(
        required=len(reasons) > 0,
        reasons=reasons,
        risk_class=risk,
        merge_policy=contract.merge_policy,
        touches_governance=touches_gov,
        touches_runtime=touches_rt,
        high_risk_by_path=high_risk_path,
        mass_deletion_count=deleted_count,
        mass_deletion_warn=mass_deletion_warn,
        net_line_deletion=net_line_deletion,
        net_line_deletion_warn=net_line_deletion_warn,
        warnings=warnings_list,
        deleted_files=deleted_files,
    )


# ---------------------------------------------------------------------------
# Prompt renderer
# ---------------------------------------------------------------------------

def _format_metadata_header(contract: ReviewContract) -> List[str]:
    """Return prompt lines for the PR metadata header block."""
    lines: List[str] = [
        f"# Codex Final Gate Review: {contract.pr_id}",
        "",
        f"**PR**: {contract.pr_id} — {contract.pr_title}",
        f"**Feature**: {contract.feature_title}",
        f"**Branch**: {contract.branch}",
        f"**Track**: {contract.track}",
        f"**Risk Class**: {contract.risk_class}",
        f"**Merge Policy**: {contract.merge_policy}",
        f"**Closure Stage**: {contract.closure_stage}",
    ]
    if contract.dispatch_id:
        lines.append(f"**Dispatch ID**: {contract.dispatch_id}")
    lines += [f"**Content Hash**: {contract.content_hash}", ""]
    return lines


def _format_deleted_files_alert(deleted_files: List[str]) -> List[str]:
    """Return prompt lines for the Net-Deletion Alert section."""
    count = len(deleted_files)
    level = "HOLD" if count >= DELETION_FILE_HOLD else "WARN"
    lines: List[str] = [
        f"## Net-Deletion Alert [{level}] ({count} file(s) deleted)",
        "",
        f"> **{count} file(s)** are fully deleted in this PR. Verify each deletion is intentional and covered by a deliverable or non-goal.",
        "",
    ]
    for f in deleted_files:
        lines.append(f"- `{f}`")
    lines.append("")
    return lines


def _format_evidence_sections(contract: ReviewContract) -> List[str]:
    """Return prompt lines for changed files, scope, test evidence, findings, and deps."""
    lines: List[str] = []

    if contract.changed_files:
        lines += [f"## Changed Files ({len(contract.changed_files)})", ""]
        for f in contract.changed_files:
            lines.append(f"- `{f}`")
        lines.append("")

    if contract.scope_files:
        lines += [f"## Declared Scope Files ({len(contract.scope_files)})", ""]
        for f in contract.scope_files:
            lines.append(f"- `{f}`")
        lines.append("")

    if contract.test_evidence:
        lines += ["## Test Evidence", ""]
        if contract.test_evidence.test_files:
            lines.append("**Test files**:")
            for tf in contract.test_evidence.test_files:
                lines.append(f"- `{tf}`")
        if contract.test_evidence.test_command:
            lines.append(f"**Test command**: `{contract.test_evidence.test_command}`")
        if contract.test_evidence.expected_assertions:
            lines.append(f"**Expected assertions**: {contract.test_evidence.expected_assertions}")
        lines.append("")

    if contract.deterministic_findings:
        lines += [f"## Deterministic Findings ({len(contract.deterministic_findings)})", ""]
        for finding in contract.deterministic_findings:
            loc = f" ({finding.file_path}:{finding.line})" if finding.file_path else ""
            lines.append(f"- **[{finding.severity}]** [{finding.source}]{loc}: {finding.message}")
        lines.append("")

    if contract.dependencies:
        lines += [f"## Dependencies: {', '.join(contract.dependencies)}", ""]

    return lines


def _format_review_instructions() -> List[str]:
    """Return prompt lines for the review instructions and severity rules sections."""
    return [
        "## Review Instructions",
        "",
        "You are the Codex final gate reviewer. Evaluate this PR against its contract:",
        "",
        "1. **Deliverable completeness**: Are all listed deliverables addressed by the changed files?",
        "2. **Scope discipline**: Do the changes stay within scope and not violate non-goals?",
        "3. **Quality gate checks**: Can each quality gate check be verified from the evidence?",
        "4. **Test coverage**: Are declared tests present and do they cover the deliverables?",
        "5. **Deterministic findings**: Are all error-severity findings resolved?",
        "6. **Net deletion sanity**: Count files listed as entirely removed in the diff. If ≥5 files are deleted, include a warning finding with the count and confirm the deletions are intentional (not accidental scope reduction).",
        "7. **Residual risk**: What risks remain after this PR merges?",
        "",
        "## Severity rules (strict)",
        "",
        "Default `severity` is `warning`. Promote to `error` ONLY when the finding's impact includes one of:",
        "- Data loss or corruption (database, files, append-only logs)",
        "- False-positive PR closure (closure_verifier passing when it should block)",
        "- False-negative PR rejection (closure_verifier blocking when it should pass)",
        "- Security boundary breach (auth bypass, secret leak, privilege escalation)",
        "- Cross-dispatch state corruption (one dispatch's data leaking into another's audit trail)",
        "",
        "Use `info` for advisory-only observations.",
        "",
        "Findings about the following are NOT `error`-severity by default:",
        "- Style, formatting, log shape (stderr vs stdout, plain vs JSON)",
        "- Truncated-but-named hash fields (unless a caller compares to a real full SHA)",
        "- Hardcoded test fixtures (only when tests run elsewhere, mark out-of-scope)",
        "- Operator-toggled surfaces (when toggling resolves the issue)",
        "",
        "Mark findings about lines NOT in this PR's diff as `severity: info` AND include `\"out_of_scope\": true` field.",
        "Mark findings introduced by a previous fix-round commit as `severity: warning` AND include `\"introduced_by_prior_fix\": true` field.",
        "",
        "Respond with a structured JSON verdict:",
        "```json",
        '{',
        '  "verdict": "pass|fail|blocked",',
        '  "findings": [{"severity": "error|warning|info", "message": "...", "out_of_scope": false, "introduced_by_prior_fix": false}],',
        '  "residual_risk": "description of remaining risks or null",',
        '  "rerun_required": false,',
        '  "rerun_reason": null',
        '}',
        "```",
    ]


def render_codex_prompt(
    contract: ReviewContract,
    deleted_files: Optional[List[str]] = None,
) -> str:
    """Render a structured Codex final gate review prompt from a ReviewContract.

    The prompt includes all contract sections that Codex needs to evaluate:
    deliverables, non-goals, tests, changed files, deterministic findings,
    closure stage, and quality gate checks.

    If deleted_files is provided, a Net-Deletion Alert section is added to
    the prompt so the Codex reviewer can assess the scope of removed files.

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

    sections: List[str] = _format_metadata_header(contract)

    sections += ["## Deliverables", ""]
    for i, d in enumerate(contract.deliverables, 1):
        sections.append(f"{i}. [{d.category}] {d.description}")
    sections.append("")

    if contract.non_goals:
        sections += ["## Non-Goals (Explicitly Out of Scope)", ""]
        for ng in contract.non_goals:
            sections.append(f"- {ng}")
        sections.append("")

    if contract.quality_gate:
        sections += [f"## Quality Gate: `{contract.quality_gate.gate_id}`", ""]
        for check in contract.quality_gate.checks:
            sections.append(f"- [ ] {check}")
        sections.append("")

    sections.extend(_format_evidence_sections(contract))

    if deleted_files:
        sections.extend(_format_deleted_files_alert(deleted_files))

    sections.extend(_format_review_instructions())

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


def _apply_mass_deletion_warning(
    enforcement: CodexGateEnforcementResult,
    findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Prepend WARN-level findings for file-count and net-line-count deletion thresholds.

    WARN does not make the gate required; findings surface as advisory in the receipt.
    """
    prefixed: List[Dict[str, Any]] = list(findings)
    if enforcement.net_line_deletion_warn:
        prefixed.insert(0, {
            "severity": "warning",
            "message": (
                f"Net line deletion warning: {enforcement.net_line_deletion} net lines deleted "
                f"(>= {NET_LINE_DELETION_WARN} threshold) — verify intentional scope reduction"
            ),
        })
    if enforcement.mass_deletion_warn:
        prefixed.insert(0, {
            "severity": "warning",
            "message": (
                f"Net deletion warning: {enforcement.mass_deletion_count} file(s) deleted "
                f"(>= {DELETION_FILE_WARN} threshold) — verify intentional scope reduction"
            ),
        })
    return prefixed


def _atomic_write_text(target: Path, content: str) -> None:
    """Atomic write: tmp → fsync → os.replace. Prevents partial-state on crash/disk-full."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _persist_result(
    receipt: CodexFinalGateReceipt,
    output_path: Optional[Path],
    contract: ReviewContract,
) -> None:
    """Write receipt to disk (if output_path given) and emit governance receipt."""
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(output_path, receipt.to_json() + "\n")

    emit_governance_receipt(
        "codex_final_gate",
        status=receipt.verdict,
        terminal="T0",
        pr_id=contract.pr_id,
        gate="codex_final_gate",
        required=receipt.required,
        enforcement_reasons=receipt.enforcement_reasons,
        residual_risk=receipt.residual_risk,
        rerun_required=receipt.rerun_required,
        rerun_reason=receipt.rerun_reason,
        content_hash=contract.content_hash,
    )


def evaluate_and_record(
    contract: ReviewContract,
    *,
    codex_verdict: Optional[Dict[str, Any]] = None,
    output_path: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> CodexFinalGateReceipt:
    """Evaluate enforcement, render prompt, and produce a final gate receipt.

    If codex_verdict is provided, it contains the Codex response parsed as JSON
    with keys: verdict, findings, residual_risk, rerun_required, rerun_reason.

    If codex_verdict is None, the receipt is created in "pending" state with the
    prompt rendered but no verdict yet.
    """
    enforcement = enforce_codex_gate(contract, project_root=project_root)

    deleted_files_for_prompt = enforcement.deleted_files if enforcement.deleted_files else None

    prompt_rendered = False
    try:
        render_codex_prompt(contract, deleted_files=deleted_files_for_prompt)
        prompt_rendered = True
    except ValueError:
        prompt_rendered = False

    if codex_verdict is not None:
        verdict = codex_verdict.get("verdict", "fail")
        findings = list(codex_verdict.get("findings") or [])
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

    findings = _apply_mass_deletion_warning(enforcement, findings)

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

    _persist_result(receipt, output_path, contract)
    return receipt


def check_gate_clearance(
    contract: ReviewContract,
    receipt: Optional[CodexFinalGateReceipt],
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Check whether a PR can clear the Codex final gate.

    Returns a dict with:
      cleared: bool — whether the gate allows merge
      reason: str — explanation
      blockers: list — specific blockers if not cleared
    """
    effective_root = project_root if project_root is not None else SCRIPT_DIR.parent
    enforcement = enforce_codex_gate(contract, project_root=effective_root)

    if not enforcement.required:
        return {
            "cleared": True,
            "reason": "codex_gate_not_required",
            "blockers": [],
            "warnings": list(enforcement.warnings),
        }

    if receipt is None:
        return {
            "cleared": False,
            "reason": "codex_gate_required_no_receipt",
            "blockers": ["missing_codex_gate_receipt"],
            "warnings": list(enforcement.warnings),
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
            "warnings": list(enforcement.warnings),
        }

    return {
        "cleared": True,
        "reason": "codex_gate_passed",
        "blockers": [],
        "warnings": list(enforcement.warnings),
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

    deleted_files = _get_deleted_files(SCRIPT_DIR.parent)

    try:
        prompt = render_codex_prompt(contract, deleted_files=deleted_files)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_VALIDATION

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(out, prompt + "\n")
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

    result = enforce_codex_gate(contract, project_root=SCRIPT_DIR.parent)
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
        project_root=SCRIPT_DIR.parent,
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

    result = check_gate_clearance(contract, receipt, project_root=SCRIPT_DIR.parent)
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
