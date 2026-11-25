# state-aggregator

Accepts project state updates and appends their events before updating per-project snapshots and a central project view. Each project snapshot retains the latest 100 events; the central view maintains event counts and last-seen timestamps.

Background: [Control Centre architecture](../governance/decisions/ADR-017-wave5-control-centre.md), [Control Centre operations](../operations/CONTROL_CENTRE.md).

## Entry points

[`scripts/aggregator/state_aggregator.py::StateAggregator.submit`](../../scripts/aggregator/state_aggregator.py), [`scripts/aggregator/state_aggregator.py::StateAggregator.read_central`](../../scripts/aggregator/state_aggregator.py).

## Tests

[tests/test_state_aggregator.py](../../tests/test_state_aggregator.py).

## Decisions

[ADR-005](../governance/decisions/ADR-005-ndjson-audit-ledger-primary.md), [ADR-017](../governance/decisions/ADR-017-wave5-control-centre.md).

## Flag

—
