#!/usr/bin/env python3
"""Canonical receipt schema for headless review gate results.

This module defines the unified schema that every headless review gate result
(Gemini, Codex, Claude GitHub) MUST conform to before being accepted as
closure evidence by T0 and the closure verifier.

The schema enforces the fields required by 45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md
Section 4. Gate-specific receipt types (GeminiReviewReceipt, CodexFinalGateReceipt,
ClaudeGitHubReviewReceipt) remain authoritative for their gate-specific logic,
but their persisted result files must include all fields defined here.

This module provides:
- HeadlessReviewReceipt: canonical dataclass for all gate results
- validate_gate_result: checks a raw dict against required fields
- normalize_gate_result: ensures a gate result dict has all required fields
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1.0.0"

# Valid top-level status values for gate results.
VALID_STATUSES = frozenset([
    "pass",
    "fail",
    "blocked",
    "pending",
    "not_configured",
    "configured_dry_run",
])

# Fields required in every gate result JSON file per the headless review
# evidence contract (45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md Section 4).
REQUIRED_FIELDS = frozenset([
    "gate",
    "pr_id",
    "branch",
    "status",
    "summary",
    "contract_hash",
    "report_path",
    "blocking_findings",
    "advisory_findings",
    "blocking_count",
    "advisory_count",
    "required_reruns",
    "residual_risk",
    "recorded_at",
])


@dataclass
class HeadlessReviewReceipt:
    """Canonical receipt for a headless review gate result.

    Every gate result stored under ``$VNX_STATE_DIR/review_gates/results/``
    must be representable as this schema.  The closure verifier and T0 consume
    this shape to make deterministic closure decisions.
    """

    gate: str
    pr_id: str
    branch: str
    status: str  # pass | fail | blocked | pending | not_configured | configured_dry_run
    summary: str
    contract_hash: str
    report_path: str  # path under $VNX_DATA_DIR/unified_reports/headless/
    blocking_findings: List[Dict[str, Any]] = field(default_factory=list)
    advisory_findings: List[Dict[str, Any]] = field(default_factory=list)
    blocking_count: int = 0
    advisory_count: int = 0
    required_reruns: List[str] = field(default_factory=list)
    residual_risk: str = ""
    recorded_at: str = ""

    # Optional fields that gate-specific logic may populate.
    pr_number: Optional[int] = None
    verdict: Optional[str] = None  # Codex uses "verdict" instead of "status"
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Ensure counts are consistent with finding arrays.
        d["blocking_count"] = len(d["blocking_findings"])
        d["advisory_count"] = len(d["advisory_findings"])
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HeadlessReviewReceipt":
        return cls(
            gate=str(d.get("gate", "")),
            pr_id=str(d.get("pr_id", "")),
            branch=str(d.get("branch", "")),
            status=str(d.get("status", "")),
            summary=str(d.get("summary", "")),
            contract_hash=str(d.get("contract_hash", "")),
            report_path=str(d.get("report_path", "")),
            blocking_findings=list(d.get("blocking_findings") or []),
            advisory_findings=list(d.get("advisory_findings") or []),
            blocking_count=int(d.get("blocking_count", 0)),
            advisory_count=int(d.get("advisory_count", 0)),
            required_reruns=list(d.get("required_reruns") or []),
            residual_risk=str(d.get("residual_risk") or ""),
            recorded_at=str(d.get("recorded_at", "")),
            pr_number=d.get("pr_number"),
            verdict=d.get("verdict"),
            schema_version=str(d.get("schema_version", SCHEMA_VERSION)),
        )

    @classmethod
    def from_json(cls, text: str) -> "HeadlessReviewReceipt":
        return cls.from_dict(json.loads(text))

    def is_pass(self) -> bool:
        """Return True only when the gate conclusively passed with no blockers."""
        effective_status = self.verdict or self.status
        return effective_status == "pass" and self.blocking_count == 0

    def is_contradictory(self) -> bool:
        """Return True when status says pass but blocking findings exist."""
        effective_status = self.verdict or self.status
        return effective_status == "pass" and len(self.blocking_findings) > 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationError:
    """A single validation issue found in a gate result dict."""

    field: str
    message: str
    severity: str = "error"  # error | warning


def validate_gate_result(result: Dict[str, Any]) -> List[ValidationError]:
    """Validate a raw gate result dict against the headless review contract.

    Returns an empty list when the result is fully compliant.
    """
    errors: List[ValidationError] = []

    # Check required fields are present and non-empty for identity fields.
    for f in REQUIRED_FIELDS:
        if f not in result:
            errors.append(ValidationError(f, f"required field '{f}' is missing"))

    # Identity fields must be non-empty strings.
    for f in ("gate", "pr_id", "branch", "status", "recorded_at"):
        val = result.get(f)
        if isinstance(val, str) and not val.strip():
            errors.append(ValidationError(f, f"required field '{f}' is empty"))

    # Status must be a recognized value.
    status = result.get("status", "")
    if status and status not in VALID_STATUSES:
        errors.append(ValidationError(
            "status",
            f"unrecognized status '{status}' — expected one of {sorted(VALID_STATUSES)}",
            severity="warning",
        ))

    # report_path must be non-empty for pass/fail results.
    report_path = result.get("report_path", "")
    if status in ("pass", "fail") and not report_path:
        errors.append(ValidationError(
            "report_path",
            "report_path is required for pass/fail gate results",
        ))

    # Findings arrays must be lists.
    for f in ("blocking_findings", "advisory_findings", "required_reruns"):
        val = result.get(f)
        if val is not None and not isinstance(val, list):
            errors.append(ValidationError(f, f"'{f}' must be a list, got {type(val).__name__}"))

    # Count consistency.
    blocking = result.get("blocking_findings") or []
    advisory = result.get("advisory_findings") or []
    if isinstance(blocking, list) and result.get("blocking_count") != len(blocking):
        errors.append(ValidationError(
            "blocking_count",
            f"blocking_count ({result.get('blocking_count')}) != len(blocking_findings) ({len(blocking)})",
            severity="warning",
        ))
    if isinstance(advisory, list) and result.get("advisory_count") != len(advisory):
        errors.append(ValidationError(
            "advisory_count",
            f"advisory_count ({result.get('advisory_count')}) != len(advisory_findings) ({len(advisory)})",
            severity="warning",
        ))

    # Contradictory evidence detection.
    if status == "pass" and isinstance(blocking, list) and len(blocking) > 0:
        errors.append(ValidationError(
            "status",
            f"contradictory: status is 'pass' but {len(blocking)} blocking finding(s) exist",
        ))

    # contract_hash should be non-empty for closure-relevant results.
    if status in ("pass", "fail") and not result.get("contract_hash"):
        errors.append(ValidationError(
            "contract_hash",
            "contract_hash is required for pass/fail gate results",
        ))

    return errors


def validate_report_path_exists(report_path: str) -> Optional[ValidationError]:
    """Check that the normalized report file exists on disk.

    Returns None when the file exists, or a ValidationError when it does not.
    """
    if not report_path:
        return ValidationError("report_path", "report_path is empty")
    p = Path(report_path)
    if not p.exists():
        return ValidationError("report_path", f"report file does not exist: {report_path}")
    if not p.is_file():
        return ValidationError("report_path", f"report_path is not a file: {report_path}")
    return None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_gate_result(
    result: Dict[str, Any],
    *,
    report_path: str = "",
) -> Dict[str, Any]:
    """Ensure a gate result dict has all required fields with safe defaults.

    This is a non-destructive operation: existing values are preserved,
    missing fields get zero-value defaults, and ``report_path`` is set
    from the keyword argument if not already present.

    Use this when recording a gate result to guarantee contract compliance.
    """
    normalized = dict(result)

    # Ensure all required fields exist with safe defaults.
    defaults: Dict[str, Any] = {
        "gate": "",
        "pr_id": "",
        "branch": "",
        "status": "",
        "summary": "",
        "contract_hash": "",
        "report_path": report_path,
        "blocking_findings": [],
        "advisory_findings": [],
        "blocking_count": 0,
        "advisory_count": 0,
        "required_reruns": [],
        "residual_risk": "",
        "recorded_at": "",
    }

    for k, default in defaults.items():
        if k not in normalized:
            normalized[k] = default

    # Override report_path only if the caller provided one and the result
    # doesn't already have one.
    if report_path and not normalized.get("report_path"):
        normalized["report_path"] = report_path

    # Reconcile counts with arrays.
    if isinstance(normalized.get("blocking_findings"), list):
        normalized["blocking_count"] = len(normalized["blocking_findings"])
    if isinstance(normalized.get("advisory_findings"), list):
        normalized["advisory_count"] = len(normalized["advisory_findings"])

    return normalized
