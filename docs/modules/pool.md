# pool

Manages worker-pool membership and scaling per project and pool.
Each tick reaps dead workers before deciding and executing a scaling action, then records the decision and updates pool size after membership changes.
Background: [parallel execution contract](../contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md).

## Entry points

- [`scripts/lib/pool_manager.py::PoolManager.tick`](../../scripts/lib/pool_manager.py)
- [`vnx_cli/commands/pool.py::main`](../../vnx_cli/commands/pool.py)

## Tests

- [tests/test_pool_tick_reap_decide_execute.py](../../tests/test_pool_tick_reap_decide_execute.py)
- [tests/test_pool_manager_door_route.py](../../tests/test_pool_manager_door_route.py)

## Decisions

[ADR-018](../governance/decisions/ADR-018-elastic-worker-pool.md), [ADR-042](../governance/decisions/ADR-042-harness-contract.md)

## Flag

—
