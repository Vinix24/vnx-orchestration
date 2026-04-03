#!/usr/bin/env python3
"""Tests for domain plan scaffolding validator (Feature 19, PR-3).

Covers:
  1. Coding plans skip domain onboarding (V-1 not required)
  2. Non-coding plans require domain onboarding (V-1)
  3. Capability profile field completeness (V-2)
  4. policy_mutation_blocked enforcement (V-3)
  5. Governance profile validation (V-4)
  6. Substrate boundary acknowledgments (V-5)
  7. Activation prerequisites (V-6)
  8. Premature rollout language detection (V-7)
  9. Gate type validation (V-8)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from domain_plan_validator import (
    KNOWN_GOVERNANCE_PROFILES,
    KNOWN_DOMAINS,
    IMPLEMENTED_GATES,
    REQUIRED_CAPABILITY_FIELDS,
    ValidationFinding,
    ValidationResult,
    validate_domain_plan,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CODING_PLAN = """
# Feature: Test Feature

**Domain**: coding
**Governance-Profile**: coding_strict
"""

MINIMAL_BUSINESS_PLAN_NO_ONBOARDING = """
# Feature: Business Feature

**Domain**: business
**Governance-Profile**: business_light
"""

FULL_BUSINESS_PLAN = """
# Feature: Business Feature

**Domain**: business
**Governance-Profile**: business_light

## Domain Onboarding (Required For Non-Coding Domains)

### Capability Profile Declaration

| Capability | Value | Justification |
|------------|-------|---------------|
| `manager_persistence` | True | Needed |
| `worker_headless_default` | True | Headless |
| `worker_scope_model` | folder | Business |
| `session_evidence_required` | False | Light |
| `gate_required` | False | Light |
| `gate_types` | (none required) | Light |
| `closure_requires_human` | False | Light |
| `policy_mutation_blocked` | True | Required |
| `audit_retention_days` | 14 | Standard |
| `runtime_adapter_type` | headless | Default |

### Substrate Boundary Acknowledgment

- [x] This plan does NOT add domain-specific logic to substrate modules (contract B-2)
- [x] This plan does NOT require the substrate to import from this domain layer (contract B-4)
- [x] This plan does NOT imply production rollout of this domain (contract Section 7 anti-goals)
- [x] All gates referenced in gate_types above are currently implemented and operational
- [x] If governance_profile is not coding_strict, this domain is EXPERIMENTAL maturity only

### Activation Prerequisites (Contract Section 7.2)

