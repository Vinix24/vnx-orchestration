# report-wrapper-machine-readable: Reports carry their own identity

Generic report wrappers identify the dispatch and provider, include the model when known, and omit the echoed instruction that could confuse verdict parsers. Headless Claude workers receive the resolved report destination. Governance can recover a validated worker report from the worktree or repository and records its relocation instead of replacing it with a synthesized wrapper.

## How

Use the [receipt pipeline runbook](../operations/RECEIPT_PIPELINE.md#the-query-interface-adr-035-5) to find the dispatch’s report and receipt. Check the identity block and any `report_relocated` warning when investigating a missing or synthesized report.

## Modules

- [Receipt pipeline](../modules/receipt-pipeline.md)
- [Headless builder context](../modules/headless-builder-context.md)

## Pull requests

Track closed: 2026-10-24 (UTC).

[#1802](https://github.com/Vinix24/vnx-orchestration/pull/1802), [#1619](https://github.com/Vinix24/vnx-orchestration/pull/1619).
