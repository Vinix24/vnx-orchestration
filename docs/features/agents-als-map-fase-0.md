# agents-als-map-fase-0: Context isolation and receipt identity

GLM and DeepSeek harness lanes run with separate configuration so personal instructions, memory and skills are not inherited. Dispatch receipts carry the assigned role, and the session analyzer labels origin and defers restricted content when its permitted analysis lane cannot process it.

## How

Inspect identity using the [receipt runbook](../operations/RECEIPT_PIPELINE.md#the-query-interface-adr-035-5), and use the [analyzer health guide](../operations/CONVERSATION_ANALYZER_HEALTH.md) to investigate deferred or failed analysis. The [provider lanes](../core/PROVIDER_LANES.md) document execution boundaries.

## Modules

- [Provider routing](../modules/provider-routing.md)
- [Receipt pipeline](../modules/receipt-pipeline.md)
- [Worker permissions](../modules/worker-permissions.md)

## Pull requests

Track closed: 2026-10-15 (UTC).

[#2026](https://github.com/Vinix24/vnx-orchestration/pull/2026), [#2028](https://github.com/Vinix24/vnx-orchestration/pull/2028), [#2031](https://github.com/Vinix24/vnx-orchestration/pull/2031), [#2032](https://github.com/Vinix24/vnx-orchestration/pull/2032).
