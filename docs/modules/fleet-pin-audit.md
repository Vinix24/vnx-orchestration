# fleet-pin-audit

Audits project pins, engine links and the central release store, producing a verdict per project.
Dead or unregistered installations, store findings and measured release lag above the configured threshold make the audit exit 1.

## Entry points

- [`scripts/fleet_pin_audit.py::main`](../../scripts/fleet_pin_audit.py)
- [`scripts/fleet_pin_audit.py::classify_project`](../../scripts/fleet_pin_audit.py)

## Tests

- [tests/test_fleet_pin_audit.py](../../tests/test_fleet_pin_audit.py)

## Decisions

[ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md)

## Flag

`VNX_FLEET_PIN_MAX_LAG_RELEASES`
