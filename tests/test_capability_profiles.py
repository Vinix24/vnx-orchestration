#!/usr/bin/env python3
"""Tests for capability profiles and domain readiness surfaces (Feature 19, PR-2).

Covers:
  1. Capability constants  — sanity checks and grouping completeness
  2. MaturityLevel         — enum values and semantics
  3. CapabilityProfile     — construction, supports(), missing_from(), to_dict()
  4. DomainReadinessSurface — is_production_ready(), is_experimental(), gaps()
  5. coding_authoritative_profile — authoritative baseline invariants
  6. experimental_profile  — experimental domain declaration semantics
  7. check_readiness helper — stateless gap computation
  8. Readiness honesty      — maturity governs readiness, not capability count
  9. Cross-domain comparisons — coding vs experimental distinctions
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import FrozenSet

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from capability_profiles import (
    CAP_GOV_ADVISORY,
    CAP_GOV_AUDIT_TRAIL,
    CAP_GOV_AUTHORITATIVE,
    CAP_GOV_GUARDRAILS,
    CAP_MANAGER_ADVANCE,
    CAP_MANAGER_ALLOCATE,
    CAP_MANAGER_PERSISTENCE,
    CAP_MANAGER_QUERY,
    CAP_MANAGER_RELEASE,
    CAP_RUNTIME_ARTIFACT,
    CAP_RUNTIME_COORDINATION,
    CAP_RUNTIME_EVENT_STREAM,
    CAP_SESSION_ABNORMAL_EXIT,
    CAP_SESSION_LIFECYCLE,
    CAP_SESSION_RETRY,
    CAP_SESSION_TIMEOUT,
    CAP_WORKER_DONE_SIGNAL,
    CAP_WORKER_HEARTBEAT,
    CAP_WORKER_PROGRESS,
    CAP_WORKER_STATE_TRANSITION,
    CODING_AUTHORITATIVE_CAPABILITIES,
    GOVERNANCE_CAPABILITIES,
    MANAGER_CAPABILITIES,
    RUNTIME_CAPABILITIES,
    SESSION_CAPABILITIES,
    WORKER_CAPABILITIES,
    CapabilityProfile,
    DomainReadinessSurface,
    MaturityLevel,
    check_readiness,
    coding_authoritative_profile,
    experimental_profile,
)


# ---------------------------------------------------------------------------
# 1. Capability constants
# ---------------------------------------------------------------------------

class TestCapabilityConstants:

    def test_manager_capabilities_complete(self) -> None:
        assert CAP_MANAGER_ALLOCATE in MANAGER_CAPABILITIES
        assert CAP_MANAGER_ADVANCE in MANAGER_CAPABILITIES
        assert CAP_MANAGER_RELEASE in MANAGER_CAPABILITIES
        assert CAP_MANAGER_QUERY in MANAGER_CAPABILITIES
        assert CAP_MANAGER_PERSISTENCE in MANAGER_CAPABILITIES
        assert len(MANAGER_CAPABILITIES) == 5

    def test_worker_capabilities_complete(self) -> None:
        assert CAP_WORKER_HEARTBEAT in WORKER_CAPABILITIES
        assert CAP_WORKER_PROGRESS in WORKER_CAPABILITIES
        assert CAP_WORKER_DONE_SIGNAL in WORKER_CAPABILITIES
        assert CAP_WORKER_STATE_TRANSITION in WORKER_CAPABILITIES
        assert len(WORKER_CAPABILITIES) == 4

    def test_session_capabilities_complete(self) -> None:
        assert CAP_SESSION_LIFECYCLE in SESSION_CAPABILITIES
        assert CAP_SESSION_RETRY in SESSION_CAPABILITIES
        assert CAP_SESSION_TIMEOUT in SESSION_CAPABILITIES
        assert CAP_SESSION_ABNORMAL_EXIT in SESSION_CAPABILITIES
        assert len(SESSION_CAPABILITIES) == 4

    def test_runtime_capabilities_complete(self) -> None:
        assert CAP_RUNTIME_COORDINATION in RUNTIME_CAPABILITIES
        assert CAP_RUNTIME_EVENT_STREAM in RUNTIME_CAPABILITIES
        assert CAP_RUNTIME_ARTIFACT in RUNTIME_CAPABILITIES
        assert len(RUNTIME_CAPABILITIES) == 3

    def test_governance_capabilities_complete(self) -> None:
        assert CAP_GOV_ADVISORY in GOVERNANCE_CAPABILITIES
        assert CAP_GOV_AUTHORITATIVE in GOVERNANCE_CAPABILITIES
        assert CAP_GOV_AUDIT_TRAIL in GOVERNANCE_CAPABILITIES
        assert CAP_GOV_GUARDRAILS in GOVERNANCE_CAPABILITIES
        assert len(GOVERNANCE_CAPABILITIES) == 4

    def test_authoritative_baseline_is_union_of_all_groups(self) -> None:
        union = (MANAGER_CAPABILITIES | WORKER_CAPABILITIES
                 | SESSION_CAPABILITIES | RUNTIME_CAPABILITIES
                 | GOVERNANCE_CAPABILITIES)
        assert CODING_AUTHORITATIVE_CAPABILITIES == union

    def test_authoritative_baseline_total_count(self) -> None:
        assert len(CODING_AUTHORITATIVE_CAPABILITIES) == 20

    def test_capability_constants_are_namespaced_strings(self) -> None:
        for cap in CODING_AUTHORITATIVE_CAPABILITIES:
            assert "." in cap, f"Capability key should be namespaced: {cap!r}"

    def test_groups_are_disjoint(self) -> None:
        groups = [MANAGER_CAPABILITIES, WORKER_CAPABILITIES,
                  SESSION_CAPABILITIES, RUNTIME_CAPABILITIES,
                  GOVERNANCE_CAPABILITIES]
        seen: set = set()
        for group in groups:
            for cap in group:
                assert cap not in seen, f"Capability appears in multiple groups: {cap!r}"
                seen.add(cap)


# ---------------------------------------------------------------------------
# 2. MaturityLevel
# ---------------------------------------------------------------------------

class TestMaturityLevel:

    def test_all_levels_defined(self) -> None:
        levels = {m.value for m in MaturityLevel}
        assert "coding_authoritative" in levels
        assert "experimental" in levels
        assert "prototype" in levels
        assert "planned" in levels

    def test_coding_authoritative_is_distinct(self) -> None:
        assert MaturityLevel.CODING_AUTHORITATIVE != MaturityLevel.EXPERIMENTAL
        assert MaturityLevel.CODING_AUTHORITATIVE != MaturityLevel.PROTOTYPE
        assert MaturityLevel.CODING_AUTHORITATIVE != MaturityLevel.PLANNED

    def test_enum_value_is_lowercase_string(self) -> None:
        for level in MaturityLevel:
            assert level.value == level.value.lower()


# ---------------------------------------------------------------------------
# 3. CapabilityProfile
# ---------------------------------------------------------------------------

class TestCapabilityProfile:

    def _minimal_profile(self, maturity: MaturityLevel = MaturityLevel.EXPERIMENTAL) -> CapabilityProfile:
        return CapabilityProfile(
            domain="test",
            maturity=maturity,
            capabilities=frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE}),
        )

    def test_construction(self) -> None:
        p = self._minimal_profile()
        assert p.domain == "test"
        assert p.maturity == MaturityLevel.EXPERIMENTAL

    def test_supports_declared_capability(self) -> None:
        p = self._minimal_profile()
        assert p.supports(CAP_MANAGER_ALLOCATE) is True

    def test_does_not_support_undeclared_capability(self) -> None:
        p = self._minimal_profile()
        assert p.supports(CAP_MANAGER_RELEASE) is False

    def test_missing_from_returns_absent_caps(self) -> None:
        p = self._minimal_profile()
        baseline = frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE, CAP_MANAGER_RELEASE})
        missing = p.missing_from(baseline)
        assert CAP_MANAGER_RELEASE in missing
        assert CAP_MANAGER_ALLOCATE not in missing

    def test_missing_from_empty_when_profile_covers_baseline(self) -> None:
        caps = frozenset({CAP_MANAGER_ALLOCATE})
        p = CapabilityProfile(domain="x", maturity=MaturityLevel.EXPERIMENTAL, capabilities=caps)
        assert p.missing_from(frozenset({CAP_MANAGER_ALLOCATE})) == frozenset()

    def test_to_dict_includes_required_fields(self) -> None:
        p = self._minimal_profile()
        d = p.to_dict()
        assert d["domain"] == "test"
        assert d["maturity"] == "experimental"
        assert isinstance(d["capabilities"], list)
        assert sorted(d["capabilities"]) == d["capabilities"]  # sorted

    def test_profile_is_frozen(self) -> None:
        p = self._minimal_profile()
        with pytest.raises(Exception):  # FrozenInstanceError
            p.domain = "modified"  # type: ignore[misc]

    def test_profile_with_no_capabilities(self) -> None:
        p = CapabilityProfile(domain="empty", maturity=MaturityLevel.PLANNED,
                               capabilities=frozenset())
        assert p.supports(CAP_MANAGER_ALLOCATE) is False
        assert len(p.missing_from(CODING_AUTHORITATIVE_CAPABILITIES)) == 20


# ---------------------------------------------------------------------------
# 4. DomainReadinessSurface
# ---------------------------------------------------------------------------

class TestDomainReadinessSurface:

    def _surface(self, maturity: MaturityLevel,
                 caps: FrozenSet[str] = frozenset()) -> DomainReadinessSurface:
        return DomainReadinessSurface(
            CapabilityProfile(domain="test", maturity=maturity, capabilities=caps)
        )

    def test_coding_authoritative_is_production_ready(self) -> None:
        s = self._surface(MaturityLevel.CODING_AUTHORITATIVE,
                          caps=CODING_AUTHORITATIVE_CAPABILITIES)
        assert s.is_production_ready() is True

    def test_experimental_is_not_production_ready(self) -> None:
        s = self._surface(MaturityLevel.EXPERIMENTAL,
                          caps=CODING_AUTHORITATIVE_CAPABILITIES)
        assert s.is_production_ready() is False

    def test_prototype_is_not_production_ready(self) -> None:
        s = self._surface(MaturityLevel.PROTOTYPE)
        assert s.is_production_ready() is False

    def test_planned_is_not_production_ready(self) -> None:
        s = self._surface(MaturityLevel.PLANNED)
        assert s.is_production_ready() is False

    def test_experimental_is_experimental(self) -> None:
        s = self._surface(MaturityLevel.EXPERIMENTAL)
        assert s.is_experimental() is True

    def test_coding_authoritative_is_not_experimental(self) -> None:
        s = self._surface(MaturityLevel.CODING_AUTHORITATIVE)
        assert s.is_experimental() is False

    def test_gaps_empty_when_all_authoritative_caps_declared(self) -> None:
        s = self._surface(MaturityLevel.CODING_AUTHORITATIVE,
                          caps=CODING_AUTHORITATIVE_CAPABILITIES)
        assert s.gaps() == []

    def test_gaps_lists_missing_caps(self) -> None:
        s = self._surface(MaturityLevel.EXPERIMENTAL,
                          caps=frozenset({CAP_MANAGER_ALLOCATE}))
        gaps = s.gaps()
        assert len(gaps) == 19  # 20 - 1
        assert CAP_MANAGER_ALLOCATE not in gaps

    def test_gaps_are_sorted(self) -> None:
        s = self._surface(MaturityLevel.EXPERIMENTAL, caps=frozenset())
        gaps = s.gaps()
        assert gaps == sorted(gaps)

    def test_gaps_against_custom_baseline(self) -> None:
        custom = frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE})
        s = self._surface(MaturityLevel.EXPERIMENTAL,
                          caps=frozenset({CAP_MANAGER_ALLOCATE}))
        gaps = s.gaps(baseline=custom)
        assert gaps == [CAP_MANAGER_ADVANCE]

    def test_readiness_summary_structure(self) -> None:
        s = self._surface(MaturityLevel.EXPERIMENTAL,
                          caps=frozenset({CAP_MANAGER_ALLOCATE}))
        summary = s.readiness_summary()
        assert summary["domain"] == "test"
        assert summary["maturity"] == "experimental"
        assert summary["production_ready"] is False
        assert isinstance(summary["gaps"], list)
        assert isinstance(summary["capability_count"], int)

    def test_readiness_summary_production_ready_true_for_authoritative(self) -> None:
        s = self._surface(MaturityLevel.CODING_AUTHORITATIVE,
                          caps=CODING_AUTHORITATIVE_CAPABILITIES)
        assert s.readiness_summary()["production_ready"] is True


# ---------------------------------------------------------------------------
# 5. coding_authoritative_profile
# ---------------------------------------------------------------------------

class TestCodingAuthoritativeProfile:

    def setup_method(self) -> None:
        self.profile = coding_authoritative_profile()

    def test_domain_is_coding(self) -> None:
        assert self.profile.domain == "coding"

    def test_maturity_is_coding_authoritative(self) -> None:
        assert self.profile.maturity == MaturityLevel.CODING_AUTHORITATIVE

    def test_all_authoritative_caps_declared(self) -> None:
        assert self.profile.capabilities == CODING_AUTHORITATIVE_CAPABILITIES

    def test_is_production_ready(self) -> None:
        surface = DomainReadinessSurface(self.profile)
        assert surface.is_production_ready() is True

    def test_no_gaps(self) -> None:
        surface = DomainReadinessSurface(self.profile)
        assert surface.gaps() == []

    def test_supports_all_manager_caps(self) -> None:
        for cap in MANAGER_CAPABILITIES:
            assert self.profile.supports(cap)

    def test_supports_all_worker_caps(self) -> None:
        for cap in WORKER_CAPABILITIES:
            assert self.profile.supports(cap)

    def test_supports_all_session_caps(self) -> None:
        for cap in SESSION_CAPABILITIES:
            assert self.profile.supports(cap)

    def test_supports_all_runtime_caps(self) -> None:
        for cap in RUNTIME_CAPABILITIES:
            assert self.profile.supports(cap)

    def test_supports_all_governance_caps(self) -> None:
        for cap in GOVERNANCE_CAPABILITIES:
            assert self.profile.supports(cap)

    def test_check_readiness_returns_empty(self) -> None:
        assert check_readiness(self.profile) == []


# ---------------------------------------------------------------------------
# 6. experimental_profile
# ---------------------------------------------------------------------------

class TestExperimentalProfile:

    def test_maturity_is_experimental(self) -> None:
        p = experimental_profile("content", frozenset({CAP_MANAGER_ALLOCATE}))
        assert p.maturity == MaturityLevel.EXPERIMENTAL

    def test_domain_name_preserved(self) -> None:
        p = experimental_profile("research", frozenset())
        assert p.domain == "research"

    def test_capabilities_preserved(self) -> None:
        caps = frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE})
        p = experimental_profile("content", caps)
        assert p.capabilities == caps

    def test_is_not_production_ready(self) -> None:
        p = experimental_profile("content", CODING_AUTHORITATIVE_CAPABILITIES)
        surface = DomainReadinessSurface(p)
        assert surface.is_production_ready() is False

    def test_is_experimental(self) -> None:
        p = experimental_profile("content", frozenset())
        surface = DomainReadinessSurface(p)
        assert surface.is_experimental() is True

    def test_manager_only_profile_has_non_manager_gaps(self) -> None:
        p = experimental_profile("content", MANAGER_CAPABILITIES)
        gaps = check_readiness(p)
        for cap in WORKER_CAPABILITIES:
            assert cap in gaps
        for cap in GOVERNANCE_CAPABILITIES:
            assert cap in gaps

    def test_partial_profile_gap_count(self) -> None:
        caps = frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE,
                          CAP_MANAGER_RELEASE, CAP_MANAGER_QUERY})
        p = experimental_profile("content", caps)
        # 20 total - 4 declared = 16 gaps
        assert len(check_readiness(p)) == 16


# ---------------------------------------------------------------------------
# 7. check_readiness helper
# ---------------------------------------------------------------------------

class TestCheckReadiness:

    def test_no_gaps_for_authoritative_profile(self) -> None:
        assert check_readiness(coding_authoritative_profile()) == []

    def test_all_caps_missing_for_empty_profile(self) -> None:
        p = CapabilityProfile(domain="empty", maturity=MaturityLevel.PLANNED,
                               capabilities=frozenset())
        gaps = check_readiness(p)
        assert len(gaps) == 20

    def test_gaps_are_sorted_strings(self) -> None:
        p = experimental_profile("test", frozenset())
        gaps = check_readiness(p)
        assert gaps == sorted(gaps)
        for g in gaps:
            assert isinstance(g, str)

    def test_partial_caps_partial_gaps(self) -> None:
        p = experimental_profile("test", frozenset({CAP_GOV_ADVISORY, CAP_GOV_AUDIT_TRAIL}))
        gaps = check_readiness(p)
        assert CAP_GOV_ADVISORY not in gaps
        assert CAP_GOV_AUDIT_TRAIL not in gaps
        assert CAP_GOV_AUTHORITATIVE in gaps
        assert CAP_GOV_GUARDRAILS in gaps


# ---------------------------------------------------------------------------
# 8. Readiness honesty — maturity governs, not capability count
# ---------------------------------------------------------------------------

class TestReadinessHonesty:

    def test_experimental_with_all_caps_is_not_production_ready(self) -> None:
        """Having all capabilities does not override experimental maturity."""
        p = experimental_profile("content", CODING_AUTHORITATIVE_CAPABILITIES)
        surface = DomainReadinessSurface(p)
        assert surface.is_production_ready() is False
        assert surface.is_experimental() is True

    def test_prototype_with_all_caps_is_not_production_ready(self) -> None:
        p = CapabilityProfile(
            domain="proto", maturity=MaturityLevel.PROTOTYPE,
            capabilities=CODING_AUTHORITATIVE_CAPABILITIES,
        )
        surface = DomainReadinessSurface(p)
        assert surface.is_production_ready() is False

    def test_planned_domain_with_all_caps_is_not_production_ready(self) -> None:
        p = CapabilityProfile(
            domain="future", maturity=MaturityLevel.PLANNED,
            capabilities=CODING_AUTHORITATIVE_CAPABILITIES,
        )
        surface = DomainReadinessSurface(p)
        assert surface.is_production_ready() is False

    def test_coding_authoritative_with_zero_caps_is_production_ready(self) -> None:
        """Maturity level alone determines production readiness gate."""
        p = CapabilityProfile(
            domain="coding", maturity=MaturityLevel.CODING_AUTHORITATIVE,
            capabilities=frozenset(),
        )
        surface = DomainReadinessSurface(p)
        assert surface.is_production_ready() is True

    def test_gaps_present_even_when_experimental_declares_all_caps(self) -> None:
        """Gap check is purely capability-based, independent of maturity."""
        p = experimental_profile("content", CODING_AUTHORITATIVE_CAPABILITIES)
        assert check_readiness(p) == []  # no capability gaps

    def test_readiness_summary_explicitly_includes_maturity(self) -> None:
        p = experimental_profile("content", frozenset())
        summary = DomainReadinessSurface(p).readiness_summary()
        assert summary["maturity"] == "experimental"
        assert summary["production_ready"] is False


# ---------------------------------------------------------------------------
# 9. Cross-domain comparisons
# ---------------------------------------------------------------------------

class TestCrossDomainComparisons:

    def test_coding_and_experimental_have_different_maturity(self) -> None:
        coding = coding_authoritative_profile()
        content = experimental_profile("content", frozenset())
        assert coding.maturity != content.maturity

    def test_coding_profile_has_more_caps_than_minimal_experimental(self) -> None:
        coding = coding_authoritative_profile()
        content = experimental_profile("content", frozenset({CAP_MANAGER_ALLOCATE}))
        assert len(coding.capabilities) > len(content.capabilities)

    def test_two_experimental_profiles_can_differ(self) -> None:
        p1 = experimental_profile("content", frozenset({CAP_MANAGER_ALLOCATE}))
        p2 = experimental_profile("research", frozenset({CAP_WORKER_HEARTBEAT}))
        assert p1.capabilities != p2.capabilities

    def test_substrate_can_hold_multiple_domain_profiles(self) -> None:
        """A registry of profiles is valid and each retains its semantics."""
        profiles = {
            "coding": coding_authoritative_profile(),
            "content": experimental_profile("content", MANAGER_CAPABILITIES),
            "research": experimental_profile("research", frozenset()),
        }
        assert DomainReadinessSurface(profiles["coding"]).is_production_ready()
        assert not DomainReadinessSurface(profiles["content"]).is_production_ready()
        assert not DomainReadinessSurface(profiles["research"]).is_production_ready()

    def test_coding_gaps_empty_others_have_gaps(self) -> None:
        coding = check_readiness(coding_authoritative_profile())
        content = check_readiness(experimental_profile("content", MANAGER_CAPABILITIES))
        assert coding == []
        assert len(content) > 0

    def test_experimental_domain_with_only_governance_caps(self) -> None:
        p = experimental_profile("regulated", GOVERNANCE_CAPABILITIES)
        surface = DomainReadinessSurface(p)
        assert surface.is_experimental()
        gaps = surface.gaps()
        for cap in MANAGER_CAPABILITIES:
            assert cap in gaps
