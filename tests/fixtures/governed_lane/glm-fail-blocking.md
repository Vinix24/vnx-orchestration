---
schema_version: 1
dispatch_id: glm-gate-pr1855-1789395957
provider: glm-harness
sub_provider: zai
model: glm-5.2
terminal_id: plan-gate
pool_id: headless
role: review-gate
task_class: research_structured
pr_id: none
duration_seconds: 619.832
exit_code: 0
token_usage:
  input: 42844
  output: 4123
  cache_read: 0
cost_usd: 0.0425391
route_decision:
  strategy: default
  selected_provider: glm-harness
  selected_model: glm-5.2
---

# Dispatch glm-gate-pr1855-1789395957

**Dispatch-ID**: glm-gate-pr1855-1789395957
**Model**: glm-5.2
**Provider**: glm-harness
**Terminal**: plan-gate
**Duration**: 619.8s

## Response

Bevestigd. Dit is de status op main (PR #1855 nog niet gemerged). Op main:
- `("zai", "glm-5.2")` staat in `_UNVERIFIED` (regel 280) EN in `_PINNED_PRICES` (regel 256).
- `_UNVERIFIED | _VERIFIED == set(_PINNED_PRICES)` (de split-covers invariant, beweert op regel 356) slaagt.

De diff verwijdert `("zai", "glm-5.2")` uit `_UNVERIFIED` en voegt alle drie de zai-modellen toe aan `_PINNED_PRICES`. Maar de diff voegt ze NIET toe aan `_VERIFIED` (regel 293-296 van de diff voegt alleen een kimi-cli model toe, geen zai-modellen). Dus na deze diff:
- `live_unverified` bevat de zai-modellen NIET (de `price_source` is nu `"OpenRouter /api/v1/models endpoint, verified by operator"`, startend met "OpenRouter" en niet "unverified").
- `test_unverified_set_has_not_grown` slaagt (niets nieuws unverified).
- MAAR `test_the_split_covers_every_model` (`_UNVERIFIED | _VERIFIED == set(_PINNED_PRICES)`) faalt: de drie `("zai", *)` modellen zitten in `_PINNED_PRICES` maar in geen van beide sets.

De modellen die nu een real source citeren hadden in `_VERIFIED` moeten worden opgenomen, of de split-invariant faalt.

```json
{
  "verdict": "fail",
  "findings": [{"severity": "error", "message": "tests/test_price_provenance.py: the three zai models are added to _PINNED_PRICES and removed from _UNVERIFIED, but NOT added to _VERIFIED. test_the_split_covers_every_model (line 356) asserts _UNVERIFIED | _VERIFIED == set(_PINNED_PRICES), which breaks: glm-5.2/glm-5.3/glm-5.3-flash are in _PINNED_PRICES but in neither _UNVERIFIED nor _VERIFIED. The wave7_models.yaml entries now carry price_source 'OpenRouter /api/v1/models endpoint, verified by operator' (not 'unverified'), so they belong in _VERIFIED — add the three ('zai','glm-5.2'), ('zai','glm-5.3'), ('zai','glm-5.3-flash') tuples to _VERIFIED to keep the split invariant and test_verified_prices_did_not_quietly_become_guesses consistent."}],
  "residual_risk": "If the three zai tuples are added to _VERIFIED in the same PR, the invariant is restored; the gate's own model pin (glm_gate.py ALLOWED_MODELS=frozenset({DEFAULT_MODEL})) is unchanged and stays deterministic, so no residual risk there."
}
```
