# control-centre

Shows registered projects with their T0 lease state and heartbeat. Its dispatch command writes an instruction and dispatch metadata to a project's pending queue, then records the forwarding event through the state aggregator.

Background: [Control Centre operations](../operations/CONTROL_CENTRE.md).

## Entry points

[`scripts/control_centre_cli.py::cmd_status`](../../scripts/control_centre_cli.py), [`scripts/control_centre_cli.py::cmd_dispatch`](../../scripts/control_centre_cli.py).

## Tests

[tests/test_control_centre_cli.py](../../tests/test_control_centre_cli.py).

## Decisions

[ADR-017](../governance/decisions/ADR-017-wave5-control-centre.md).

## Flag

—
