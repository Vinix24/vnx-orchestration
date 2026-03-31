#!/usr/bin/env python3
"""Renders deliverable-aware Gemini review prompts from a ReviewContract.

Responsibilities:
- Validate that required contract fields are present; fail explicitly on missing fields
- Render a structured prompt covering deliverables, non-goals, changed files, declared
  tests, quality gate checks, and deterministic findings
- Build GeminiReviewReceipt payloads that clearly distinguish advisory from blocking
  findings so T0 can act on the classification without parsing raw text
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from review_contract import ReviewContract

# Fields that MUST be present for the prompt renderer to produce a valid prompt.
# Missing any of these raises MissingContractFieldError immediately.
REQUIRED_CONTRACT_FIELDS = [
    "pr_id",
    "pr_title",
    "deliverables",
    "review_stack",
    "risk_class",
    "merge_policy",
]


class MissingContractFieldError(ValueError):
    """Raised when a required review contract field is absent or empty."""

    def __init__(self, field_name: str) -> None:
        super().__init__(f"Required review contract field is missing or empty: '{field_name}'")
        self.field_name = field_name


@dataclass(frozen=True)
class GeminiReviewFinding:
    """A single finding emitted from a Gemini review, classified by severity."""

    severity: str  # "advisory" | "blocking"
    category: str  # "correctness" | "security" | "style" | "coverage" | "contract"
    message: str
    file_path: str = ""
    line: int = 0

    def is_blocking(self) -> bool:
        return self.severity == "blocking"

    def is_advisory(self) -> bool:
        return self.severity == "advisory"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "file_path": self.file_path,
            "line": self.line,
        }


@dataclass
class GeminiReviewReceipt:
    """Structured receipt from a Gemini review gate.

    advisory_findings and blocking_findings are always separate lists so that
    T0 and downstream gates can act on the classification without re-parsing text.
    """

    pr_id: str
    gate: str = "gemini_review"
    status: str = "pending"  # "pending" | "pass" | "fail" | "blocked"
    summary: str = ""
    advisory_findings: List[GeminiReviewFinding] = field(default_factory=list)
    blocking_findings: List[GeminiReviewFinding] = field(default_factory=list)
    contract_hash: str = ""
    reviewed_at: str = ""

    @property
    def advisory_count(self) -> int:
        return len(self.advisory_findings)

    @property
    def blocking_count(self) -> int:
        return len(self.blocking_findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "gate": self.gate,
            "status": self.status,
            "summary": self.summary,
            "advisory_findings": [f.to_dict() for f in self.advisory_findings],
            "blocking_findings": [f.to_dict() for f in self.blocking_findings],
            "advisory_count": self.advisory_count,
            "blocking_count": self.blocking_count,
            "contract_hash": self.contract_hash,
            "reviewed_at": self.reviewed_at,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_raw_findings(
        cls,
        *,
        pr_id: str,
        raw_findings: List[Dict[str, Any]],
        contract_hash: str = "",
        reviewed_at: str = "",
    ) -> "GeminiReviewReceipt":
        """Classify raw findings dicts into advisory vs blocking.

        A finding is blocking when its severity is ``"blocking"`` or ``"error"``.
        All other severity values (``"advisory"``, ``"warning"``, ``"info"``, etc.)
        are classified as advisory.
        """
        advisory: List[GeminiReviewFinding] = []
        blocking: List[GeminiReviewFinding] = []

        for raw in raw_findings:
            raw_severity = str(raw.get("severity", "advisory")).lower()
            classified = "blocking" if raw_severity in ("blocking", "error") else "advisory"
            finding = GeminiReviewFinding(
                severity=classified,
                category=str(raw.get("category", "general")),
                message=str(raw.get("message", "")),
                file_path=str(raw.get("file_path", "")),
                line=int(raw.get("line", 0)),
            )
            if finding.is_blocking():
                blocking.append(finding)
            else:
                advisory.append(finding)

        if blocking:
            status = "fail"
            summary = f"{len(blocking)} blocking, {len(advisory)} advisory finding(s)"
        elif advisory:
            status = "pass"
            summary = f"0 blocking, {len(advisory)} advisory finding(s)"
        else:
            status = "pass"
            summary = "LGTM — no findings"

        return cls(
            pr_id=pr_id,
            status=status,
            summary=summary,
            advisory_findings=advisory,
            blocking_findings=blocking,
            contract_hash=contract_hash,
            reviewed_at=reviewed_at,
        )


def _validate_contract(contract: ReviewContract) -> None:
    """Assert all required fields are present; raise MissingContractFieldError if not."""
    if not contract.pr_id:
        raise MissingContractFieldError("pr_id")
    if not contract.pr_title:
        raise MissingContractFieldError("pr_title")
    if not contract.deliverables:
        raise MissingContractFieldError("deliverables")
    if not contract.review_stack:
        raise MissingContractFieldError("review_stack")
    if not contract.risk_class:
        raise MissingContractFieldError("risk_class")
    if not contract.merge_policy:
        raise MissingContractFieldError("merge_policy")


def render_gemini_prompt(contract: ReviewContract) -> str:
    """Render a deliverable-aware Gemini review prompt from a ReviewContract.

    The rendered prompt includes:
    - PR metadata (id, title, feature, branch, risk class, merge policy, closure stage)
    - All deliverables with their category
    - Non-goals (out-of-scope items the reviewer must not flag)
    - Changed files to focus the review
    - Quality gate checks for explicit pass/fail criteria
    - Declared test evidence (test files, test command)
    - Pre-computed deterministic findings from static analysis
    - Structured response schema so Gemini emits parseable JSON with advisory/blocking classification

    Raises:
        MissingContractFieldError: when any required field is absent or empty.
    """
    _validate_contract(contract)

    lines: List[str] = []

    # Header
    lines.append(f"# Gemini Code Review: {contract.pr_id} — {contract.pr_title}")
    lines.append("")

    # PR metadata
    if contract.feature_title:
        lines.append(f"**Feature**: {contract.feature_title}")
    lines.append(f"**Branch**: {contract.branch or '(unset)'}")
    lines.append(f"**Track**: {contract.track or '(unset)'}")
    lines.append(f"**Risk class**: {contract.risk_class}")
    lines.append(f"**Merge policy**: {contract.merge_policy}")
    lines.append(f"**Closure stage**: {contract.closure_stage}")
    if contract.content_hash:
        lines.append(f"**Contract hash**: `{contract.content_hash}`")
    if contract.dependencies:
        lines.append(f"**Dependencies**: {', '.join(contract.dependencies)}")
    lines.append("")

    # Deliverables — required field, always present after validation
    lines.append("## Deliverables")
    lines.append("Verify that ALL of the following deliverables are present, correct, and tested:")
    lines.append("")
    for d in contract.deliverables:
        lines.append(f"- **[{d.category}]** {d.description}")
    lines.append("")

    # Non-goals
    if contract.non_goals:
        lines.append("## Non-Goals (Out of Scope — Do Not Flag)")
        lines.append(
            "The following items are explicitly out of scope for this PR. "
            "Do NOT raise findings for them."
        )
        lines.append("")
        for ng in contract.non_goals:
            lines.append(f"- {ng}")
        lines.append("")

    # Changed files
    if contract.changed_files:
        lines.append("## Changed Files")
        lines.append("Restrict your review to these files:")
        lines.append("")
        for f in contract.changed_files:
            lines.append(f"- `{f}`")
        lines.append("")
    elif contract.scope_files:
        lines.append("## Expected Scope Files")
        lines.append("No git diff was provided; these files are declared in scope:")
        lines.append("")
        for f in contract.scope_files:
            lines.append(f"- `{f}`")
        lines.append("")

    # Quality gate checks
    if contract.quality_gate:
        lines.append("## Quality Gate")
        lines.append(f"Gate ID: `{contract.quality_gate.gate_id}`")
        lines.append("")
        lines.append("Mark each check below as PASS or FAIL with evidence:")
        lines.append("")
        for check in contract.quality_gate.checks:
            lines.append(f"- [ ] {check}")
        lines.append("")

    # Declared tests
    if contract.test_evidence:
        te = contract.test_evidence
        lines.append("## Declared Test Evidence")
        lines.append("")
        if te.test_command:
            lines.append(f"**Test command**: `{te.test_command}`")
        if te.test_files:
            lines.append("**Test files**:")
            for tf in te.test_files:
                lines.append(f"  - `{tf}`")
        if te.expected_assertions:
            lines.append(f"**Expected assertions**: {te.expected_assertions}")
        lines.append("")
        lines.append(
            "Verify that the declared test files exist, cover the deliverables, "
            "and that the test command passes."
        )
        lines.append("")

    # Deterministic findings
    if contract.deterministic_findings:
        lines.append("## Pre-computed Deterministic Findings")
        lines.append(
            "The following findings were produced by static analysis before this review. "
            "You MUST include each of them in your response classified as advisory or blocking."
        )
        lines.append("")
        for f in contract.deterministic_findings:
            loc = f" ({f.file_path}:{f.line})" if f.file_path else ""
            lines.append(f"- **[{f.severity.upper()}]** `{f.source}`: {f.message}{loc}")
        lines.append("")

    # Response schema and instructions
    lines.append("## Review Instructions")
    lines.append("")
    lines.append(
        "Classify each finding as **blocking** or **advisory**:"
    )
    lines.append("")
    lines.append(
        "- **blocking** — must be resolved before merge "
        "(correctness error, security vulnerability, missing deliverable, contract violation)"
    )
    lines.append(
        "- **advisory** — improvement recommended but does not block merge "
        "(style, minor performance, documentation)"
    )
    lines.append("")
    lines.append("Respond with valid JSON only, using this exact schema:")
    lines.append("")
    lines.append("```json")
    lines.append("{")
    lines.append('  "summary": "<one-sentence verdict>",')
    lines.append('  "findings": [')
    lines.append('    {')
    lines.append('      "severity": "blocking | advisory",')
    lines.append('      "category": "correctness | security | style | coverage | contract",')
    lines.append('      "message": "<clear description of the issue>",')
    lines.append('      "file_path": "<relative file path or empty string>",')
    lines.append('      "line": <line number as integer, 0 if unknown>')
    lines.append('    }')
    lines.append('  ]')
    lines.append('}')
    lines.append("```")
    lines.append("")
    lines.append(
        "If all deliverables are satisfied, quality gate checks pass, and there are "
        'no blocking findings, set `"summary"` to `"LGTM"` and `"findings"` to `[]`.'
    )

    return "\n".join(lines)
