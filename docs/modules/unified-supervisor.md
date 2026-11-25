# unified-supervisor

Enables periodic lease expiry and runtime anomaly checks in the dispatcher when unified mode is selected. The anomaly tick records detected problems and creates open items for blocking anomalies unless disabled.

Background: [runtime supervision contract](../core/131_RUNTIME_STATE_MACHINE_CONTRACT.md), [unified supervisor operations](../operations/UNIFIED_SUPERVISOR.md).

## Entry points

[`scripts/lib/runtime_supervise.py::main`](../../scripts/lib/runtime_supervise.py), [`scripts/lib/lease_sweep.py::run`](../../scripts/lib/lease_sweep.py).

## Tests

[tests/test_runtime_supervise.py](../../tests/test_runtime_supervise.py), [tests/test_lease_sweep.py](../../tests/test_lease_sweep.py), [tests/test_dispatcher_supervisor.py](../../tests/test_dispatcher_supervisor.py).

## Decisions

none

## Flag

`VNX_SUPERVISOR_MODE`
