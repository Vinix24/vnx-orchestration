#!/usr/bin/env python3
"""Canonical review contract schema and serializer.

A ReviewContract is the structured document that every review gate
(Gemini advisory, Codex final gate, Claude GitHub optional) consumes
to produce deliverable-aware reviews. It is materialized deterministically
from FEATURE_PLAN.md, PR_QUEUE.md, changed files, declared tests,
quality gates, and deterministic verifier findings.

The contract is the single source of truth for what a PR promises to
deliver, what it explicitly excludes, and what evidence must be present
before closure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class Deliverable:
    """A single deliverable promised by the PR."""

    description: str
    category: str = "implementation"  # implementation | test | documentation | infrastructure


@dataclass(frozen=True)
class QualityGate:
    """A quality gate checkpoint from the FEATURE_PLAN."""

    gate_id: str
    checks: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class TestEvidence:
    """Declared test evidence for the PR."""

    test_files: List[str] = field(default_factory=list)
    test_command: str = ""
    expected_assertions: int = 0


@dataclass(frozen=True)
class DeterministicFinding:
    """A finding from deterministic verification (linting, type checks, etc.)."""

    source: str
    severity: str  # error | warning | info
    message: str
    file_path: str = ""
    line: int = 0


@dataclass(frozen=True)
class ReviewContract:
    """Canonical review contract for a single PR.

    This is the structured document that every review gate consumes.
    It is deterministic for the same inputs.
    """

    schema_version: str = SCHEMA_VERSION
    pr_id: str = ""
    pr_title: str = ""
    feature_title: str = ""
    branch: str = ""
    track: str = ""
    risk_class: str = "medium"
    merge_policy: str = "human"
    review_stack: List[str] = field(default_factory=list)
    closure_stage: str = "open"  # open | in_review | approved | merged | closed

    deliverables: List[Deliverable] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)
    scope_files: List[str] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)

    quality_gate: Optional[QualityGate] = None
    test_evidence: Optional[TestEvidence] = None
    deterministic_findings: List[DeterministicFinding] = field(default_factory=list)

    dependencies: List[str] = field(default_factory=list)
    estimated_time: str = ""
    dispatch_id: str = ""

    content_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d["quality_gate"] is None:
            d["quality_gate"] = None
        if d["test_evidence"] is None:
            d["test_evidence"] = None
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @staticmethod
    def compute_content_hash(contract_dict: Dict[str, Any]) -> str:
        """Compute a deterministic hash of contract content (excluding the hash field itself)."""
        d = dict(contract_dict)
        d.pop("content_hash", None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReviewContract":
        deliverables = [
            Deliverable(**item) if isinstance(item, dict) else item
            for item in (d.get("deliverables") or [])
        ]
        quality_gate = None
        if d.get("quality_gate"):
            qg = d["quality_gate"]
            quality_gate = QualityGate(
                gate_id=qg["gate_id"],
                checks=list(qg.get("checks") or []),
            )
        test_evidence = None
        if d.get("test_evidence"):
            te = d["test_evidence"]
            test_evidence = TestEvidence(
                test_files=list(te.get("test_files") or []),
                test_command=te.get("test_command", ""),
                expected_assertions=te.get("expected_assertions", 0),
            )
        deterministic_findings = [
            DeterministicFinding(**item) if isinstance(item, dict) else item
            for item in (d.get("deterministic_findings") or [])
        ]
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            pr_id=d.get("pr_id", ""),
            pr_title=d.get("pr_title", ""),
            feature_title=d.get("feature_title", ""),
            branch=d.get("branch", ""),
            track=d.get("track", ""),
            risk_class=d.get("risk_class", "medium"),
            merge_policy=d.get("merge_policy", "human"),
            review_stack=list(d.get("review_stack") or []),
            closure_stage=d.get("closure_stage", "open"),
            deliverables=deliverables,
            non_goals=list(d.get("non_goals") or []),
            scope_files=list(d.get("scope_files") or []),
            changed_files=list(d.get("changed_files") or []),
            quality_gate=quality_gate,
            test_evidence=test_evidence,
            deterministic_findings=deterministic_findings,
            dependencies=list(d.get("dependencies") or []),
            estimated_time=d.get("estimated_time", ""),
            dispatch_id=d.get("dispatch_id", ""),
            content_hash=d.get("content_hash", ""),
        )

    @classmethod
    def from_json(cls, text: str) -> "ReviewContract":
        return cls.from_dict(json.loads(text))


def _parse_pr_section(content: str, pr_id: str) -> Dict[str, Any]:
    """Extract a PR section from FEATURE_PLAN.md content."""
    pattern = re.compile(
        rf"^##\s+{re.escape(pr_id)}:\s*(.+?)$\s*(.*?)(?=^##\s+PR-\d+:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        return {}

    title = match.group(1).strip()
    body = match.group(2)

    result: Dict[str, Any] = {"title": title}

    for field_name in (
        "Track", "Priority", "Complexity", "Risk", "Skill",
        "Requires-Model", "Risk-Class", "Merge-Policy",
        "Review-Stack", "Estimated Time",
    ):
        m = re.search(rf"^\*\*{re.escape(field_name)}\*\*:\s*(.+)$", body, re.MULTILINE)
        if m:
            key = field_name.lower().replace("-", "_").replace(" ", "_")
            result[key] = m.group(1).strip()

    deps_match = re.search(r"^\*\*Dependencies\*\*:\s*\[([^\]]*)\]", body, re.MULTILINE)
    if deps_match:
        raw = deps_match.group(1).strip()
        result["dependencies"] = [d.strip() for d in raw.split(",") if d.strip()] if raw else []

    desc_match = re.search(r"###\s+Description\s*\n(.*?)(?=###|\Z)", body, re.DOTALL)
    if desc_match:
        result["description"] = desc_match.group(1).strip()

    scope_match = re.search(r"###\s+Scope\s*\n(.*?)(?=###|\Z)", body, re.DOTALL)
    if scope_match:
        scope_text = scope_match.group(1).strip()
        result["scope_items"] = [
            line.lstrip("- ").strip()
            for line in scope_text.splitlines()
            if line.strip().startswith("- ")
        ]

    success_match = re.search(r"###\s+Success Criteria\s*\n(.*?)(?=###|\Z)", body, re.DOTALL)
    if success_match:
        criteria_text = success_match.group(1).strip()
        result["success_criteria"] = [
            line.lstrip("- ").strip()
            for line in criteria_text.splitlines()
            if line.strip().startswith("- ")
        ]

    gate_match = re.search(r"###\s+Quality Gate\s*\n`([^`]+)`[:\s]*\n(.*?)(?=---|\Z)", body, re.DOTALL)
    if gate_match:
        gate_id = gate_match.group(1).strip()
        checks_text = gate_match.group(2).strip()
        checks = [
            re.sub(r"^\[[ x]\]\s*", "", line.lstrip("- ").strip())
            for line in checks_text.splitlines()
            if line.strip().startswith("- ")
        ]
        result["quality_gate"] = {"gate_id": gate_id, "checks": checks}

    return result


def _parse_feature_title(content: str) -> str:
    match = re.search(r"^#\s+Feature:\s*(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_pr_queue_status(content: str, pr_id: str) -> str:
    """Determine closure stage from PR_QUEUE.md."""
    if re.search(rf"^\s*-\s+{re.escape(pr_id)}:", content, re.MULTILINE):
        completed_section = re.search(
            r"###\s+.*Completed.*?\n(.*?)(?=###|\Z)", content, re.DOTALL | re.IGNORECASE
        )
        if completed_section and pr_id in completed_section.group(1):
            return "merged"

        active_section = re.search(
            r"###\s+.*Active.*?\n(.*?)(?=###|\Z)", content, re.DOTALL | re.IGNORECASE
        )
        if active_section and pr_id in active_section.group(1):
            return "in_review"

    return "open"


def _derive_non_goals(pr_section: Dict[str, Any], all_pr_ids: List[str], pr_id: str) -> List[str]:
    """Derive non-goals: other PRs in the feature that are explicitly out of scope."""
    non_goals = []
    scope_items = pr_section.get("scope_items") or []
    description = pr_section.get("description", "")
    scope_text = description + "\n" + "\n".join(scope_items)

    for other_id in all_pr_ids:
        if other_id == pr_id:
            continue
        if other_id not in scope_text:
            non_goals.append(f"{other_id} deliverables are out of scope for this PR")

    return non_goals


def _extract_scope_files(scope_items: List[str]) -> List[str]:
    """Extract file paths mentioned in scope items."""
    files = []
    for item in scope_items:
        matches = re.findall(r"`([^`]+\.[a-z]{1,5})`", item)
        files.extend(matches)
        bare_matches = re.findall(r"(?:^|\s)((?:scripts|tests|lib|src|docs)/\S+\.\w+)", item)
        files.extend(bare_matches)
    return sorted(set(files))


def materialize_review_contract(
    *,
    pr_id: str,
    feature_plan_content: str,
    pr_queue_content: str,
    branch: str = "",
    changed_files: Optional[List[str]] = None,
    test_evidence: Optional[TestEvidence] = None,
    deterministic_findings: Optional[List[DeterministicFinding]] = None,
    dispatch_id: str = "",
) -> ReviewContract:
    """Materialize a ReviewContract from source inputs.

    This function is deterministic: the same inputs always produce
    the same contract (including content_hash).
    """
    feature_title = _parse_feature_title(feature_plan_content)
    pr_section = _parse_pr_section(feature_plan_content, pr_id)

    if not pr_section:
        raise ValueError(f"PR section '{pr_id}' not found in FEATURE_PLAN")

    all_pr_ids = re.findall(r"^##\s+(PR-\d+):", feature_plan_content, re.MULTILINE)
    closure_stage = _parse_pr_queue_status(pr_queue_content, pr_id)

    scope_items = pr_section.get("scope_items") or []
    success_criteria = pr_section.get("success_criteria") or []

    deliverables = [
        Deliverable(description=criterion, category="implementation")
        for criterion in success_criteria
    ]

    non_goals = _derive_non_goals(pr_section, all_pr_ids, pr_id)
    scope_files = _extract_scope_files(scope_items)

    review_stack_raw = pr_section.get("review_stack", "")
    review_stack = [s.strip() for s in review_stack_raw.split(",") if s.strip()]

    quality_gate = None
    if pr_section.get("quality_gate"):
        qg = pr_section["quality_gate"]
        quality_gate = QualityGate(gate_id=qg["gate_id"], checks=qg["checks"])

    contract_no_hash = ReviewContract(
        schema_version=SCHEMA_VERSION,
        pr_id=pr_id,
        pr_title=pr_section.get("title", ""),
        feature_title=feature_title,
        branch=branch,
        track=pr_section.get("track", ""),
        risk_class=pr_section.get("risk_class", "medium"),
        merge_policy=pr_section.get("merge_policy", "human"),
        review_stack=review_stack,
        closure_stage=closure_stage,
        deliverables=deliverables,
        non_goals=non_goals,
        scope_files=scope_files,
        changed_files=sorted(changed_files or []),
        quality_gate=quality_gate,
        test_evidence=test_evidence,
        deterministic_findings=list(deterministic_findings or []),
        dependencies=pr_section.get("dependencies") or [],
        estimated_time=pr_section.get("estimated_time", ""),
        dispatch_id=dispatch_id,
        content_hash="",
    )

    content_hash = ReviewContract.compute_content_hash(contract_no_hash.to_dict())

    return ReviewContract(
        schema_version=contract_no_hash.schema_version,
        pr_id=contract_no_hash.pr_id,
        pr_title=contract_no_hash.pr_title,
        feature_title=contract_no_hash.feature_title,
        branch=contract_no_hash.branch,
        track=contract_no_hash.track,
        risk_class=contract_no_hash.risk_class,
        merge_policy=contract_no_hash.merge_policy,
        review_stack=contract_no_hash.review_stack,
        closure_stage=contract_no_hash.closure_stage,
        deliverables=contract_no_hash.deliverables,
        non_goals=contract_no_hash.non_goals,
        scope_files=contract_no_hash.scope_files,
        changed_files=contract_no_hash.changed_files,
        quality_gate=contract_no_hash.quality_gate,
        test_evidence=contract_no_hash.test_evidence,
        deterministic_findings=contract_no_hash.deterministic_findings,
        dependencies=contract_no_hash.dependencies,
        estimated_time=contract_no_hash.estimated_time,
        dispatch_id=contract_no_hash.dispatch_id,
        content_hash=content_hash,
    )


def materialize_from_files(
    *,
    pr_id: str,
    feature_plan_path: Path,
    pr_queue_path: Path,
    branch: str = "",
    changed_files: Optional[List[str]] = None,
    test_evidence: Optional[TestEvidence] = None,
    deterministic_findings: Optional[List[DeterministicFinding]] = None,
    dispatch_id: str = "",
) -> ReviewContract:
    """Convenience wrapper that reads files from disk before materializing."""
    feature_plan_content = feature_plan_path.read_text(encoding="utf-8")
    pr_queue_content = pr_queue_path.read_text(encoding="utf-8")
    return materialize_review_contract(
        pr_id=pr_id,
        feature_plan_content=feature_plan_content,
        pr_queue_content=pr_queue_content,
        branch=branch,
        changed_files=changed_files,
        test_evidence=test_evidence,
        deterministic_findings=deterministic_findings,
        dispatch_id=dispatch_id,
    )
