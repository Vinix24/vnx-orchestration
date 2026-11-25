# daily-metrics

Collects per-project day documents from discovered metric sources, retaining unknown records for missing required metrics. The daily command collects the catch-up window, runs ordered command steps and writes a health beacon.

Background: [daily metrics operations](../operations/DAILY_METRICS.md).

The nightly `daily` command also collects [ADR-097](../governance/decisions/ADR-097-ci-run-record.md)
GitHub attempt records into the central project's `state/ci_runs.ndjson` (`--state-dir`
overrides that destination). One `VNX_CI_RECORD_MAX_CALLS` budget covers the entire
14-day settled window, including repository discovery and all pages. Discovery scans
run history so recent reruns of old runs remain visible. `VNX_CI_WORKFLOWS` is a
comma-separated exact-name filter; empty selects all PR workflows. Each earlier
attempt is read separately. A stopped read cannot establish an earlier attempt's
timestamp/status: these unresolved attempts are named in beacon issues, never guessed.
`state/health/ci_runs.json` and a stderr JSON line report coverage, calls, skipped
attempts and failures. Incomplete coverage has `population_total: null`. Writer
failure leaves the other daily metrics running. The existing merged-PR metric is unchanged.

Each validated line has six field wrappers. Test fields remain `junit_not_configured`
without `VNX_CI_JUNIT_ARTIFACT`; configured artifacts remain `source_missing` naming
the artifact and M3b reader. A budget/rate stop makes all six fields unknown for
identified completed attempts. Observations are immutable: recollection does not
replace unknowns or append duplicates. Ledger readers must hold a shared `flock`;
writers hold its exclusive lock across validation, deduplication and append, including
short writes. A corrupt/partial existing line blocks further appends visibly.

## Entry points

[`scripts/lib/daily_metrics/collect.py::collect_day`](../../scripts/lib/daily_metrics/collect.py), [`scripts/daily_metrics.py::_run_daily`](../../scripts/daily_metrics.py).

## Tests

[tests/test_daily_metrics/test_collect.py](../../tests/test_daily_metrics/test_collect.py), [tests/test_daily_metrics/test_cli.py](../../tests/test_daily_metrics/test_cli.py).

## Decisions

[ADR-063](../governance/decisions/ADR-063-dagelijkse-doorloopmeting.md).

## Flag

—
