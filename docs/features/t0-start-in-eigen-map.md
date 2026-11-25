# t0-start-in-eigen-map: T0 rotation starts with its role

After rotation, the successor starts in the project’s T0 folder when its role file exists and its settings link resolves to the project settings. Otherwise the spawner reports the fallback to the project root. The rotation kickoff brings the successor back to its role and current T0 state.

## How

Follow the [T0 rotation and handoff runbook](../operations/T0_CONTEXT_ROTATION.md). Check the successor’s opening and start-folder message before retiring the previous session.

## Modules

- [T0 context rotation](../modules/t0-context-rotation.md)

## Pull requests

Track closed: 2026-10-05 (UTC).

[#2037](https://github.com/Vinix24/vnx-orchestration/pull/2037), [#2042](https://github.com/Vinix24/vnx-orchestration/pull/2042).
