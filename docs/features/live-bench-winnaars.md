# live-bench-winnaars: Project-scoped receipts and visible failures

Receipt lookups and pending-item reconciliation select the intended project, including when dispatch IDs collide. Abandoned dispatches and failed worker exits remain open outcomes for T0 to assess. Gate checks also expose stale-head evidence and unreadable blocker sources instead of presenting them as clean.

## How

Use the [receipt query runbook](../operations/RECEIPT_PIPELINE.md#the-query-interface-adr-035-5) for project-scoped lookups and open outcomes. Follow [review gate verification](../operations/T0_RUNBOOKS.md#34-review-gate-verification) when a gate reports missing or stale evidence.

## Modules

- [Receipt pipeline](../modules/receipt-pipeline.md)
- [Governance enforcement](../modules/governance-enforcement-stack.md)
- [Plan-gate panel](../modules/plan-gate-panel.md)

## Pull requests

Track closed: 2026-10-15 (UTC).

[#2022](https://github.com/Vinix24/vnx-orchestration/pull/2022), [#2024](https://github.com/Vinix24/vnx-orchestration/pull/2024), [#2023](https://github.com/Vinix24/vnx-orchestration/pull/2023), [#2021](https://github.com/Vinix24/vnx-orchestration/pull/2021), [#2019](https://github.com/Vinix24/vnx-orchestration/pull/2019), [#2017](https://github.com/Vinix24/vnx-orchestration/pull/2017), [#2016](https://github.com/Vinix24/vnx-orchestration/pull/2016), [#2014](https://github.com/Vinix24/vnx-orchestration/pull/2014), [#2013](https://github.com/Vinix24/vnx-orchestration/pull/2013), [#2012](https://github.com/Vinix24/vnx-orchestration/pull/2012), [#2020](https://github.com/Vinix24/vnx-orchestration/pull/2020), [#2015](https://github.com/Vinix24/vnx-orchestration/pull/2015), [#2018](https://github.com/Vinix24/vnx-orchestration/pull/2018).
