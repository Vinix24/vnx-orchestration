# context-rotation-plugin: Context handovers for standalone Claude Code

Claude Code users can install context rotation without VNX. The plugin measures context against configurable thresholds, validates a handover and supplies it to the fresh session after a clear. Automatic clearing is available in tmux when its readiness checks pass; elsewhere the user receives a manual instruction.

## How

Use the plugin’s [installation and operation guide](../../packages/claude-code-toolkit/context-rotation/README.md#install), including project-scoped installation, window settings and recovery. The [VNX T0 rotation runbook](../operations/T0_CONTEXT_ROTATION.md) covers the separate orchestrator workflow.

## Modules

- [Related VNX T0 context rotation](../modules/t0-context-rotation.md)

## Pull requests

Track closed: 2026-10-20 (UTC).

[#2064](https://github.com/Vinix24/vnx-orchestration/pull/2064), [#2080](https://github.com/Vinix24/vnx-orchestration/pull/2080), [#2091](https://github.com/Vinix24/vnx-orchestration/pull/2091), [#2090](https://github.com/Vinix24/vnx-orchestration/pull/2090), [#2092](https://github.com/Vinix24/vnx-orchestration/pull/2092), [#2093](https://github.com/Vinix24/vnx-orchestration/pull/2093), [#2094](https://github.com/Vinix24/vnx-orchestration/pull/2094), [#2095](https://github.com/Vinix24/vnx-orchestration/pull/2095), [#2096](https://github.com/Vinix24/vnx-orchestration/pull/2096), [#2089](https://github.com/Vinix24/vnx-orchestration/pull/2089), [#2122](https://github.com/Vinix24/vnx-orchestration/pull/2122), [#2123](https://github.com/Vinix24/vnx-orchestration/pull/2123).
