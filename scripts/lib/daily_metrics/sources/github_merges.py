"""Source: the day's merged PRs; count and lead time from one proven population.

``collect`` reads one MergedPopulation and derives ``merges`` (a measured count,
0 included), the open-to-merge median and its p75; every record carries
``details.population``, so one set of rows stands behind all three (decision 21).
Unknown is never 0: a truncated population, a failed read, an unavailable
checkout or an unparseable ``createdAt`` gives ``value: None`` with a reason.
"""

from __future__ import annotations

from typing import Any

from daily_metrics import baseline
from daily_metrics.collect import CollectContext
from daily_metrics.github_fetch import GH_FAILED, MergedPopulation, fetch_merged, parse_timestamp
from daily_metrics.metric import ScalarMetric

SOURCE = "daily_metrics.sources.github_merges"
MERGES = "merges"
MEDIAN = "pr_open_to_merge_median_hours"
P75 = "pr_open_to_merge_p75_hours"
SAMPLES_CAP = baseline.MAX_SAMPLES_PER_DAY
TRUNCATED_FETCH = "truncated_fetch"
POPULATION_MISMATCH = "population_mismatch"
NO_MERGES_ON_DAY = "no_merges_on_day"

def _stamp(moment: Any) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

def _population_details(population: MergedPopulation) -> dict[str, Any]:
    return {"n": len(population.rows), "truncated": population.truncated,
            "range": [_stamp(population.range_from), _stamp(population.range_to)],
            "fetched_at": _stamp(population.fetched_at), "calls": population.calls}

def _unknown(reason: str, population: MergedPopulation | None = None,
             error: str | None = None) -> ScalarMetric:
    details: dict[str, Any] = {}
    if population is not None:
        details["population"] = _population_details(population)
        if population.error is not None:
            details["population_error"] = population.error
    if error is not None:
        details["error"] = error
    return ScalarMetric(value=None, source=SOURCE, unknown_reason=reason,
                        sample_size=len(population.rows) if population else 0,
                        details=details)

def _all_unknown(reason: str, population: MergedPopulation | None = None,
                 error: str | None = None) -> dict[str, ScalarMetric]:
    return {metric_id: _unknown(reason, population, error)
            for metric_id in (MERGES, MEDIAN, P75)}

def _durations(rows: tuple[dict[str, Any], ...]) -> list[float] | None:
    durations: list[float] = []
    for row in rows:
        created = parse_timestamp(row.get("createdAt"))
        merged = parse_timestamp(row.get("mergedAt"))
        if created is None or merged is None:
            return None
        durations.append((merged - created).total_seconds() / 3600.0)
    return durations

def _hour_histogram(rows: tuple[dict[str, Any], ...]) -> list[int]:
    counts = [0] * 24
    for row in rows:
        counts[parse_timestamp(row.get("mergedAt")).hour] += 1
    return counts

def _duration_record(metric_id: str, durations: list[float], q: float,
                     population: MergedPopulation) -> ScalarMetric:
    details: dict[str, Any] = {"population": _population_details(population),
                               "samples": durations[:SAMPLES_CAP]}
    if len(durations) > SAMPLES_CAP:
        details["samples_truncated"] = True
    return ScalarMetric(value=baseline.quantile(durations, q), source=SOURCE,
                        sample_size=len(durations), details=details)

def collect(ctx: CollectContext) -> dict[str, ScalarMetric]:
    """The three merged-PR records for ``ctx.day``, from one population (decision 21)."""
    try:
        project_root = ctx.project_root()
    except Exception as exc:  # noqa: BLE001 — an unavailable checkout is a named unknown
        return _all_unknown(GH_FAILED, error=f"{type(exc).__name__}: {exc}")
    population = fetch_merged(ctx.day, ctx.day, project_root=project_root, now=ctx.now)
    if population.truncated:
        return _all_unknown(TRUNCATED_FETCH, population)
    if population.error is not None:
        # Fail-closed: every fetch error is unknown, and the code stays in details.
        return _all_unknown(GH_FAILED, population)
    durations = _durations(population.rows)
    if durations is None:
        return _all_unknown(POPULATION_MISMATCH, population)
    merges = ScalarMetric(
        value=len(population.rows), source=SOURCE, sample_size=len(population.rows),
        details={"population": _population_details(population),
                 "hour_histogram": _hour_histogram(population.rows)})
    if not population.rows:
        return {MERGES: merges, MEDIAN: _unknown(NO_MERGES_ON_DAY, population),
                P75: _unknown(NO_MERGES_ON_DAY, population)}
    return {MERGES: merges, MEDIAN: _duration_record(MEDIAN, durations, 0.5, population),
            P75: _duration_record(P75, durations, 0.75, population)}
