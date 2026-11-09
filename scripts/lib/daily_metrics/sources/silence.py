"""Source: silence windows over 30 minutes, counted on the UTC day they close.

A window is a gap of *more than* 30 minutes between consecutive merges
(decisions 8 and 21). Every merge on ``ctx.day`` is measured against the merge
before it in a 14-day lookback that reaches ``ctx.now``'s UTC date. A gap that
closes on the day counts on the day, however far back it started: with no
earlier merge in the lookback it counts with ``started_at: None``,
``lookback_exhausted: True`` and a lower-bound ``duration_minutes_min``. A gap
closing on the next day counts there; the still-open window after the day's
last merge is reported in ``details.open_window_at_day_end``, not counted.

``open_pr_through`` counts PRs created before a window started that were merged
after it ended (lookback rows) or are still open (the open-PR list). A PR
closed without being merged is in neither list and is not counted -- a
documented undertelling; for a ``lookback_exhausted`` window the lookback start
stands in for the unknown start. Readiness during a window is not measurable
historically, so ``work_ready_coverage`` stays an explicit ``0 of n`` with
reason ``no_historical_readiness_snapshot``.

Unknown is never 0: a truncated population or open list, a failed ``gh`` read,
an unparseable row or a lookback/day row-count disagreement give ``value: None``
with a reason, with the day population in ``details.population``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from daily_metrics.collect import CollectContext
from daily_metrics.github_fetch import (
    GH_FAILED, GH_TIMEOUT_SECONDS, MergedPopulation, fetch_merged, parse_timestamp,
)
from daily_metrics.metric import ScalarMetric

SOURCE = "daily_metrics.sources.silence"
METRIC_ID = "silence_windows_over_30m"
LOOKBACK_DAYS = 14
WINDOW_MINUTES = 30.0
OPEN_PR_LIMIT = 1000
TRUNCATED_FETCH = "truncated_fetch"
POPULATION_MISMATCH = "population_mismatch"
NO_HISTORICAL_READINESS = "no_historical_readiness_snapshot"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _population_details(population: MergedPopulation) -> dict[str, Any]:
    return {"n": len(population.rows), "truncated": population.truncated,
            "range": [_stamp(population.range_from), _stamp(population.range_to)],
            "fetched_at": _stamp(population.fetched_at), "calls": population.calls}


def _default_runner(args: Sequence[str], cwd: Path) -> tuple:
    try:
        done = subprocess.run(list(args), cwd=str(cwd), capture_output=True,
                              text=True, timeout=GH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a failed launch is data, not a crash
        return None, "", f"{type(exc).__name__}: {str(exc)[:120]}"
    return done.returncode, done.stdout, done.stderr


def read_open_prs(project_root: Path, runner: Callable[..., tuple] | None = None,
                  ) -> tuple[list[dict[str, Any]], bool, str | None]:
    """``(rows, truncated, error)`` for ``gh pr list --state open``; never a short read."""
    args = ["gh", "pr", "list", "--state", "open", "--limit", str(OPEN_PR_LIMIT),
            "--json", "number,createdAt"]
    try:
        returncode, stdout, stderr = (runner or _default_runner)(args, cwd=Path(project_root))
    except Exception as exc:  # noqa: BLE001 - a raising runner is a failed read
        return [], False, f"{type(exc).__name__}: {str(exc)[:120]}"
    if returncode != 0:
        return [], False, f"rc={returncode}: {(stderr or '').strip()[:120]}"
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        return [], False, f"unparseable gh output: {str(exc)[:120]}"
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return [], False, "unparseable gh output: expected a list of objects"
    return data, len(data) >= OPEN_PR_LIMIT, None


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


def _parsed(population: MergedPopulation) -> tuple[list[dict[str, Any]] | None, str | None]:
    rows: list[dict[str, Any]] = []
    for row in population.rows:
        merged = parse_timestamp(row.get("mergedAt"))
        if merged is None:
            return None, "mergedAt"
        rows.append({"number": row.get("number"),
                     "created_at": parse_timestamp(row.get("createdAt")), "merged_at": merged})
    rows.sort(key=lambda item: item["merged_at"])
    return rows, None


def _population_rows(day: date, day_population: MergedPopulation, lookback: MergedPopulation,
                     ) -> tuple[list[dict[str, Any]] | None, tuple[str, str] | None]:
    """Sorted lookback rows, or ``(reason, error)`` when the populations disagree."""
    lookback_rows, lookback_error = _parsed(lookback)
    day_rows, day_error = _parsed(day_population)
    if lookback_error == "mergedAt" or day_error == "mergedAt":
        return None, (GH_FAILED, "unparseable mergedAt in the population")
    if lookback_error is not None or day_error is not None:
        return None, (POPULATION_MISMATCH, "unparseable createdAt in the population")
    if any(row["created_at"] is None for row in lookback_rows):
        return None, (POPULATION_MISMATCH, "missing createdAt in the population")
    on_day = sum(1 for row in lookback_rows if row["merged_at"].date() == day)
    if on_day != len(day_population.rows) or any(row["merged_at"].date() != day for row in day_rows):
        return None, (POPULATION_MISMATCH, "day population and lookback disagree")
    return lookback_rows, None


def _open_pr_through(start: datetime | None, end: datetime, lookback: list[dict[str, Any]],
                     open_rows: list[dict[str, Any]], lookback_start: datetime) -> int:
    effective_start = start if start is not None else lookback_start
    candidates = [row for row in lookback if row["created_at"] is not None
                  and row["created_at"] < effective_start and row["merged_at"] > end]
    candidates += [row for row in open_rows if row["created_at"] < effective_start]
    seen: set = set()
    count = 0
    for row in candidates:
        if row.get("number") is not None:
            if row["number"] in seen:
                continue
            seen.add(row["number"])
        count += 1
    return count


def _window_record(previous: datetime | None, moment: datetime, lookback: list[dict[str, Any]],
                   open_rows: list[dict[str, Any]], lookback_start: datetime) -> dict[str, Any]:
    through = _open_pr_through(previous, moment, lookback, open_rows, lookback_start)
    if previous is None:
        return {"started_at": None, "ended_at": _stamp(moment),
                "duration_minutes_min": (moment - lookback_start).total_seconds() / 60.0,
                "lookback_exhausted": True, "open_pr_through": through}
    return {"started_at": _stamp(previous), "ended_at": _stamp(moment),
            "duration_minutes": (moment - previous).total_seconds() / 60.0,
            "lookback_exhausted": False, "open_pr_through": through}


def _counted_windows(day: date, lookback: list[dict[str, Any]],
                     open_rows: list[dict[str, Any]], lookback_start: datetime) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for index, row in enumerate(lookback):
        if row["merged_at"].date() != day:
            continue
        previous = lookback[index - 1]["merged_at"] if index else None
        gap = (row["merged_at"] - previous).total_seconds() / 60.0 if previous else None
        if gap is not None and gap <= WINDOW_MINUTES:
            continue
        windows.append(_window_record(previous, row["merged_at"], lookback, open_rows, lookback_start))
    return windows


def _open_window_at_day_end(day: date, lookback: list[dict[str, Any]]) -> dict[str, Any] | None:
    day_rows = [row for row in lookback if row["merged_at"].date() == day]
    if not day_rows:
        return None
    last = day_rows[-1]["merged_at"]
    later = [row["merged_at"] for row in lookback if row["merged_at"] > last]
    closing = later[0] if later else None
    day_end = datetime.combine(day + timedelta(days=1), time(0, 0, 0), tzinfo=timezone.utc)
    inside_end = min(closing, day_end) if closing is not None else day_end
    if (inside_end - last).total_seconds() / 60.0 <= WINDOW_MINUTES:
        return None
    return {"started_at": _stamp(last),
            **({"closed_at": _stamp(closing)} if closing is not None else {})}


def _population_failure(day: MergedPopulation,
                        lookback: MergedPopulation) -> dict[str, ScalarMetric] | None:
    if day.error is not None:
        return {METRIC_ID: _unknown(GH_FAILED, day)}
    if lookback.error is not None:
        return {METRIC_ID: _unknown(GH_FAILED, day, error=f"lookback: {lookback.error}")}
    if day.truncated or lookback.truncated:
        record = _unknown(TRUNCATED_FETCH, day)
        record.details["lookback_population"] = _population_details(lookback)
        return {METRIC_ID: record}
    return None


def collect(ctx: CollectContext) -> dict[str, ScalarMetric]:
    """The ``silence_windows_over_30m`` record for ``ctx.day`` (decisions 8 and 21)."""
    try:
        project_root = ctx.project_root()
    except Exception as exc:  # noqa: BLE001 - an unavailable checkout is a named unknown
        return {METRIC_ID: _unknown(GH_FAILED, error=f"{type(exc).__name__}: {exc}")}
    day = fetch_merged(ctx.day, ctx.day, project_root=project_root, now=ctx.now)
    lookback = fetch_merged(ctx.day - timedelta(days=LOOKBACK_DAYS),
                            ctx.now.astimezone(timezone.utc).date(),
                            project_root=project_root, now=ctx.now)
    failure = _population_failure(day, lookback)
    if failure is not None:
        return failure
    lookback_rows, mismatch = _population_rows(ctx.day, day, lookback)
    if mismatch is not None:
        return {METRIC_ID: _unknown(mismatch[0], day, error=mismatch[1])}
    open_rows, open_truncated, open_error = read_open_prs(project_root)
    if open_error is not None:
        return {METRIC_ID: _unknown(GH_FAILED, day, error=f"open PR list: {open_error}")}
    if open_truncated:
        record = _unknown(TRUNCATED_FETCH, day)
        record.details["open_prs"] = len(open_rows)
        return {METRIC_ID: record}
    parsed_open = [{"number": row.get("number"),
                    "created_at": parse_timestamp(row.get("createdAt"))} for row in open_rows]
    if any(row["created_at"] is None for row in parsed_open):
        return {METRIC_ID: _unknown(GH_FAILED, day, error="open PR without a parseable createdAt")}
    windows = _counted_windows(ctx.day, lookback_rows, parsed_open, lookback.range_from)
    details: dict[str, Any] = {
        "population": _population_details(day),
        "lookback_population": _population_details(lookback),
        "windows": windows,
        "work_ready_coverage": {"measured": 0, "total": len(windows),
                                "reason": NO_HISTORICAL_READINESS},
        "open_prs": len(parsed_open),
    }
    open_window = _open_window_at_day_end(ctx.day, lookback_rows)
    if open_window is not None:
        details["open_window_at_day_end"] = open_window
    return {METRIC_ID: ScalarMetric(value=len(windows), source=SOURCE,
                                    sample_size=len(day.rows), details=details)}
