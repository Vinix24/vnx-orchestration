# provider-switch

Decides whether a switchable provider family may be called, using an off-only environment override, project setting, global setting, then the default on.
It rereads switch state for each decision and refuses unknown identifiers or unreadable state for switchable families.
Background: [provider switch](../core/PROVIDER_SWITCH.md).

## Entry points

- [`scripts/lib/provider_switch.py::decide`](../../scripts/lib/provider_switch.py)
- [`scripts/lib/provider_switch.py::require`](../../scripts/lib/provider_switch.py)

## Tests

- [tests/test_provider_switch.py](../../tests/test_provider_switch.py)
- [tests/test_provider_switch_router_isolation.py](../../tests/test_provider_switch_router_isolation.py)

## Decisions

[ADR-044](../governance/decisions/ADR-044-provider-schakelaar.md)

## Flag

—
