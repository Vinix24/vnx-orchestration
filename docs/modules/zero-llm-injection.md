# zero-llm-injection

Selects stored intelligence and contextual material for dispatch prompts using deterministic queries, confidence/evidence thresholds and payload limits. The shared injection builder formats the selected items and records their dispatch attribution.

Background: [dispatch intelligence](../core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md#7-intelligence--injection-and-the-self-learning-loop), [intelligence system](../core/technical/INTELLIGENCE_SYSTEM.md).

## Entry points

[`scripts/lib/intelligence_selector.py::IntelligenceSelector.select`](../../scripts/lib/intelligence_selector.py), [`scripts/lib/intelligence_injection.py::fetch_intelligence_section`](../../scripts/lib/intelligence_injection.py).

## Tests

[tests/test_intelligence_selector.py](../../tests/test_intelligence_selector.py), [tests/test_intelligence_injection.py](../../tests/test_intelligence_injection.py).

## Decisions

[ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md), [ADR-069](../governance/decisions/ADR-069-intelligence-effectmeting.md).

## Flag

—
