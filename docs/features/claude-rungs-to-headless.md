# claude-rungs-to-headless: Claude workers use the headless lane

Claude workers have one execution lane: headless, with the tmux worker lane and its opt-out removed. A legacy staged specification requesting `force_tmux: true` emits a warning naming the old request and runs headless.

## How

Use the [dispatch guide](../DISPATCH_GUIDE.md#3b-claude-workers-concurrency-and-permissions) for the worker workflow, concurrency and permissions. Inspect worker output through the [event-stream runbook](../operations/EVENT_STREAMS.md).

## Modules

- [Dispatch plan](../modules/dispatch-plan.md)
- [Claude concurrency serialization](../modules/claude-tmux-serialization.md)
- [Event streams](../modules/event-streams.md)

## Pull requests

Track closed: 2026-10-17 (UTC).

[#1868](https://github.com/Vinix24/vnx-orchestration/pull/1868).
