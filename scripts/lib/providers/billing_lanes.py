"""billing_lanes.py — canonical billing classification for provider lanes.

SSOT for which provider lanes bill as subscription/OAuth-key (no per-token
metered cost lands against this account) vs genuinely API-metered lanes.
Consumed by dispatch_plan.py's D2 billing rule so cost-tracking and the
per-provider quota cap see the real lane instead of a blanket
"everything non-claude is metered" label.

Each entry ties back to a provider_constraints.yaml guard-rail so the
classification and the routing constraint never drift apart:
  - "kimi": kimi-via-cli-only — CLI OAuth login (`kimi login`), no per-token
    billing surfaces to this account.
  - "glm-harness" / "litellm:zai": glm-via-harness-only — GLM runs via the
    claude-CLI harness -> local litellm proxy -> OpenRouter on an
    already-provisioned key; litellm:zai is the alias a legacy caller may
    still present before the door normalizes it to glm-harness.
  - "deepseek-harness": deepseek-harness-subscription-blocked — runs via the
    claude-CLI harness with an own DeepSeek API key + hardening, never the
    production Anthropic OAuth subscription.

Genuinely per-token-metered lanes (codex, gemini, litellm:deepseek,
litellm:moonshot, local-gemma) are deliberately absent — they classify as
"provider_metered" by omission.
"""
from __future__ import annotations

# Provider.value strings (see dispatch_spec.Provider) whose lane is
# subscription/OAuth-key billed rather than genuinely per-token API-metered
# on this account.
SUBSCRIPTION_LANE_PROVIDERS: frozenset[str] = frozenset({
    "kimi",
    "litellm:zai",
    "glm-harness",
    "deepseek-harness",
})


def is_subscription_lane(provider_value: str) -> bool:
    """True if *provider_value* (a Provider.value string) bills as subscription/OAuth-key."""
    return provider_value in SUBSCRIPTION_LANE_PROVIDERS
