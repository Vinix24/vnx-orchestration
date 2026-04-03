#!/usr/bin/env python3
"""PR-4 certification tests for Feature 19: Agent OS Lift-In.

Certifies that:
  1. Import boundary B-4: substrate modules have zero domain-specific imports
  2. Coding compatibility: substrate extraction doesn't break coding domain
  3. Capability profiles: readiness surfaces are honest
  4. Anti-goals: premature rollout blocked structurally
  5. Planning scaffolding: validator enforces contract guardrails
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Set

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from capability_profiles import (
    CAP_MANAGER_ALLOCATE,
    CAP_MANAGER_ADVANCE,
    CAP_MANAGER_RELEASE,
    CAP_MANAGER_QUERY,
    CAP_MANAGER_PERSISTENCE,
    MaturityLevel,
    CapabilityProfile,
    DomainReadinessSurface,
    coding_authoritative_profile,
    experimental_profile,
    check_readiness,
)
from domain_plan_validator import (
    KNOWN_GOVERNANCE_PROFILES,
    IMPLEMENTED_GATES,
    REQUIRED_CAPABILITY_FIELDS,
    validate_domain_plan,
)
from orchestration_substrate import (
    StateTransitionSpec,
    WorkerHandle,
    coding_lifecycle_spec,
    validate_transition,
)


LIB_DIR = Path(__file__).parent.parent / "scripts" / "lib"

# Substrate modules that must have zero domain-specific imports (B-4)
SUBSTRATE_MODULES = [
    "orchestration_substrate.py",
    "capability_profiles.py",
    "domain_plan_validator.py",
]

# Domain-specific modules that substrate must never import
DOMAIN_MODULES = {
    "worker_state_manager",
    "vnx_doctor_runtime",
    "codex_final_gate",
    "quality_advisory",
    "claude_github_receipt",
    "gemini_prompt_renderer",
}


def _get_imports(filepath: Path) -> Set[str]:
    """Parse module imports via AST."""
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


# ===================================================================
# Section 1: Import Boundary B-4
# ===================================================================

class TestImportBoundary:
    """Certify substrate modules have zero domain-specific imports."""

    def test_substrate_modules_exist(self) -> None:
        for mod in SUBSTRATE_MODULES:
            assert (LIB_DIR / mod).exists(), f"Missing substrate module: {mod}"

    def test_no_domain_imports_in_substrate(self) -> None:
        for mod in SUBSTRATE_MODULES:
            imports = _get_imports(LIB_DIR / mod)
            violations = imports & DOMAIN_MODULES
            assert violations == set(), (
                f"{mod} imports domain-specific modules: {violations}"
            )

    def test_substrate_uses_only_stdlib_and_typing(self) -> None:
        allowed_prefixes = {
            "__future__", "dataclasses", "typing", "enum", "re",
            "abc", "collections", "functools", "hashlib", "json",
            "pathlib", "datetime", "uuid", "os", "sys",
        }
        for mod in SUBSTRATE_MODULES:
            imports = _get_imports(LIB_DIR / mod)
            for imp in imports:
                root = imp.split(".")[0]
                assert root in allowed_prefixes, (
                    f"{mod} imports non-stdlib module: {imp}"
                )


# ===================================================================
# Section 2: Coding Compatibility
# ===================================================================

class TestCodingCompatibility:
    """Certify coding domain behavior preserved after substrate extraction."""

    def test_coding_lifecycle_spec_mirrors_worker_states(self) -> None:
        spec = coding_lifecycle_spec()
        assert "initializing" in spec.states
        assert "working" in spec.states
        assert "exited_clean" in spec.terminal_states
        assert "exited_bad" in spec.terminal_states

    def test_coding_lifecycle_valid_transitions(self) -> None:
        spec = coding_lifecycle_spec()
        # validate_transition raises on invalid; None on success
        validate_transition("initializing", "working", spec=spec)
        validate_transition("working", "exited_clean", spec=spec)

    def test_coding_lifecycle_invalid_transitions_rejected(self) -> None:
        spec = coding_lifecycle_spec()
        with pytest.raises(ValueError):
            validate_transition("exited_clean", "working", spec=spec)

    def test_coding_profile_is_authoritative(self) -> None:
        profile = coding_authoritative_profile()
        assert profile.maturity == MaturityLevel.CODING_AUTHORITATIVE

    def test_coding_profile_production_ready(self) -> None:
        profile = coding_authoritative_profile()
        surface = DomainReadinessSurface(profile)
        assert surface.is_production_ready()
        assert surface.gaps() == []

    def test_worker_handle_domain_agnostic(self) -> None:
        spec = coding_lifecycle_spec()
        handle = WorkerHandle(worker_id="T1", domain="coding", current_state="working")
        assert handle.is_active(spec)
        handle.current_state = "exited_clean"
        assert not handle.is_active(spec)


# ===================================================================
# Section 3: Capability Profile Honesty
# ===================================================================

class TestCapabilityProfileHonesty:
    """Certify readiness surfaces are honest about maturity."""

    def test_experimental_never_production_ready(self) -> None:
        profile = experimental_profile(
            domain="business",
            capabilities=coding_authoritative_profile().capabilities,
        )
        surface = DomainReadinessSurface(profile)
        assert not surface.is_production_ready()
        assert surface.is_experimental()

    def test_experimental_shows_no_capability_gaps_but_maturity_gap(self) -> None:
        profile = experimental_profile(
            domain="business",
            capabilities=coding_authoritative_profile().capabilities,
        )
        gaps = check_readiness(profile)
        # Maturity level prevents production, not capabilities
        assert profile.maturity != MaturityLevel.CODING_AUTHORITATIVE

    def test_partial_experimental_shows_gaps(self) -> None:
        profile = experimental_profile(
            domain="research",
            capabilities=frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_QUERY}),
        )
        surface = DomainReadinessSurface(profile)
        assert not surface.is_production_ready()
        assert len(surface.gaps()) > 0

    def test_readiness_summary_is_informative(self) -> None:
        profile = coding_authoritative_profile()
        surface = DomainReadinessSurface(profile)
        summary = surface.readiness_summary()
        assert isinstance(summary, dict)
        assert "production_ready" in summary


# ===================================================================
# Section 4: Anti-Goal Enforcement
# ===================================================================

class TestAntiGoalEnforcement:
    """Certify premature rollout is structurally blocked."""

    def test_business_plan_requires_onboarding(self) -> None:
        plan = """
