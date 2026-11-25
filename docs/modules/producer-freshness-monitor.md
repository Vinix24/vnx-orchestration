# producer-freshness-monitor

Checks freshness per producer key against configured cadences and expected keys, distinguishing ongoing producers from one-shot artefacts. The CLI combines the sweep with job-exit harvesting and writes a report and heartbeat unless running read-only.

Background: [producer freshness monitor](../operations/PRODUCER_FRESHNESS_MONITOR.md).

## Entry points

[`scripts/lib/producer_freshness.py::run_sweep`](../../scripts/lib/producer_freshness.py), [`scripts/producer_freshness_monitor.py::main`](../../scripts/producer_freshness_monitor.py).

## Tests

[tests/test_producer_freshness.py](../../tests/test_producer_freshness.py), [tests/test_producer_freshness_store_resolution.py](../../tests/test_producer_freshness_store_resolution.py).

## Decisions

none

## Flag

—
