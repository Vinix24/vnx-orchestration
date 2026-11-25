# fabric-state-herstel: Dispatch outcomes and live work

Operators see outcomes grouped per dispatch, so bookkeeping events no longer bury failed work. The T0 overview derives running work from dispatch records and worktree liveness, and exposes unresolved outcomes for a decision.

## How

Use the [receipt query runbook](../operations/RECEIPT_PIPELINE.md#the-query-interface-adr-035-5) to inspect the digest and open outcomes for the intended project. The [state fabric](../core/STATE_FABRIC.md) describes the T0 overview.

## Modules

- [Receipt pipeline](../modules/receipt-pipeline.md)
- [Ledger lifecycle](../modules/ledger-lifecycle.md)

## Pull requests

Track closed: 2026-10-03 (UTC).

[#1974](https://github.com/Vinix24/vnx-orchestration/pull/1974), [#1983](https://github.com/Vinix24/vnx-orchestration/pull/1983), [#1979](https://github.com/Vinix24/vnx-orchestration/pull/1979), [#1975](https://github.com/Vinix24/vnx-orchestration/pull/1975), [#1981](https://github.com/Vinix24/vnx-orchestration/pull/1981), [#1980](https://github.com/Vinix24/vnx-orchestration/pull/1980), [#1990](https://github.com/Vinix24/vnx-orchestration/pull/1990), [#1987](https://github.com/Vinix24/vnx-orchestration/pull/1987), [#1984](https://github.com/Vinix24/vnx-orchestration/pull/1984), [#1999](https://github.com/Vinix24/vnx-orchestration/pull/1999), [#1992](https://github.com/Vinix24/vnx-orchestration/pull/1992), [#2000](https://github.com/Vinix24/vnx-orchestration/pull/2000), [#2008](https://github.com/Vinix24/vnx-orchestration/pull/2008), [#2010](https://github.com/Vinix24/vnx-orchestration/pull/2010).
