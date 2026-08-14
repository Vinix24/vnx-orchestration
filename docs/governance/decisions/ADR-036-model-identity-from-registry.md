# ADR-036 — Model identity from the registry, not Python literals

**Status:** Accepted (operator-akkoord 2026-08-14; decision-only — this ADR records the ruling, it ships no code)
**Date:** 2026-08-14
**Decided by:** Operator (Vincent van Deth). Grounded in a measurement on `main` `75e1b54c` over 100 real staged bundles (OI-1193).
**Resolves / Cross-refs:** OI-1193 (the measurement in §Context), OI-1187 and OI-1188 (the open router blockades), the `smart-routing-cluster` track (points 8 and 10), and the `bench-v2-model-refresh` track. Continues the model-identity SSOT direction begun in `dispatch-20260802-model-ssot-en-ketenlink`, which made `scripts/lib/providers/wave7_models.yaml` the canonical registry (see the comment in `tier_routing.py:14-19`).

## Context

There are two routers, and the door runs on the inflexible one.

**Router 1 — dynamic, config-driven.** `scripts/lib/smart_router.py` plus `scripts/lib/providers/routing_recommendations.yaml` (595 lines, 7 task classes). Adding a model is a YAML rule. `_compute_quality_tier(composite_score, cost_tier)` (`smart_router.py:202`) derives the tier from the score, so the ordering shifts with the data. `_cost_aware_sort_key` (`smart_router.py:217`) is the operator's 2026-06-28 choice: at or above `_CAPABILITY_THRESHOLD = 7.0` (`smart_router.py:199`) candidates compete on cost, below it on capability, and an unmeasured cost ranks as `+inf` so "unmeasured" is never treated as "free". Callers: `subprocess_dispatch.py:567`, `provider_dispatch.py:2582`, `report_to_receipt_converter.py:569`, and `dispatch_cli.py:1341` (`classify_task` only).

**Router 2 — static, hard-coded.** `scripts/lib/providers/smart_router/tier_routing.py` plus `door_routing.py`. `_ROUTE_MID` (`tier_routing.py:83`) and `_ROUTE_HIGH` (`tier_routing.py:90`) are dataclass literals with model strings in Python. Tier-zero and tier-low build their `TierRoute` in-line (`tier_routing.py:117-137`) with `deepseek-v4-flash` and `gpt-5.5`. `_TIER_PROVIDER_TO_ENUM` (`door_routing.py:32`) is a hand-written dict with four keys. Its caller is `dispatch_cli.py:1061` — the door.

**The door therefore routes on the hard-coded half, and it fails silently.** This is not abstract. Measured on `main` `75e1b54c` (OI-1193), over 100 real staged bundles from `dispatches/pending/` with the actual `instruction.md` (median 3,453 characters) and `provider=AUTO` forced:

```
routed:   23
declined: 77
tier-high 40, of which declined 40
tier-mid  37, of which declined 37
tier-zero 23, of which declined  0
cause:    77x provider='claude' missing from _TIER_PROVIDER_TO_ENUM
```

One missing key in a hand-written translation table wiped 77% of AUTO-routing with no error message, because the path is fail-open (`return None` in `door_routing.py:92-96` and again in the `except` at `door_routing.py:100-111`). `_ROUTE_MID` and `_ROUTE_HIGH` return `provider="claude"`, which `_TIER_PROVIDER_TO_ENUM` does not contain; `.get("claude")` yields `None`, and `resolve_door_route` treats that as "the router declined to route".

**The same shape of fault exists elsewhere.** `scripts/lib/observability_tier.py` omits kimi, glm and deepseek from `ADAPTER_DEFAULT_TIERS` (`observability_tier.py:26`) and `ADAPTER_MINIMUM_TIERS` (`observability_tier.py:35`), so they land on tier 2 via `.get(provider, 2)` (`observability_tier.py:75`) — and `coding-strict`, which requires tier 1, would refuse them. That is the same class of fault on a second site, and it is exactly why this is an ADR and not a one-off bugfix.

**Adding a provider costs at least three Python edits today**, in three places that know nothing about each other: the `TierRoute` literals, `_TIER_PROVIDER_TO_ENUM`, and the two tables in `observability_tier.py`. Every forgotten site fails silently.

**Router 1's data is a generation old.** The provenance note at the top of `routing_recommendations.yaml` says it itself: `composite_score`, `avg_duration_seconds` and `launch_success_rate` are inherited from the 2026-05 field-tests benchmark, which ran on the prior model generation, and the model ids were bumped on 2026-07-22 without a re-benchmark. The candidate lists carry `glm-5` (base) five times (`model_id: glm-5` at lines 67, 189, 288, 386, 501), while `deprecated-glm-models` in `provider_constraints.yaml` explicitly blocks that version. That is the reason the decision below is **not** to wire the door onto router 1.

## Decision

### 1. Model identity and provider strings come from the registry

The static router reads its model identity and provider strings from `scripts/lib/providers/wave7_models.yaml`. There are zero model names and zero provider strings as literals in Python on the routing path. `wave7_models.yaml` already carries the canonical provider → model mapping, per-provider `dispatch_allowed` flags, and the `deprecated_models` list (for zai), so it is the single source of truth this decision points the static path at.

