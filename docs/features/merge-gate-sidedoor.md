# merge-gate-sidedoor: CI evidence at the merge door

The governed merge path checks the project's CI workflow on the exact PR head before allowing a merge. Missing runs, failed conclusions and unverifiable results produce a refusal; an explicit operator override carries its reason in the result.

## How

Follow the [forge gate runbook](../operations/FORGE_GATE.md) for the governed merge path and server-side required-check protection. Inspect a PR with `vnx pr-ready <pr-number>`; see `vnx pr-ready --help` for flags and verbose evidence output.

## Modules

- [Merge door](../modules/merge-door.md)
- [Governance enforcement](../modules/governance-enforcement-stack.md)

## Pull requests

Track closed: 2026-10-15 (UTC).

[#1575](https://github.com/Vinix24/vnx-orchestration/pull/1575).
