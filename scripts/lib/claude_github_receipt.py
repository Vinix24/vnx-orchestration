#!/usr/bin/env python3
"""Structured receipt and state model for the optional Claude GitHub review gate.

The Claude GitHub review is optional — it requires a configured gh CLI and an
explicit opt-in env var. This module makes those states explicit and auditable
so that T0 can distinguish:

  - ``not_configured``     — gh CLI or env var not set; review intentionally absent
  - ``configured_dry_run`` — env configured but trigger not set; would run but didn't
  - ``requested``          — gh pr comment was successfully posted
  - ``blocked``            — trigger attempted but gh CLI call failed
  - ``completed``          — a result was recorded (pass / fail / advisory)

Every receipt is linked to a ReviewContract via ``contract_hash`` so T0 can
correlate the GitHub review evidence with the same contract that drove Gemini
and Codex.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Explicit state values — these are the only valid states for a Claude GitHub
# review gate event. Using string constants (not an Enum) for JSON transparency.
STATE_NOT_CONFIGURED = "not_configured"
STATE_CONFIGURED_DRY_RUN = "configured_dry_run"
STATE_REQUESTED = "requested"
STATE_BLOCKED = "blocked"
STATE_COMPLETED = "completed"

VALID_STATES = frozenset([
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_DRY_RUN,
    STATE_REQUESTED,
    STATE_BLOCKED,
    STATE_COMPLETED,
])

# States that indicate the review was intentionally absent (not an error).
INTENTIONALLY_ABSENT_STATES = frozenset([
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_DRY_RUN,
])

# States that indicate the review contributed evidence.
EVIDENCE_STATES = frozenset([
    STATE_REQUESTED,
    STATE_COMPLETED,
])


@dataclass(frozen=True)
class ClaudeGitHubReviewFinding:
    """A single finding from a completed Claude GitHub review result."""

    severity: str  # "blocking" | "advisory"
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
class ClaudeGitHubReviewReceipt:
    """Structured receipt for the optional Claude GitHub review gate.

    This receipt is always present — even when the review was not triggered.
    The ``state`` field makes the absence explicit and auditable.

    Linked to a ReviewContract via ``contract_hash``.
    """

    pr_id: str
    gate: str = "claude_github_optional"
    state: str = STATE_NOT_CONFIGURED
    contract_hash: str = ""
    branch: str = ""
    pr_number: Optional[int] = None
    gh_comment_body: str = ""
    reason: Optional[str] = None

    # Result fields — populated when state == "completed"
    result_status: Optional[str] = None  # "pass" | "fail"
    result_summary: str = ""
    advisory_findings: List[ClaudeGitHubReviewFinding] = field(default_factory=list)
    blocking_findings: List[ClaudeGitHubReviewFinding] = field(default_factory=list)

    requested_at: str = ""
    completed_at: str = ""

    def contributed_evidence(self) -> bool:
        """Return True when this review contributed auditable evidence."""
        return self.state in EVIDENCE_STATES

    def was_intentionally_absent(self) -> bool:
        """Return True when the absence of a review was an explicit governance decision."""
        return self.state in INTENTIONALLY_ABSENT_STATES

    @property
    def advisory_count(self) -> int:
        return len(self.advisory_findings)

    @property
    def blocking_count(self) -> int:
        return len(self.blocking_findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate": self.gate,
            "pr_id": self.pr_id,
            "state": self.state,
            "contributed_evidence": self.contributed_evidence(),
            "was_intentionally_absent": self.was_intentionally_absent(),
            "contract_hash": self.contract_hash,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "gh_comment_body": self.gh_comment_body,
            "reason": self.reason,
            "result_status": self.result_status,
            "result_summary": self.result_summary,
            "advisory_findings": [f.to_dict() for f in self.advisory_findings],
            "blocking_findings": [f.to_dict() for f in self.blocking_findings],
            "advisory_count": self.advisory_count,
            "blocking_count": self.blocking_count,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_request_payload(cls, payload: Dict[str, Any]) -> "ClaudeGitHubReviewReceipt":
        """Construct a receipt from a persisted request payload dict."""
        return cls(
            pr_id=str(payload.get("pr_id", "")),
            gate=str(payload.get("gate", "claude_github_optional")),
            state=str(payload.get("state", STATE_NOT_CONFIGURED)),
            contract_hash=str(payload.get("contract_hash", "")),
            branch=str(payload.get("branch", "")),
            pr_number=payload.get("pr_number"),
            gh_comment_body=str(payload.get("gh_comment_body", "")),
            reason=payload.get("reason"),
            requested_at=str(payload.get("requested_at", "")),
        )

    @classmethod
    def from_result_payload(cls, payload: Dict[str, Any]) -> "ClaudeGitHubReviewReceipt":
        """Construct a receipt from a persisted result payload dict.

        Classifies raw findings (blocking/error → blocking, rest → advisory).
        """
        advisory: List[ClaudeGitHubReviewFinding] = []
        blocking: List[ClaudeGitHubReviewFinding] = []
        for raw in payload.get("findings") or []:
            raw_sev = str(raw.get("severity", "advisory")).lower()
            classified = "blocking" if raw_sev in ("blocking", "error") else "advisory"
            finding = ClaudeGitHubReviewFinding(
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

        return cls(
            pr_id=str(payload.get("pr_id", "")),
            gate=str(payload.get("gate", "claude_github_optional")),
            state=STATE_COMPLETED,
            contract_hash=str(payload.get("contract_hash", "")),
            branch=str(payload.get("branch", "")),
            pr_number=payload.get("pr_number"),
            result_status=str(payload.get("status", "")),
            result_summary=str(payload.get("summary", "")),
            advisory_findings=advisory,
            blocking_findings=blocking,
            requested_at=str(payload.get("requested_at", "")),
            completed_at=str(payload.get("completed_at", "")),
        )
