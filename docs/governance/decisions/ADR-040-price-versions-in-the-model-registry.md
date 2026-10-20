# ADR-040 — Dated price versions in the model registry

**Status:** Accepted
**Date:** 2026-10-19
**Decided by:** Operator (Vincent van Deth), via dispatch `20261019-kp-k1-prijzen` (track `kosten-per-model-in-het-grootboek`, K1 row).
**Resolves / Cross-refs:** ADR-036 (model identity from the registry — this decision extends that registry with the *price* dimension); ADR-005 (cost events are emitted before downstream mutation); the `~/.claude/rules/provider-constraints.md` measurements of 2026-09-14; OI-1335 (price inheritance on version bump) and OI-1361 (no fabricated price on lookup miss).

## Context

Prices lived in two places that could disagree, and one of them was a hard-coded Python table.

**The registry carried flat prices.** `scripts/lib/providers/wave7_models.yaml` (the SSOT per ADR-036) had one `cost_input_per_mtok` / `cost_output_per_mtok` pair per model plus a `price_source` / `price_checked_at` provenance note. It could not express two things the world actually has:

- **A price that changes over time.** A re-measurement rewrites the single flat pair and moves `price_checked_at`, so the *previous* price is lost the moment it is corrected. There is no way to say "opus cost 15/75 until 2026-08-19, then 5/25".
- **A peak/off-peak split.** DeepSeek bills a peak tariff (01:00-04:00 and 06:00-10:00 UTC on weekdays) and half that off-peak. The registry had no field for the split, so it booked only the peak number and silently priced off-peak runs at the upper bound.

**The legacy rate table duplicated the registry and drifted.** `scripts/lib/provider_costs.py` carried `_PROVIDER_RATES`, a hand-written `(provider, model) -> (in, out, is_flat)` table used as the fallback when the registry lookup missed. It was already wrong in ways the registry was not: `codex/gpt-5.5` was 0.25/1.00 in the table against 5.00/30.00 in the registry, and `codex/gpt-5.2-codex` existed only in the table (a retired id absent from the registry). A model not in the table resolved to `None`, which downstream treated as a confirmed `$0` — a silent under-report.

**An alias carried a contradictory price.** `anthropic.opus` and `anthropic.opus-4-8` name the *same* model (`litellm_name: anthropic/claude-opus-4-8`), yet `opus` carried the retired Opus 4.1 price (15.00/75.00) while `opus-4-8` carried 5.00/25.00. This is the OI-1335 inheritance pattern in miniature: the alias kept the predecessor's number after the rename.

**The measured prices existed but were not recorded.** `provider-constraints.md` records, measured live on 2026-09-14: `deepseek-flash` 0.30/1.20 peak and 0.15/0.60 off-peak; `deepseek-v4-pro` 1.32/3.96 peak and 0.66/1.98 off-peak; `z-ai/glm-5.2` 0.68/2.15, `glm-5.3` 1.40/4.40, `glm-5.3-flash` 0.15/0.50. The registry carried a superseded discounted pair (0.435/0.87) for deepseek-v4-pro and no off-peak split for either DeepSeek model.

## Decision

### 1. Each priced model carries dated price versions

Every model in `wave7_models.yaml` gains a `price_versions` list. One version is the fields `date`, `source`, `input_per_mtok`, `output_per_mtok`, and optional `cache_read_per_mtok`, `cache_write_per_mtok`, `off_peak_input_per_mtok`, `off_peak_output_per_mtok`. A version without a date or without numeric input/output rates fails loud at registry load (it names the model) — an undated price is unversioned, and a version without a rate is not a price.

Every alias of a model resolves to the *same* versions. `anthropic.opus` and `anthropic.opus-4-8` now carry identical versions, which removes the 15/75-versus-5/25 contradiction.

The existing flat cost fields remain as a read surface for raw YAML consumers (`cost_loader`, the analyzer smoke test) and are authored equal to the *latest* version. A dedicated drift test asserts that for every model the flat pair equals the latest version, so the two surfaces cannot silently diverge. The forward SSOT is `price_versions`; the flat pair is a projection.

### 2. One function: `price_run(provider, model, segments)`

Pricing goes through a single entry point in `scripts/lib/provider_costs.py`:

```
price_run(provider, model, segments) -> (cost_usd, cost_source)
```

`segments` splits one run at a price boundary. Each segment carries token counts (`input`, `output`, `cache_read`, `cache_write`) and optionally a rate-class selector (`peak` boolean or `ts` timestamp). A run whose segments straddle the DeepSeek peak window is priced with two rates. A segment with neither `peak` nor `ts` defaults to peak — the upper bound, never an under-report.

`cost_source` is a provenance string: `"<model>@<version-date>"` for a priced run. A model without a price returns `cost_usd=None` and `cost_source="unavailable:no_price:<model>"` — never a silent 0.

### 3. `_PROVIDER_RATES` is removed

The hard-coded rate table, its `_lookup_rate`/`_compute_cost_from_rates`/`resolve_cost_usd` helpers, and the fallback they fed are deleted. Every caller (`codex_wrapper`, `gemini_wrapper`, the `provider_dispatch` fallback) prices through `price_run`. The `billing_mode` label in `emit_provider_cost` no longer consults the table; it derives from `cost_usd_estimate` and a small subscription-flat *lane* set (kimi CLI OAuth, local ollama) — a lane property, not a price.

### 4. Prices are measured values with their measurement date

DeepSeek and OpenRouter prices are recorded as measured on 2026-09-14 (`provider-constraints.md`), with that date: `deepseek-flash` (0.30/1.20 peak, 0.15/0.60 off-peak, cache-read 0.006 from the DeepSeek API pricing page), `deepseek-v4-pro` (1.32/3.96 peak, 0.66/1.98 off-peak), and the z-ai/glm models already at their measured values. No price is invented from memory; a model without a measured price is `unavailable`, not a guess.

## Consequences

- **Positive — prices are dated and auditable.** A re-measurement appends a version instead of overwriting history; the receipt's `cost_source` names the version date that produced the number. The alias contradiction and the table/registry drift are gone, because there is now one resolution path.
- **Positive — peak/off-peak is modelled, not bounded.** A DeepSeek run that crosses a price boundary is priced with two rates, so off-peak work is no longer over-billed at the peak tariff.
- **Stricter — an unpriced model is explicit.** Where the rate-table fallback produced `None` that downstream could read as `$0`, `price_run` returns `unavailable:no_price:<model>`. A model dropped from the registry (or a lane the registry does not curate, such as `litellm:openrouter`'s gpt-4o-mini) surfaces as unpriced instead of free.
- **The introduced risk — a table caller breaks.** Any caller that still reaches for the removed rate-table symbols fails at import time, which is the point: the removal is enforced by the language, not by convention. The blast radius was enumerated and each caller migrated in the same change.
- **The introduced risk — two price surfaces.** Keeping the flat pair alongside `price_versions` is a deliberate transitional read surface for raw YAML consumers, and it re-introduces the possibility of drift. It is bounded by the drift test, which fails the moment the flat pair and the latest version disagree.
