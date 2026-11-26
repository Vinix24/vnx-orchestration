# dispatch-guard-live-meting: Dispatch decisions use a fresh measurement

The dispatch guard measures live work and the pending queue at the call before deciding GO or WAIT. A snapshot-only response, an incomplete measurement or a measurement older than ten seconds produces WAIT with a reason; work appearing after the last state build therefore remains visible.

## How

Using the engine CLI from the intended project, inspect `vnx status --json --live`; see `vnx status --help` for flags. Run `bash skills/t0-orchestrator/scripts/dispatch_guard.sh json` from the engine checkout for the guard decision, or follow the [dispatch guide](../DISPATCH_GUIDE.md).

## Modules

- [Headless dispatch routing](../modules/headless-dispatch-routing.md)
- [Ledger lifecycle](../modules/ledger-lifecycle.md)

## Pull requests

Track closed: 2026-10-22 (UTC).

[#2166](https://github.com/Vinix24/vnx-orchestration/pull/2166).
