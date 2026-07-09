## VNX Governance

This repository is governed by **VNX Glass Box Governance**: multi-agent orchestration with a human gate at every step and an append-only NDJSON receipt per dispatch.

The mechanism is not duplicated here. How the fabric works — the single-entry dispatch door and its lanes, review gates, the horizon planning layer, state resolution, and the report contract — lives in one canonical place so it can never drift out of a project file:

- **How the fabric works:** the canonical orchestrator role, `.claude/terminals/T0/role-orchestrator.md`, kept in sync fleet-wide by `vnx role sync`.
- **Runbooks + gotchas:** the `fabric-reference` skill.
- **Dispatch mechanics (lanes, provider routing, failure modes):** `docs/core/DISPATCH_RULES.md`.

Everything above this block describes *this project*. Everything the fabric does lives in the canonical role — never copy fabric mechanism back into this file, or the copy drifts the moment the fabric changes.
