# subsystem-cockpit

Builds subsystem rows from canonical configuration flags and flagless registry entries for the CLI and dashboard.
The CLI attaches health from registered probes when requested, otherwise from health beacons or the committed ledger's seed values.
Background: [subsystem register](../core/SUBSYSTEMS.md).

## Entry points

- [`vnx_cli/commands/subsystems.py::vnx_subsystems`](../../vnx_cli/commands/subsystems.py)
- [`dashboard/api_subsystems.py::build_rows`](../../dashboard/api_subsystems.py)

## Tests

- [tests/vnx_cli/test_subsystems.py](../../tests/vnx_cli/test_subsystems.py)
- [tests/dashboard/test_api_subsystems.py](../../tests/dashboard/test_api_subsystems.py)

## Decisions

[ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md), [ADR-079](../governance/decisions/ADR-079-documentatie-volgt-de-code.md)

## Flag

—
