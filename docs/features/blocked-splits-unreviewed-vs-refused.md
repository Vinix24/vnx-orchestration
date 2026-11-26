# blocked-splits-unreviewed-vs-refused: Unread plans and refused plans

The objective list distinguishes a plan awaiting review from one refused by the plan gate. Drift reasons name the same unread/refused distinction and identify ordinary blocking open items separately, so operators can distinguish review backlog from recorded objections.

## How

Run `vnx objective list --project-id vnx-dev` and `vnx objective drift --project-id vnx-dev`, substituting the intended project. See `vnx objective list --help` and `vnx objective drift --help` for flags, including JSON output.

## Modules

- [Horizon planning](../modules/horizon-planning.md)
- [Plan-gate panel](../modules/plan-gate-panel.md)
- [Git-grounded reconciliation](../modules/git-grounded-reconcile.md)

## Pull requests

Track closed: 2026-10-15 (UTC).

[#1768](https://github.com/Vinix24/vnx-orchestration/pull/1768).
