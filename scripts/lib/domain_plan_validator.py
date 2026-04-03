#!/usr/bin/env python3
"""Domain plan scaffolding validator (Feature 19, PR-3).

Validates that non-coding domain feature plans reference substrate
boundaries and capability profiles explicitly, and do not imply
premature rollout.

Validation rules:
  V-1: Non-coding plans must contain Domain Onboarding section
  V-2: Capability profile declaration must have all required fields
  V-3: policy_mutation_blocked must be True
  V-4: Governance profile must be one of the known profiles
  V-5: Substrate boundary acknowledgments must be present
  V-6: Activation prerequisites must be present
  V-7: Plan must not contain premature-rollout language
  V-8: gate_types must reference only implemented gates
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_GOVERNANCE_PROFILES = frozenset({
    "coding_strict", "business_light", "regulated_strict",
})

KNOWN_DOMAINS = frozenset({
    "coding", "business", "regulated", "research",
})

IMPLEMENTED_GATES = frozenset({
    "codex_gate", "gemini_review", "claude_github_optional",
})

REQUIRED_CAPABILITY_FIELDS = frozenset({
    "manager_persistence",
    "worker_headless_default",
    "worker_scope_model",
    "session_evidence_required",
    "gate_required",
    "gate_types",
    "closure_requires_human",
    "policy_mutation_blocked",
    "audit_retention_days",
    "runtime_adapter_type",
})

# Language patterns that imply premature production rollout
PREMATURE_ROLLOUT_PATTERNS = [
    r"(?<!NOT imply )(?<!not imply )(?<!NOT\simply\s)production.{1,10}rollout",
    r"(?<!NOT )(?<!not )launch.+business.+domain",
    r"(?<!NOT )(?<!not )activate.+regulated",
    r"(?<!NOT )(?<!not )go.+live.+with",
    r"(?<!NOT )(?<!not )deploy.+to.+production",
    r"general.+availability",
]

BOUNDARY_ACKNOWLEDGMENTS = [
    "does NOT add domain-specific logic to substrate",
    "does NOT require the substrate to import",
    "does NOT imply production rollout",
    "gates referenced.*are currently implemented",
    "EXPERIMENTAL maturity only",
]

ACTIVATION_PREREQUISITES = [
    "Capability profile is fully defined",
    "Governance profile has passing conformance",
    "Scope isolation model.*is implemented",
    "Domain-specific gates.*are operational",
    "Operator has explicitly approved",
    "Activation decision is recorded",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationFinding:
    """A single validation finding."""
    rule: str           # V-1, V-2, etc.
    severity: str       # blocker, warn, info
    message: str
    line_hint: int = 0  # approximate line number (0 = unknown)


@dataclass
class ValidationResult:
    """Result of validating a domain plan."""
    domain: str
    governance_profile: str
    is_coding: bool
    findings: List[ValidationFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity == "blocker" for f in self.findings)

    @property
    def blocker_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "blocker")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def validate_domain_plan(content: str) -> ValidationResult:
    """Validate a domain feature plan against scaffolding rules.

    Args:
        content: Full markdown content of the feature plan.

    Returns:
        ValidationResult with findings.
    """
    domain = _extract_field(content, "Domain") or "coding"
    governance = _extract_field(content, "Governance-Profile") or "coding_strict"
    is_coding = domain == "coding"

    result = ValidationResult(
        domain=domain,
        governance_profile=governance,
        is_coding=is_coding,
    )

    # V-4: Governance profile must be known
    if governance not in KNOWN_GOVERNANCE_PROFILES:
        result.findings.append(ValidationFinding(
            rule="V-4", severity="blocker",
            message=f"Unknown governance profile: {governance}. "
                    f"Must be one of: {', '.join(sorted(KNOWN_GOVERNANCE_PROFILES))}",
        ))

    # Coding plans skip domain onboarding checks
    if is_coding:
        return result

    # V-1: Non-coding plans must have Domain Onboarding section
    if "## Domain Onboarding" not in content:
        result.findings.append(ValidationFinding(
            rule="V-1", severity="blocker",
            message="Non-coding domain plan must contain '## Domain Onboarding' section",
        ))
        return result  # Cannot validate further without onboarding section

    # V-2: Capability profile must have all required fields
    for cap_field in sorted(REQUIRED_CAPABILITY_FIELDS):
        if f"`{cap_field}`" not in content:
            result.findings.append(ValidationFinding(
                rule="V-2", severity="blocker",
                message=f"Missing capability field: {cap_field}",
            ))

    # V-3: policy_mutation_blocked must be True
    if re.search(r"policy_mutation_blocked.*False", content):
        result.findings.append(ValidationFinding(
            rule="V-3", severity="blocker",
            message="policy_mutation_blocked must be True (contract G-5)",
        ))

    # V-5: Substrate boundary acknowledgments
    for pattern in BOUNDARY_ACKNOWLEDGMENTS:
        if not re.search(pattern, content, re.IGNORECASE):
            result.findings.append(ValidationFinding(
                rule="V-5", severity="warn",
                message=f"Missing substrate boundary acknowledgment: {pattern}",
            ))

    # V-6: Activation prerequisites
    for pattern in ACTIVATION_PREREQUISITES:
        if not re.search(pattern, content, re.IGNORECASE):
            result.findings.append(ValidationFinding(
                rule="V-6", severity="warn",
                message=f"Missing activation prerequisite: {pattern}",
            ))

    # V-7: No premature rollout language
    for pattern in PREMATURE_ROLLOUT_PATTERNS:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            result.findings.append(ValidationFinding(
                rule="V-7", severity="blocker",
                message=f"Premature rollout language detected: '{match.group()}'",
            ))

    # V-8: gate_types must reference implemented gates
    gate_match = re.search(r"gate_types.*?\|.*?\|(.*?)\|", content)
    if gate_match:
        gate_text = gate_match.group(1).strip()
        if gate_text and gate_text not in ("(none required)", "[list]", "[]"):
            for gate in re.findall(r"\w+_\w+", gate_text):
                if gate not in IMPLEMENTED_GATES and gate not in REQUIRED_CAPABILITY_FIELDS:
                    result.findings.append(ValidationFinding(
                        rule="V-8", severity="warn",
                        message=f"Gate '{gate}' referenced but not in implemented gates: "
                                f"{', '.join(sorted(IMPLEMENTED_GATES))}",
                    ))

    return result


def _extract_field(content: str, field_name: str) -> Optional[str]:
    """Extract a metadata field value from markdown content."""
    pattern = rf"\*\*{field_name}\*\*:\s*(.+)"
    match = re.search(pattern, content)
    if match:
        return match.group(1).strip()
    return None
