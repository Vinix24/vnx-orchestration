# agent-layout

Selects `off`, `shadow` or `on` for agent-skill materialisation.
Shadow writes a non-discovered copy; on writes skills to the provider's native discovery location and records materialisation evidence for the receipt.
Unknown values warn and resolve to off; agent-folder resolution is independent of this setting.

## Entry points

- [`scripts/lib/agent_layout.py::layout_mode`](../../scripts/lib/agent_layout.py)

## Tests

- [tests/test_agent_layout.py](../../tests/test_agent_layout.py)
- [tests/test_agent_layout_on.py](../../tests/test_agent_layout_on.py)

## Decisions

[ADR-050](../governance/decisions/ADR-050-agent-layout-fase-1.md), [ADR-059](../governance/decisions/ADR-059-agent-skill-native-materialisation.md)

## Flag

`VNX_AGENT_LAYOUT`
