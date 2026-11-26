# plan-gate-tiebreaker: A recorded decision after the review threshold

After the configured number of plan versions have been reviewed without a PASS, a single tiebreaker returns START or STOP, a reason and at most one required change. The current threshold is one version, and every new version receives its seat review before the tiebreaker sees it. The result identifies the model and round; STOP attempts to record remaining findings as open items before clearing the plan-gate blocker, warning if storage fails.

## How

Run `vnx horizon plan-gate run <track-id> --project-id vnx-dev --doc <plan-path>`, substituting the track, project and plan; see `vnx horizon plan-gate run --help` for flags. Operator dispositions are available through `vnx horizon plan-gate attest --help` and `vnx horizon plan-gate reblock --help`; both require a reason and approval ID.

## Modules

- [Plan-gate panel](../modules/plan-gate-panel.md)
- [Horizon planning](../modules/horizon-planning.md)

## Pull requests

Track closed: 2026-10-17 (UTC).

[#1520](https://github.com/Vinix24/vnx-orchestration/pull/1520).
