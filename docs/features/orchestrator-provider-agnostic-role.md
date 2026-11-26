# orchestrator-provider-agnostic-role: The same T0 role across provider CLIs

Operators can synchronize the canonical orchestrator role into the project’s T0 folder for Claude, Codex, Gemini and Kimi. Synchronization refreshes the role file and marked role blocks in that folder’s AGENTS.md and GEMINI.md; the default is a preview, and applying changes makes backups.

## How

Preview with `vnx role sync --project-dir /path/to/project`, then apply with `vnx role sync --apply --project-dir /path/to/project`. See `vnx role sync --help` for flags; the target project must already have its T0 folder.

## Modules

- [Related provider routing](../modules/provider-routing.md)
- [Related agent layout](../modules/agent-layout.md)

## Pull requests

Track closed: 2026-10-27 (UTC).

[#1057](https://github.com/Vinix24/vnx-orchestration/pull/1057).
