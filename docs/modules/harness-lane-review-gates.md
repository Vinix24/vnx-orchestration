# harness-lane-review-gates

Resolves the diff budget shared by the Kimi, GLM and DeepSeek review gates at runtime, with a separate configurable cap for each gate. The default is 800,000 characters; larger values are clamped to the argv delivery ceiling and invalid or non-positive values use the default.

Background: [Provider lanes and review stack](../core/PROVIDER_LANES.md).

## Entry points

[`scripts/lib/gate_lane_contract.py::max_diff_chars`](../../scripts/lib/gate_lane_contract.py).

## Tests

[tests/test_gate_lane_contract.py](../../tests/test_gate_lane_contract.py).

## Decisions

[ADR-008](../governance/decisions/ADR-008-dual-llm-adversarial-review.md), [ADR-042](../governance/decisions/ADR-042-harness-contract.md)

## Flag

`VNX_KIMI_GATE_MAX_DIFF_CHARS`
