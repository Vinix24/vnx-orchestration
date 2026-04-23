"""Headless review gate receipt schema, validation, and normalization.

Contract: Section 4 of the headless review gate spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"

VALID_STATUSES = {
    "pass",
    "fail",
    "blocked",
    "pending",
    "not_configured",
    "configured_dry_run",
}

REQUIRED_FIELDS = {
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
}

_IDENTITY_FIELDS = {"gate", "pr_id"}

_DEFAULT_EMPTY: Dict[str, Any] = {
    "gate": "",
    "pr_id": "",
    "branch": "",
    "status": "",
    "summary": "",
    "contract_hash": "",
    "report_path": "",
    "blocking_findings": [],
    "advisory_findings": [],
    "blocking_count": 0,
    "advisory_count": 0,
    "required_reruns": [],
    "residual_risk": "",
    "recorded_at": "",
}


@dataclass
class ValidationError:
    field: str
    severity: str  # "error" | "warning" | "info"
    message: str


@dataclass
class HeadlessReviewReceipt:
    gate: str
    pr_id: str
    branch: str
    status: str
    summary: str
    contract_hash: str
    report_path: str
    blocking_findings: List[Dict[str, Any]]
    advisory_findings: List[Dict[str, Any]]
    blocking_count: int
    advisory_count: int
    required_reruns: List[str]
    residual_risk: str
    recorded_at: str
    verdict: str = ""
    dispatch_id: str = ""
    pr_number: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HeadlessReviewReceipt":
        return cls(
            gate=str(d.get("gate") or ""),
            pr_id=str(d.get("pr_id") or ""),
            branch=str(d.get("branch") or ""),
            status=str(d.get("status") or ""),
            summary=str(d.get("summary") or ""),
            contract_hash=str(d.get("contract_hash") or ""),
            report_path=str(d.get("report_path") or ""),
            blocking_findings=list(d.get("blocking_findings") or []),
            advisory_findings=list(d.get("advisory_findings") or []),
            blocking_count=int(d.get("blocking_count") or 0),
            advisory_count=int(d.get("advisory_count") or 0),
            required_reruns=list(d.get("required_reruns") or []),
            residual_risk=str(d.get("residual_risk") or ""),
            recorded_at=str(d.get("recorded_at") or ""),
            verdict=str(d.get("verdict") or ""),
            dispatch_id=str(d.get("dispatch_id") or ""),
            pr_number=d.get("pr_number"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "gate": self.gate,
            "pr_id": self.pr_id,
            "branch": self.branch,
            "status": self.status,
            "summary": self.summary,
            "contract_hash": self.contract_hash,
            "report_path": self.report_path,
            "blocking_findings": self.blocking_findings,
            "advisory_findings": self.advisory_findings,
            "blocking_count": len(self.blocking_findings),
            "advisory_count": len(self.advisory_findings),
            "required_reruns": self.required_reruns,
            "residual_risk": self.residual_risk,
            "recorded_at": self.recorded_at,
        }
        if self.verdict:
            d["verdict"] = self.verdict
        if self.dispatch_id:
            d["dispatch_id"] = self.dispatch_id
        if self.pr_number is not None:
            d["pr_number"] = self.pr_number
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> "HeadlessReviewReceipt":
        return cls.from_dict(json.loads(text))

    def is_pass(self) -> bool:
        if self.blocking_findings:
            return False
        effective_status = self.status or self.verdict
        return effective_status == "pass"

    def is_contradictory(self) -> bool:
        return self.status == "pass" and bool(self.blocking_findings)


def validate_gate_result(d: Dict[str, Any]) -> List[ValidationError]:
    errors: List[ValidationError] = []

    for f in REQUIRED_FIELDS:
        if f not in d:
            errors.append(ValidationError(
                field=f,
                severity="error",
                message=f"Required field '{f}' is missing",
            ))

    for f in _IDENTITY_FIELDS:
        val = d.get(f)
        if val is not None and str(val).strip() == "":
            errors.append(ValidationError(
                field=f,
                severity="error",
                message=f"Identity field '{f}' must not be empty",
            ))

    status = str(d.get("status") or "").strip()
    if status and status not in VALID_STATUSES:
        errors.append(ValidationError(
            field="status",
            severity="warning",
            message=f"Unrecognized status '{status}'; expected one of {sorted(VALID_STATUSES)}",
        ))

    # report_path required for pass/fail; not required for blocked
    report_path = str(d.get("report_path") or "").strip()
    if not report_path and status not in ("blocked", "pending", "not_configured", "configured_dry_run", ""):
        errors.append(ValidationError(
            field="report_path",
            severity="error",
            message="report_path is required for non-blocked results",
        ))

    # contract_hash required for pass
    contract_hash = str(d.get("contract_hash") or "").strip()
    if not contract_hash and status == "pass":
        errors.append(ValidationError(
            field="contract_hash",
            severity="warning",
            message="contract_hash is empty for a passing gate",
        ))

    # Contradictory: pass + blocking findings
    blocking = d.get("blocking_findings")
    if isinstance(blocking, list) and blocking and status == "pass":
        errors.append(ValidationError(
            field="status",
            severity="warning",
            message="contradictory: status is pass but blocking_findings are present",
        ))
    elif not isinstance(blocking, list) and blocking is not None:
        errors.append(ValidationError(
            field="blocking_findings",
            severity="error",
            message="blocking_findings must be a list",
        ))

    # Count mismatch warning
    blocking_list = d.get("blocking_findings") if isinstance(d.get("blocking_findings"), list) else []
    blocking_count = d.get("blocking_count")
    if blocking_count is not None and isinstance(blocking_list, list):
        if int(blocking_count) != len(blocking_list):
            errors.append(ValidationError(
                field="blocking_count",
                severity="warning",
                message=f"blocking_count ({blocking_count}) does not match len(blocking_findings) ({len(blocking_list)})",
            ))

    return errors


def validate_report_path_exists(path: str) -> Optional[ValidationError]:
    if not path or not path.strip():
        return ValidationError(
            field="report_path",
            severity="error",
            message="report_path is empty",
        )
    p = Path(path)
    if not p.exists():
        return ValidationError(
            field="report_path",
            severity="error",
            message=f"report_path does not exist: {path}",
        )
    if not p.is_file():
        return ValidationError(
            field="report_path",
            severity="error",
            message=f"report_path is not a file: {path}",
        )
    return None


def normalize_gate_result(
    d: Dict[str, Any],
    *,
    report_path: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(_DEFAULT_EMPTY)
    result.update(d)

    # Inject report_path kwarg only if not already set
    if report_path and not result.get("report_path"):
        result["report_path"] = report_path

    # Reconcile counts from actual lists
    blocking = result.get("blocking_findings")
    advisory = result.get("advisory_findings")
    if isinstance(blocking, list):
        result["blocking_count"] = len(blocking)
    if isinstance(advisory, list):
        result["advisory_count"] = len(advisory)

    return result


__all__ = [
    "REQUIRED_FIELDS",
    "SCHEMA_VERSION",
    "VALID_STATUSES",
    "HeadlessReviewReceipt",
    "ValidationError",
    "normalize_gate_result",
    "validate_gate_result",
    "validate_report_path_exists",
]
