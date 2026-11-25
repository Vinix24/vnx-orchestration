# reparatiegolf: Visible failures and consistent project and review selection

The analyzer reports degraded health when sessions are deferred or skipped by configuration without a deep-analysis attempt, and records causes for failed calls. In the installed Python CLI, an explicit project ID selects that project's store; a conflicting explicit project directory or central-store override is refused. Merge and readiness checks share review-seat evaluation, requiring distinct reviewers for the applicable seats and exposing blocking findings on the current head.

## How

Use the [analyzer health runbook](../operations/CONVERSATION_ANALYZER_HEALTH.md) to investigate the beacon and failure causes. Follow [review gate verification](../operations/T0_RUNBOOKS.md#34-review-gate-verification) for missing, failed or takeover review evidence.

## Modules

- [Self-learning loop](../modules/intelligence-self-learning-loop.md)
- [Central database routing](../modules/central-db-routing.md)
- [Governance enforcement](../modules/governance-enforcement-stack.md)
- [Merge door](../modules/merge-door.md)

## Pull requests

Track closed: 2026-10-17 (UTC).

[#2067](https://github.com/Vinix24/vnx-orchestration/pull/2067), [#2068](https://github.com/Vinix24/vnx-orchestration/pull/2068), [#2071](https://github.com/Vinix24/vnx-orchestration/pull/2071), [#2072](https://github.com/Vinix24/vnx-orchestration/pull/2072), [#2066](https://github.com/Vinix24/vnx-orchestration/pull/2066), [#2069](https://github.com/Vinix24/vnx-orchestration/pull/2069), [#2070](https://github.com/Vinix24/vnx-orchestration/pull/2070), [#2073](https://github.com/Vinix24/vnx-orchestration/pull/2073), [#2077](https://github.com/Vinix24/vnx-orchestration/pull/2077), [#2081](https://github.com/Vinix24/vnx-orchestration/pull/2081), [#2074](https://github.com/Vinix24/vnx-orchestration/pull/2074), [#2084](https://github.com/Vinix24/vnx-orchestration/pull/2084), [#2086](https://github.com/Vinix24/vnx-orchestration/pull/2086).
