#!/usr/bin/env python3
"""Structured review receipt: advisory and blocking findings kept apart.

Builds ReviewReceipt payloads from raw gate findings, keeping advisory findings distinct
from blocking ones so T0 can act on the classification without parsing raw text.
Gate-agnostic: ``gate_result_parser.record_result`` classifies the findings of every
review gate through it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from review_contract import _normalize_line  # canonical line-coercion, never a second copy


@dataclass(frozen=True)
class ReviewFinding:
    """A single finding emitted from a review gate, classified by severity."""

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
class ReviewReceipt:
    """Structured receipt from a review gate.

    advisory_findings and blocking_findings are always separate lists so that
    T0 and downstream gates can act on the classification without re-parsing text.
    """

    pr_id: str
    gate: str = ""
    status: str = "pending"  # "pending" | "pass" | "fail" | "blocked"
    summary: str = ""
    advisory_findings: List[ReviewFinding] = field(default_factory=list)
    blocking_findings: List[ReviewFinding] = field(default_factory=list)
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
    ) -> "ReviewReceipt":
        """Classify raw findings dicts into advisory vs blocking.

        A finding is blocking when its severity is ``"blocking"`` or ``"error"``.
        All other severity values (``"advisory"``, ``"warning"``, ``"info"``, etc.)
        are classified as advisory.
        """
        advisory: List[ReviewFinding] = []
        blocking: List[ReviewFinding] = []

        for raw in raw_findings:
            raw_severity = str(raw.get("severity", "advisory")).lower()
            classified = "blocking" if raw_severity in ("blocking", "error") else "advisory"
            finding = ReviewFinding(
                severity=classified,
                category=str(raw.get("category", "general")),
                message=str(raw.get("message", "")),
                file_path=str(raw.get("file_path", "")),
                line=_normalize_line(raw.get("line", 0)),
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
