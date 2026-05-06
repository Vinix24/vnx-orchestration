#!/usr/bin/env python3
"""gate_stack_resolver.py — Dispatch admission via observability tier gating.

Rejects a dispatch BEFORE subprocess spawn when the governance variant's
min_observability_tier exceeds the adapter's effective observability tier.

Usage::

    from gate_stack_resolver import check_tier_admission, TierAdmissionResult

    result = check_tier_admission("gemini", "coding-strict")
    if result.decision == "reject":
        raise DispatchAdmissionError(result.reason)

BILLING SAFETY: No Anthropic SDK. No network calls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Optional

from observability_tier import (
    ADAPTER_DEFAULT_TIERS,
    GOVERNANCE_MIN_TIERS,
    get_governance_min_tier,
    resolve_effective_tier,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TierAdmissionResult:
    """Result of a tier admission check.

    Attributes:
        decision:          "allow" or "reject"
        provider:          Normalized provider name (e.g. "gemini")
        adapter_tier:      Effective tier for this adapter under current config
        required_tier:     min_observability_tier required by governance_variant
        governance_variant: The governance profile name that was checked
        reason:            Human-readable explanation
    """
    decision: Literal["allow", "reject"]
    provider: str
    adapter_tier: int
    required_tier: int
    governance_variant: str
    reason: str

    def is_allowed(self) -> bool:
        return self.decision == "allow"

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "provider": self.provider,
            "adapter_tier": self.adapter_tier,
            "required_tier": self.required_tier,
            "governance_variant": self.governance_variant,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Admission check
# ---------------------------------------------------------------------------

def check_tier_admission(
    provider: str,
    governance_variant: str = "default",
    *,
    streaming_enabled: bool = True,
    min_tier_override: Optional[int] = None,
) -> TierAdmissionResult:
    """Check whether a provider is admitted for a governance variant.

    Args:
        provider:           Provider name (claude/codex/gemini/litellm/ollama).
        governance_variant: Governance profile name (coding-strict/business-light/...).
        streaming_enabled:  Hint passed to resolve_effective_tier(); controls
                            whether the streaming path is considered active.
        min_tier_override:  Explicit minimum tier override (skips GOVERNANCE_MIN_TIERS lookup).

    Returns:
        TierAdmissionResult with decision=allow when adapter_tier <= required_tier,
        or decision=reject when adapter_tier > required_tier.

    Tier arithmetic: lower numbers are MORE capable (Tier 1 > Tier 2 > Tier 3).
    "reject" means adapter_tier > required_tier (adapter is LESS capable than required).
    """
    provider = provider.lower()
    governance_variant = governance_variant.lower()

    adapter_tier = resolve_effective_tier(provider, streaming_enabled=streaming_enabled)
    required_tier = (
        min_tier_override
        if min_tier_override is not None
        else get_governance_min_tier(governance_variant)
    )

    if adapter_tier <= required_tier:
        reason = (
            f"provider={provider!r} tier={adapter_tier} meets "
            f"governance={governance_variant!r} min_tier={required_tier}"
        )
        return TierAdmissionResult(
            decision="allow",
            provider=provider,
            adapter_tier=adapter_tier,
            required_tier=required_tier,
            governance_variant=governance_variant,
            reason=reason,
        )
    else:
        reason = (
            f"provider={provider!r} tier={adapter_tier} does not meet "
            f"governance={governance_variant!r} min_tier={required_tier}: "
            f"upgrade to a Tier-{required_tier} adapter or lower min_observability_tier"
        )
        return TierAdmissionResult(
            decision="reject",
            provider=provider,
            adapter_tier=adapter_tier,
            required_tier=required_tier,
            governance_variant=governance_variant,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Structured rejection receipt builder
# ---------------------------------------------------------------------------

def build_rejection_receipt(
    result: TierAdmissionResult,
    dispatch_id: str,
    terminal: str = "",
) -> dict:
    """Build a structured rejection receipt for a tier-blocked dispatch.

    Callers write this as an NDJSON record before returning so T0 can
    read it as a failed dispatch.
    """
    import datetime as _dt
    return {
        "event_type": "task_failed",
        "status": "rejected",
        "rejection_reason": "min_observability_tier_violation",
        "dispatch_id": dispatch_id,
        "terminal": terminal,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "tier_admission": result.to_dict(),
    }


# ---------------------------------------------------------------------------
# CLI helper (for `vnx observability gate-check`)
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import sys
    if len(argv) < 3:
        print("Usage: gate_stack_resolver.py <provider> [governance_variant]", file=sys.stderr)
        return 2
    provider = argv[1]
    variant = argv[2] if len(argv) > 2 else "default"
    result = check_tier_admission(provider, variant)
    print(f"decision={result.decision} adapter_tier={result.adapter_tier} "
          f"required_tier={result.required_tier}")
    print(result.reason)
    return 0 if result.is_allowed() else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv))