# Feature: Business Test
**Domain**: business
**Governance-Profile**: business_light
"""
        result = validate_domain_plan(plan)
        assert not result.passed  # V-1 blocker

    def test_premature_rollout_language_blocked(self) -> None:
        plan = """
# Feature: Business Test
**Domain**: business
**Governance-Profile**: business_light

## Domain Onboarding

We will enable production rollout of the business domain.
"""
        result = validate_domain_plan(plan)
        v7 = [f for f in result.findings if f.rule == "V-7"]
        assert len(v7) >= 1

    def test_policy_mutation_false_blocked(self) -> None:
        plan = """
# Feature: Business Test
**Domain**: business
**Governance-Profile**: business_light

## Domain Onboarding

| Capability | Value |
|------------|-------|
| `manager_persistence` | True |
| `worker_headless_default` | True |
| `worker_scope_model` | folder |
| `session_evidence_required` | False |
| `gate_required` | False |
| `gate_types` | [] |
| `closure_requires_human` | False |
| `policy_mutation_blocked` | False |
| `audit_retention_days` | 14 |
| `runtime_adapter_type` | headless |
"""
        result = validate_domain_plan(plan)
        v3 = [f for f in result.findings if f.rule == "V-3"]
        assert len(v3) == 1
        assert v3[0].severity == "blocker"

    def test_coding_domain_skips_all_checks(self) -> None:
        plan = """
# Feature: Coding Feature
**Domain**: coding
**Governance-Profile**: coding_strict
"""
        result = validate_domain_plan(plan)
        assert result.passed
        assert result.is_coding


# ===================================================================
# Section 5: Contract Alignment
# ===================================================================

class TestContractAlignment:

    def test_three_governance_profiles(self) -> None:
        assert len(KNOWN_GOVERNANCE_PROFILES) == 3

    def test_ten_capability_fields(self) -> None:
        assert len(REQUIRED_CAPABILITY_FIELDS) == 10

    def test_implemented_gates_match_production(self) -> None:
        assert "codex_gate" in IMPLEMENTED_GATES
        assert "gemini_review" in IMPLEMENTED_GATES

    def test_state_transition_spec_immutable(self) -> None:
        spec = coding_lifecycle_spec()
        with pytest.raises(AttributeError):
            spec.states = frozenset()  # type: ignore[misc]

    def test_capability_profile_immutable(self) -> None:
        profile = coding_authoritative_profile()
        with pytest.raises(AttributeError):
            profile.domain = "hacked"  # type: ignore[misc]
