#!/usr/bin/env python3
"""Capability profiles and domain readiness surfaces (Feature 19, PR-2).

Provides an honest onboarding surface for future domain-specific agents so they
can declare what they support without overclaiming coding runtime guarantees.

Components:
  Capability constants     — string keys for each distinct capability
  MaturityLevel            — enum distinguishing authoritative vs experimental
  CapabilityProfile        — immutable capability declaration for a domain
  DomainReadinessSurface   — readiness checks against the authoritative baseline
  coding_authoritative_profile() — canonical VNX coding domain profile
  experimental_profile()   — factory for future experimental domains
  check_readiness()        — stateless helper, returns gap list

Design invariants:
  - CODING_AUTHORITATIVE maturity is the only production-ready maturity level.
  - Experimental domains cannot claim coding-level guarantees regardless of
    which individual capabilities they declare.
  - Capability profiles are frozen and immutable after construction.
  - Gaps are always computed against the authoritative baseline, making
    readiness surfaces explicit rather than inferred.

Usage (coding domain):
    profile = coding_authoritative_profile()
    surface = DomainReadinessSurface(profile)
    assert surface.is_production_ready()
    assert surface.gaps() == []

Usage (future domain):
    profile = experimental_profile(
        domain="content",
        capabilities=frozenset({CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE,
                                 CAP_MANAGER_RELEASE, CAP_MANAGER_QUERY}),
    )
    surface = DomainReadinessSurface(profile)
    assert surface.is_experimental()
    assert not surface.is_production_ready()
    print(surface.gaps())  # lists missing capabilities vs coding baseline
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List


# ---------------------------------------------------------------------------
# Capability constants — manager layer
# ---------------------------------------------------------------------------

CAP_MANAGER_ALLOCATE    = "manager.allocate"
CAP_MANAGER_ADVANCE     = "manager.advance"
CAP_MANAGER_RELEASE     = "manager.release"
CAP_MANAGER_QUERY       = "manager.query"
CAP_MANAGER_PERSISTENCE = "manager.state_persistence"

# ---------------------------------------------------------------------------
# Capability constants — worker layer
# ---------------------------------------------------------------------------

CAP_WORKER_HEARTBEAT        = "worker.heartbeat"
CAP_WORKER_PROGRESS         = "worker.progress_report"
CAP_WORKER_DONE_SIGNAL      = "worker.done_signal"
CAP_WORKER_STATE_TRANSITION = "worker.state_transition"

# ---------------------------------------------------------------------------
# Capability constants — session layer
# ---------------------------------------------------------------------------

CAP_SESSION_LIFECYCLE     = "session.lifecycle_tracking"
CAP_SESSION_RETRY         = "session.attempt_rollover"
CAP_SESSION_TIMEOUT       = "session.timeout_handling"
CAP_SESSION_ABNORMAL_EXIT = "session.abnormal_exit_handling"

# ---------------------------------------------------------------------------
# Capability constants — runtime layer
# ---------------------------------------------------------------------------

CAP_RUNTIME_COORDINATION = "runtime.coordination_db"
CAP_RUNTIME_EVENT_STREAM = "runtime.event_stream"
CAP_RUNTIME_ARTIFACT     = "runtime.artifact_correlation"

# ---------------------------------------------------------------------------
# Capability constants — governance layer
# ---------------------------------------------------------------------------

CAP_GOV_ADVISORY       = "governance.advisory_recommendations"
CAP_GOV_AUTHORITATIVE  = "governance.authoritative_decisions"
CAP_GOV_AUDIT_TRAIL    = "governance.audit_trail"
CAP_GOV_GUARDRAILS     = "governance.guardrail_enforcement"

# ---------------------------------------------------------------------------
# Capability groupings
# ---------------------------------------------------------------------------

MANAGER_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_MANAGER_ALLOCATE, CAP_MANAGER_ADVANCE,
    CAP_MANAGER_RELEASE, CAP_MANAGER_QUERY, CAP_MANAGER_PERSISTENCE,
})

WORKER_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_WORKER_HEARTBEAT, CAP_WORKER_PROGRESS,
    CAP_WORKER_DONE_SIGNAL, CAP_WORKER_STATE_TRANSITION,
})

SESSION_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_SESSION_LIFECYCLE, CAP_SESSION_RETRY,
    CAP_SESSION_TIMEOUT, CAP_SESSION_ABNORMAL_EXIT,
})

RUNTIME_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_RUNTIME_COORDINATION, CAP_RUNTIME_EVENT_STREAM, CAP_RUNTIME_ARTIFACT,
})

GOVERNANCE_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_GOV_ADVISORY, CAP_GOV_AUTHORITATIVE,
    CAP_GOV_AUDIT_TRAIL, CAP_GOV_GUARDRAILS,
})

#: All capabilities expected of a coding-authoritative domain.
CODING_AUTHORITATIVE_CAPABILITIES: FrozenSet[str] = (
    MANAGER_CAPABILITIES
    | WORKER_CAPABILITIES
    | SESSION_CAPABILITIES
    | RUNTIME_CAPABILITIES
    | GOVERNANCE_CAPABILITIES
)


# ---------------------------------------------------------------------------
# Maturity level
# ---------------------------------------------------------------------------

class MaturityLevel(Enum):
    """Domain maturity classification.

    CODING_AUTHORITATIVE — proven, tested, production-stable (the coding domain).
    EXPERIMENTAL         — declared capabilities, not yet at coding maturity.
    PROTOTYPE            — early-stage; capability declarations are aspirational.
    PLANNED              — domain is defined but not yet implemented.
    """
    CODING_AUTHORITATIVE = "coding_authoritative"
    EXPERIMENTAL         = "experimental"
    PROTOTYPE            = "prototype"
    PLANNED              = "planned"


# ---------------------------------------------------------------------------
# Capability profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityProfile:
    """Immutable capability declaration for a domain.

    Attributes:
        domain:       Domain name (e.g., "coding", "content").
        maturity:     Maturity classification — governs readiness semantics.
        capabilities: Set of capability keys this domain declares as supported.
    """
    domain: str
    maturity: MaturityLevel
    capabilities: FrozenSet[str]

    def supports(self, capability: str) -> bool:
        """Return True if this profile declares the given capability."""
        return capability in self.capabilities

    def missing_from(self, baseline: FrozenSet[str]) -> FrozenSet[str]:
        """Return capabilities in baseline that this profile does not declare."""
        return baseline - self.capabilities

    def to_dict(self) -> Dict:
        return {
            "domain": self.domain,
            "maturity": self.maturity.value,
            "capabilities": sorted(self.capabilities),
        }


# ---------------------------------------------------------------------------
# Domain readiness surface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DomainReadinessSurface:
    """Readiness surface for a domain, derived from its capability profile.

    Readiness is determined by maturity level, not just by declared
    capabilities.  A domain that declares all coding capabilities but
    carries EXPERIMENTAL maturity is still not production-ready — maturity
    is an assertion about proven stability, not just a capability checklist.
    """
    profile: CapabilityProfile

    def is_production_ready(self) -> bool:
        """True only when domain maturity is CODING_AUTHORITATIVE."""
        return self.profile.maturity == MaturityLevel.CODING_AUTHORITATIVE

    def is_experimental(self) -> bool:
        """True when domain maturity is EXPERIMENTAL."""
        return self.profile.maturity == MaturityLevel.EXPERIMENTAL

    def gaps(
        self,
        baseline: FrozenSet[str] = CODING_AUTHORITATIVE_CAPABILITIES,
    ) -> List[str]:
        """Sorted list of capabilities in baseline missing from this profile."""
        return sorted(self.profile.missing_from(baseline))

    def readiness_summary(self) -> Dict:
        """Human/machine-readable readiness report for this domain."""
        return {
            "domain": self.profile.domain,
            "maturity": self.profile.maturity.value,
            "production_ready": self.is_production_ready(),
            "capability_count": len(self.profile.capabilities),
            "gaps": self.gaps(),
        }


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def coding_authoritative_profile() -> CapabilityProfile:
    """Return the capability profile for the VNX coding domain.

    This is the authoritative baseline.  All coding runtime, governance,
    session, and manager/worker capabilities are declared and proven.
    """
    return CapabilityProfile(
        domain="coding",
        maturity=MaturityLevel.CODING_AUTHORITATIVE,
        capabilities=CODING_AUTHORITATIVE_CAPABILITIES,
    )


def experimental_profile(
    domain: str,
    capabilities: FrozenSet[str],
) -> CapabilityProfile:
    """Build an EXPERIMENTAL capability profile for a future domain.

    Experimental domains declare what they support without inheriting
    coding-authoritative guarantees automatically.  The maturity level
    prevents is_production_ready() from returning True regardless of
    which capabilities are declared.
    """
    return CapabilityProfile(
        domain=domain,
        maturity=MaturityLevel.EXPERIMENTAL,
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# Stateless helpers
# ---------------------------------------------------------------------------

def check_readiness(profile: CapabilityProfile) -> List[str]:
    """Return sorted gap list for profile vs the coding-authoritative baseline.

    Empty list means the profile declares all authoritative capabilities.
    Non-empty list names each gap explicitly for operator review.
    """
    return DomainReadinessSurface(profile).gaps()
