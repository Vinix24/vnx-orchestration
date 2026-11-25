# daily-metrics

Collects per-project day documents from discovered metric sources, retaining unknown records for missing required metrics. The daily command collects the catch-up window, runs ordered command steps and writes a health beacon.

Background: [daily metrics operations](../operations/DAILY_METRICS.md).

## Entry points

[`scripts/lib/daily_metrics/collect.py::collect_day`](../../scripts/lib/daily_metrics/collect.py), [`scripts/daily_metrics.py::_run_daily`](../../scripts/daily_metrics.py).

## Tests

[tests/test_daily_metrics/test_collect.py](../../tests/test_daily_metrics/test_collect.py), [tests/test_daily_metrics/test_cli.py](../../tests/test_daily_metrics/test_cli.py).

## Decisions

[ADR-063](../governance/decisions/ADR-063-dagelijkse-doorloopmeting.md).

## Flag

—