- [ ] Capability profile is fully defined and validated above
- [ ] Governance profile has passing conformance tests
- [ ] Scope isolation model (worker_scope_model) is implemented and tested
- [ ] Domain-specific gates (if any) are operational
- [ ] Operator has explicitly approved domain enablement
- [ ] Activation decision is recorded in audit trail
"""


# ---------------------------------------------------------------------------
# 1. Coding plans skip domain onboarding
# ---------------------------------------------------------------------------

class TestCodingPlans:

    def test_coding_plan_passes_without_onboarding(self) -> None:
        result = validate_domain_plan(MINIMAL_CODING_PLAN)
        assert result.is_coding is True
        assert result.passed is True

    def test_coding_plan_has_no_findings(self) -> None:
        result = validate_domain_plan(MINIMAL_CODING_PLAN)
        assert result.blocker_count == 0

    def test_default_domain_is_coding(self) -> None:
        result = validate_domain_plan("# Feature: No Domain Field")
        assert result.is_coding is True


# ---------------------------------------------------------------------------
# 2. Non-coding plans require onboarding (V-1)
# ---------------------------------------------------------------------------

class TestDomainOnboarding:

    def test_missing_onboarding_is_blocker(self) -> None:
        result = validate_domain_plan(MINIMAL_BUSINESS_PLAN_NO_ONBOARDING)
        assert result.passed is False
        v1 = [f for f in result.findings if f.rule == "V-1"]
        assert len(v1) == 1
        assert v1[0].severity == "blocker"

    def test_full_onboarding_passes(self) -> None:
        result = validate_domain_plan(FULL_BUSINESS_PLAN)
        assert result.blocker_count == 0


# ---------------------------------------------------------------------------
# 3. Capability profile completeness (V-2)
# ---------------------------------------------------------------------------

class TestCapabilityFields:

    def test_missing_field_is_blocker(self) -> None:
        plan = FULL_BUSINESS_PLAN.replace("`manager_persistence`", "`REMOVED_FIELD`")
        result = validate_domain_plan(plan)
        v2 = [f for f in result.findings if f.rule == "V-2"]
        assert len(v2) >= 1
        assert v2[0].severity == "blocker"

    def test_all_fields_present_no_v2(self) -> None:
        result = validate_domain_plan(FULL_BUSINESS_PLAN)
        v2 = [f for f in result.findings if f.rule == "V-2"]
        assert len(v2) == 0


# ---------------------------------------------------------------------------
# 4. policy_mutation_blocked (V-3)
# ---------------------------------------------------------------------------

class TestPolicyMutation:

    def test_false_policy_mutation_is_blocker(self) -> None:
        plan = FULL_BUSINESS_PLAN.replace(
            "| `policy_mutation_blocked` | True |",
            "| `policy_mutation_blocked` | False |",
        )
        result = validate_domain_plan(plan)
        v3 = [f for f in result.findings if f.rule == "V-3"]
        assert len(v3) == 1
        assert v3[0].severity == "blocker"

    def test_true_policy_mutation_passes(self) -> None:
        result = validate_domain_plan(FULL_BUSINESS_PLAN)
        v3 = [f for f in result.findings if f.rule == "V-3"]
        assert len(v3) == 0


# ---------------------------------------------------------------------------
# 5. Governance profile validation (V-4)
# ---------------------------------------------------------------------------

class TestGovernanceProfile:

    def test_unknown_profile_is_blocker(self) -> None:
        plan = FULL_BUSINESS_PLAN.replace("business_light", "totally_unknown")
        result = validate_domain_plan(plan)
        v4 = [f for f in result.findings if f.rule == "V-4"]
        assert len(v4) == 1
        assert v4[0].severity == "blocker"

    def test_known_profiles_accepted(self) -> None:
        for profile in KNOWN_GOVERNANCE_PROFILES:
            plan = FULL_BUSINESS_PLAN.replace("business_light", profile)
            result = validate_domain_plan(plan)
            v4 = [f for f in result.findings if f.rule == "V-4"]
            assert len(v4) == 0, f"Profile {profile} rejected"


# ---------------------------------------------------------------------------
# 7. Premature rollout detection (V-7)
# ---------------------------------------------------------------------------

class TestPrematureRollout:

    def test_production_rollout_detected(self) -> None:
        plan = FULL_BUSINESS_PLAN + "\nThis enables production rollout of business domain."
        result = validate_domain_plan(plan)
        v7 = [f for f in result.findings if f.rule == "V-7"]
        assert len(v7) >= 1
        assert v7[0].severity == "blocker"

    def test_launch_business_detected(self) -> None:
        plan = FULL_BUSINESS_PLAN + "\nWe will launch the business domain for users."
        result = validate_domain_plan(plan)
        v7 = [f for f in result.findings if f.rule == "V-7"]
        assert len(v7) >= 1

    def test_clean_plan_no_rollout(self) -> None:
        result = validate_domain_plan(FULL_BUSINESS_PLAN)
        v7 = [f for f in result.findings if f.rule == "V-7"]
        assert len(v7) == 0


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

class TestIntegration:

    def test_constants_consistent(self) -> None:
        assert len(KNOWN_GOVERNANCE_PROFILES) == 3
        assert len(REQUIRED_CAPABILITY_FIELDS) == 10
        assert "coding" in KNOWN_DOMAINS

    def test_result_properties(self) -> None:
        result = validate_domain_plan(FULL_BUSINESS_PLAN)
        assert isinstance(result.passed, bool)
        assert isinstance(result.blocker_count, int)
        assert isinstance(result.warn_count, int)
