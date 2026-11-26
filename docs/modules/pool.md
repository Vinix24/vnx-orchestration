# pool

Manages worker-pool membership and scaling per project and pool.
Each tick reaps dead workers before deciding and executing a scaling action, then records the decision and updates pool size after membership changes.
Background: [parallel execution contract](../contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md).

## Bundle provider and model

[`scripts/lib/pool_worker_runner.py::run`](../../scripts/lib/pool_worker_runner.py)
uses both `target_profile.provider` and `target_profile.model` when the bundle
supplies a whole pair. Neither `--model` nor `VNX_DISPATCH_MODEL` overrides it.
The existing `Executing` delivery log records `bundle_pair: whole`.

During the AG-2p transition, a partial pair keeps the legacy behavior: provider
from the bundle (fallback `kimi`) and model from `--model` (fallback `kimi-k3`).
The runner does not independently read `VNX_DISPATCH_MODEL`. It emits one WARNING
naming the dispatch and missing field, and records `bundle_pair: partial`.
With neither field, the same fallbacks apply without a pair warning and the trace
records `bundle_pair: absent`. Null and empty values count as absent.
Refusing partial pairs with `bundle_pair_incomplete` belongs to AG-2s, after
AG-2c makes producers write whole pairs; AG-2b changes the absent-pair fallback.

## Entry points

- [`scripts/lib/pool_manager.py::PoolManager.tick`](../../scripts/lib/pool_manager.py)
- [`vnx_cli/commands/pool.py::main`](../../vnx_cli/commands/pool.py)

## Tests

- [tests/test_pool_tick_reap_decide_execute.py](../../tests/test_pool_tick_reap_decide_execute.py)
- [tests/test_pool_manager_door_route.py](../../tests/test_pool_manager_door_route.py)
- [tests/test_pool_worker_runner.py](../../tests/test_pool_worker_runner.py)

## Decisions

[ADR-018](../governance/decisions/ADR-018-elastic-worker-pool.md), [ADR-042](../governance/decisions/ADR-042-harness-contract.md)

## Flag

—
