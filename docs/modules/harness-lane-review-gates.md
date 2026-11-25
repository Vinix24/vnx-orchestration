# harness-lane-review-gates

Resolves the diff budget shared by the Kimi, GLM and DeepSeek review gates at runtime, with a separate configurable cap for each gate. The default is 800,000 characters; larger values are clamped to the argv delivery ceiling and invalid or non-positive values use the default.

Background: [Provider lanes and review stack](../core/PROVIDER_LANES.md).

## PR head and review tools (OI-3084)

GLM (`glm-5.2`), DeepSeek and Claude receive the working-directory check rule;
Kimi remains single-shot. GLM and DeepSeek harness runs with role `review-gate`
reuse `claude_gate_lane.seat_scope_args`: only `Read`, `Grep`, `Glob`, with
`--tools`, `--allowedTools`, the `mcp__*` deny and MCP disabled. Build roles
retain their worker scope.

The standalone governed dispatcher validates and fetches `origin/<PR branch>`
before spawning, as the gate runner does. Missing, unsafe or unfetchable refs
fail before dispatch; there is no fallback to main. `--diff-file` remains an
offline test seam; without a PR ref it cannot launch a real review dispatcher.

The dispatcher's fetch uses the consumer project root supplied by the gate
runner through the dispatch table. Standalone callers without an explicit root
use `resolve_consumer_project_root()` (including the `VNX_PROJECT_ROOT` pin).
The VNX code checkout, which may be a separate central install, is never used
as the default repository for this fetch. Root-resolution failures remain
dispatch failures and prevent the review seat from starting.

Standalone `execution_depth` still measures prompt coverage conservatively:
its legacy `single_shot` mode does not measure GLM's tool activity. Empty and
truncated diff guards remain active; tool access alone never proves coverage.
Usage accounting is separate: `unavailable:no_usage_fields` can come from
gate attempt booking without supplied usage, and does not prove a proxy outage.

## Entry points

[`scripts/lib/gate_lane_contract.py::max_diff_chars`](../../scripts/lib/gate_lane_contract.py).

## Tests

[tests/test_gate_lane_contract.py](../../tests/test_gate_lane_contract.py).

## Decisions

[ADR-008](../governance/decisions/ADR-008-dual-llm-adversarial-review.md), [ADR-042](../governance/decisions/ADR-042-harness-contract.md)

## Flag

`VNX_KIMI_GATE_MAX_DIFF_CHARS`
