#!/usr/bin/env python3
"""Conditional auto-merge policy evaluator for VNX governance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence


HIGH_RISK_PATH_MARKERS = (
    "scripts/dispatcher",
    "scripts/receipt_processor",
    "scripts/pr_queue_manager",
    "scripts/pre_merge_gate",
    "scripts/closure_verifier",
    "scripts/review_gate_manager",
    "scripts/roadmap_manager",
    "scripts/lib/vnx_paths",
    "scripts/commands/start.sh",
    "scripts/commands/stop.sh",
    "scripts/commands/doctor.sh",
    "schemas/",
    ".github/workflows/",
)


@dataclass(frozen=True)
class AutoMergeDecision:
    allowed: bool
    reason: str
    blockers: List[str]


def _normalize_paths(changed_files: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    for path in changed_files:
        p = str(path).strip()
        if not p:
            continue
        normalized.append(Path(p).as_posix())
    return normalized


def codex_final_gate_required(changed_files: Iterable[str]) -> bool:
    for path in _normalize_paths(changed_files):
        if any(marker in path for marker in HIGH_RISK_PATH_MARKERS):
            return True
        if path.endswith(".sql"):
            return True
    return False


def evaluate_auto_merge_policy(
    *,
    risk_class: str,
    merge_policy: str,
    changed_files: Sequence[str],
    gemini_review_passed: bool,
    codex_gate_passed: bool,
    required_checks_passed: bool,
    closure_verifier_passed: bool,
) -> AutoMergeDecision:
    blockers: List[str] = []
    risk = (risk_class or "").strip().lower()
    policy = (merge_policy or "").strip().lower()

    if policy != "conditional_auto":
        blockers.append("merge_policy_not_conditional_auto")
    if risk != "low":
        blockers.append("risk_class_not_low")
    if codex_final_gate_required(changed_files):
        blockers.append("high_risk_change_scope")
    if not gemini_review_passed:
        blockers.append("gemini_review_not_passed")
    if not codex_gate_passed:
        blockers.append("codex_gate_not_passed")
    if not required_checks_passed:
        blockers.append("required_checks_not_passed")
    if not closure_verifier_passed:
        blockers.append("closure_verifier_not_passed")

    if blockers:
        return AutoMergeDecision(
            allowed=False,
            reason="conditional auto-merge denied",
            blockers=blockers,
        )

    return AutoMergeDecision(
        allowed=True,
        reason="conditional auto-merge allowed",
        blockers=[],
    )


__all__ = [
    "AutoMergeDecision",
    "codex_final_gate_required",
    "evaluate_auto_merge_policy",
]