This is deliberately the small, reversible variant. A provider that the registry does not know fails loudly; it does not vanish as a silent `None`.

### 2. Fail-loud on an unknown provider or model

A provider or model the registry does not know fails with an explicit error that names **what** was missing and **where** it was looked for. Never a silent `None`.

The hinge of the whole decision is the distinction between two failure modes that `door_routing.py` currently conflates:

- **A broken classifier fails open — and stays fail-open.** If the cost-tier classifier or the resolver raises (a real bug, a stale `loc_estimate`, an import error), the door still falls through to its existing behavior. A routing *bug* must never block a dispatch.
- **An unknown provider fails loud.** If the classifier worked and produced a provider string that the registry does not know, that is not a routing bug to paper over — it is drift between the router and the registry, and it must surface. The `77x claude missing` measurement is precisely this case being swallowed today.

The two are currently indistinguishable because both funnel into `return None`. The decision keeps the fail-open property for the first (classifier failure) and replaces it with a fail-loud error for the second (unknown provider). The provider → enum translation (`_TIER_PROVIDER_TO_ENUM`) stops being a hand-written dict with a `.get()` fallback and becomes a lookup against the registry that raises with the missing key and the registry path.

### 3. Scope

Files that lose their literals under this decision:

- `scripts/lib/providers/smart_router/tier_routing.py` — the `TierRoute` dataclass literals (`_ROUTE_MID`, `_ROUTE_HIGH`, and the in-line tier-zero/tier-low routes) read `provider` and `model` from the registry instead of hard-coding `"claude"`/`"sonnet-5"`/`"opus-5"`/`"deepseek-v4-flash"`/`"gpt-5.5"`.
- `scripts/lib/providers/smart_router/door_routing.py` — `_TIER_PROVIDER_TO_ENUM` is derived from the registry (or removed in favor of the registry's own provider mapping), and the unknown-provider branch becomes the fail-loud error of §2.
- `scripts/lib/observability_tier.py` — `ADAPTER_DEFAULT_TIERS` and `ADAPTER_MINIMUM_TIERS` are populated from the registry rather than a hand-written dict with a `.get(provider, 2)` default. It falls under the same decision because it is the same class of fault.

Files that are **not** in scope:

- `scripts/lib/smart_router.py` and `scripts/lib/providers/routing_recommendations.yaml` (router 1) are not re-sourced here. Router 1 is already config-driven and carries no Python literals; its problem is stale benchmark data, which is `bench-v2-model-refresh`'s job, not this decision's.
- `scripts/lib/providers/smart_router/cost_tier.py` (the classifier) is untouched — its output is what the fail-loud lookup consumes, and its failure keeps the fail-open property.

### 4. Explicitly not chosen: wiring the door onto router 1

The alternative — connecting the door straight to the dynamic, score-driven router — is possible but not chosen now. Router 1 runs on benchmark data from a prior model generation (the provenance note above), so wiring the door onto it would replace a silent refusal with a *wrong* choice, not a better one. That is the opposite of what this decision wants.

The choice comes back on the table under one condition: a re-benchmark on the current generation. The `bench-v2-model-refresh` track already exists for exactly that. Until it lands and router 1's scores reflect the current models, the door keeps routing on the static (registry-sourced) path, and router 1 stays an opt-in advisory.

## Consequences

- **Positive — a model becomes a registry rule.** Adding a model or a provider is one YAML entry in `wave7_models.yaml`. The three disconnected Python sites no longer each need their own edit, so the "forgotten site fails silently" failure class is gone. The registry is testable and reproducible; the Python literals were neither.
- **Stricter — an unknown provider now blocks.** Where today a missing key silently falls through to the default lane, after this decision a provider the registry does not know is a hard, named error. That is the point: silent degradation becomes visible drift.
- **The introduced risk — a registry error becomes a dispatch blockade.** If the registry is wrong (a typo, a dropped key, a stale entry), a dispatch that used to silently fall back now fails at routing time. That is a real cost of fail-loud. It is bounded because: (a) the failure happens before any work is spawned — the door rejects at routing, no partial dispatch, no receipt of a half-run; (b) the error names the missing key and the registry path, so the fix is a one-line registry edit, not a debugging session; and (c) the fail-open property is preserved for classifier failure, so the only new failure surface is the registry lookup itself, which is deterministic and unit-testable. A registry parse/validation step at load fails on a malformed registry before any dispatch reaches the lookup, so the failure is loud and early rather than discovered mid-dispatch.

## References

- OI-1193 — the AUTO-routing measurement on `main` `75e1b54c` quoted in §Context (the 100-bundle run, 77% silently declined on `provider='claude'` missing from `_TIER_PROVIDER_TO_ENUM`).
- OI-1187 and OI-1188 — the open router blockades this decision is part of resolving.
- `smart-routing-cluster` — points 8 and 10 (the registry-as-SSOT and fail-loud items).
- `bench-v2-model-refresh` — the re-benchmark track that is the precondition for re-opening §4.
- `scripts/lib/providers/wave7_models.yaml` — the canonical registry this decision makes the static router read.
- ADR-015 (Wave 7 provider integration) and the `provider_constraints.yaml` `deprecated-glm-models` constraint — the constraint surface the registry lookup must stay consistent with.
