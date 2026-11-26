# daily-metrics

Collects per-project day documents from discovered metric sources, retaining unknown records for missing required metrics. The daily command collects the catch-up window, runs ordered command steps and writes a health beacon.

Background: [daily metrics operations](../operations/DAILY_METRICS.md).

The nightly `daily` command also collects [ADR-097](../governance/decisions/ADR-097-ci-run-record.md)
GitHub attempt records into the central project's `state/ci_runs.ndjson` (`--state-dir`
overrides that destination). One `VNX_CI_RECORD_MAX_CALLS` budget covers the entire
14-day settled window, including repository discovery and all pages. Discovery starts
at the later of repository creation and `from_day - RERUN_HORIZON_DAYS` (30 days,
[GitHub's rerun eligibility limit](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/re-running-workflows-and-jobs)).
Creation bounds end on the last settled day. Searches over GitHub's 1,000-result
ceiling split by time, newest half first. Repository lookup and discovery use at most
one third of the call budget (rounded down, minimum two calls, never exceeding the
budget); the rest is reserved for attempt/job/PR reads. Each attempt's jobs are read
before the next attempt. Unread ranges still make coverage incomplete, even when
all identified window attempts were collected. `VNX_CI_WORKFLOWS` is a
comma-separated exact-name filter; empty selects all PR workflows. Each earlier
attempt is read separately. A stopped read cannot establish an earlier attempt's
timestamp/status: these unresolved attempts are named in beacon issues, never guessed.
`state/health/ci_runs.json` and a stderr JSON line report coverage, calls, skipped
attempts and failures. Incomplete coverage has `population_total: null`. Writer
failure leaves the other daily metrics running. The existing merged-PR metric is unchanged.
Both `ci_runs` and `daily_metrics` beacons are opt-in: absence alone is not a failure
on projects that never ran the nightly command. Existing beacons still report failures.
The top-level reason is the strongest actual blocking cause, in descending order:
`rate_limited`, `call_budget_exhausted`, `source_invalid`, `source_missing`.

Each validated line has six field wrappers. Test fields remain `junit_not_configured`
without `VNX_CI_JUNIT_ARTIFACT`; configured artifacts remain `source_missing` naming
the artifact and M3b reader. A budget/rate stop makes all six fields unknown for
identified completed attempts. Unresolved Actions fields (including transient read
failures and records written by the earlier writer) are retried inside the catch-up
window; they remain issues/skipped attempts with a failing beacon until resolved.
Lines remain immutable, but one complete Actions observation may supersede an
unresolved observation with the same repository/project/run/attempt identity. Readers
must use `existing_records`: the latest complete Actions observation wins, unresolved
observations cannot replace it, and an identity counts once. JUnit unknowns await M3b
and do not cause Actions rereads. Ledger readers must hold a shared `flock`;
writers hold its exclusive lock across validation, deduplication and append, including
short writes. A corrupt/partial existing line blocks further appends visibly.
When GitHub omits a PR association, the writer paginates the commit-associated PR
endpoint and accepts only one exact head-SHA match. This also covers fork and deleted
branches without guessing from branch names. Failed, malformed or ambiguous lookups
remain blocking and are retried. A complete lookup without a match records `pr_id: null`
with `pr_id_unknown_reason: source_missing` once; that unknown alone does not fail
coverage because the four-part attempt identity is proven. Malformed associations
remain blocking. Unknown observations outside the catch-up window remain unknown;
collecting their settled window explicitly is required to retry them.

## Entry points

[`scripts/lib/daily_metrics/collect.py::collect_day`](../../scripts/lib/daily_metrics/collect.py), [`scripts/daily_metrics.py::_run_daily`](../../scripts/daily_metrics.py).

## Tests

[tests/test_daily_metrics/test_collect.py](../../tests/test_daily_metrics/test_collect.py), [tests/test_daily_metrics/test_cli.py](../../tests/test_daily_metrics/test_cli.py).

## Decisions

[ADR-063](../governance/decisions/ADR-063-dagelijkse-doorloopmeting.md).

## Flag

—
