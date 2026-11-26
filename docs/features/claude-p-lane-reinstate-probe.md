# claude-p-lane-reinstate-probe: Headless Claude builds by default

A Claude build without an explicit lane choice resolves to the headless lane and subprocess adapter. The linked delivery changed that default; the track's historical probe title does not describe the full shipped change.

## How

Follow the [dispatch guide](../DISPATCH_GUIDE.md#3b-claude-workers-concurrency-and-permissions) for the current worker workflow and the [event-stream runbook](../operations/EVENT_STREAMS.md) for run output. The later [tmux-lane retirement](claude-rungs-to-headless.md) removed the original opt-out.

## Modules

- [Dispatch plan](../modules/dispatch-plan.md)
- [Headless builder context](../modules/headless-builder-context.md)

## Pull requests

Track closed: 2026-10-17 (UTC).

[#1693](https://github.com/Vinix24/vnx-orchestration/pull/1693).
