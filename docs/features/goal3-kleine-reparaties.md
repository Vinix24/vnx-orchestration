# goal3-kleine-reparaties: Explicit deliverable close candidates

A merged PR on a track no longer silently completes that track’s delivery stubs. Operators see close candidates in the drift output and can close a ready delivery with explicit evidence.

## How

Run `vnx objective drift --project-id vnx-dev` (substitute the intended project); see `vnx objective drift --help` for flags. Use `vnx deliverable close --help` for the evidence-bearing close operation. Track-level reconciliation is covered by the [reconcile runbook](../operations/OBJECTIVE_RECONCILE.md).

## Modules

- [Git-grounded reconciliation](../modules/git-grounded-reconcile.md)

## Pull requests

Track closed: 2026-10-15 (UTC).

[#2046](https://github.com/Vinix24/vnx-orchestration/pull/2046), [#2048](https://github.com/Vinix24/vnx-orchestration/pull/2048), [#2047](https://github.com/Vinix24/vnx-orchestration/pull/2047), [#2045](https://github.com/Vinix24/vnx-orchestration/pull/2045).
