"""Source: median CI duration per merged PR, from the runs on its head (decision 22).

The population is the day's merged PRs, read once by ``fetch_merged`` (the same
call the merges source makes). Per PR the duration is
``max(updatedAt) - min(startedAt)`` over every run on its head commit, and the
metric is the median over those PR durations.

Fail-closed everywhere: ``gh run list --commit`` silently returns zero rows for
a sha that is not 40 hex characters, so the sha is validated before the call.
An invalid sha, a failed ``gh`` call, a run list that reaches ``RUN_LIMIT`` (the
truncation is not proven either way), a run that is not ``completed`` or a run
without a parseable ``startedAt``/``updatedAt`` makes the PR unreadable and the
whole metric ``None`` with a transient reason. A successful call with ``[]`` is
a measured fact: that PR counts in ``details.no_run_prs`` and out of the median.

Unreadability is counted per row, never per PR number: ``fetch_merged`` does not
guarantee ``number``, so a row without one is still counted in the reason and in
``details.unreadable_rows_without_number`` even though it cannot be listed.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from daily_metrics import baseline
from daily_metrics.collect import CollectContext
from daily_metrics.github_fetch import (
    GH_FAILED,
    GH_TIMEOUT_SECONDS,
    MergedPopulation,
    fetch_merged,
    parse_timestamp,
)
from daily_metrics.metric import ScalarMetric

SOURCE = "daily_metrics.sources.ci"
CI_DURATION = "ci_duration_seconds_median"
SAMPLES_CAP = baseline.MAX_SAMPLES_PER_DAY
TRUNCATED_FETCH = "truncated_fetch"
NO_MERGES_ON_DAY = "no_merges_on_day"
NO_RUNS_ON_ANY_HEAD = "no_ci_runs_on_any_head"
UNREADABLE_PREFIX = "ci_run_unreadable_for_"
RUN_LIMIT = 100
RUN_FIELDS = "databaseId,status,conclusion,event,startedAt,updatedAt,workflowName"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

OK, NO_RUN, UNREADABLE = "ok", "no_run", "unreadable"


def _stamp(moment: Any) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _population_details(population: MergedPopulation) -> dict[str, Any]:
    return {"n": len(population.rows), "truncated": population.truncated,
            "range": [_stamp(population.range_from), _stamp(population.range_to)],
            "fetched_at": _stamp(population.fetched_at), "calls": population.calls}


def _default_runner(args: Sequence[str], cwd: Path) -> tuple:
    """Run ``gh`` in ``cwd``; a launch failure is data, returned as a failed tuple."""
    try:
        done = subprocess.run(list(args), cwd=str(cwd), capture_output=True,
                              text=True, timeout=GH_TIMEOUT_SECONDS)
    except Exception as exc:  # vnx-silent-except: a failed launch is a named unknown
        return None, "", f"{type(exc).__name__}: {str(exc)[:120]}"
    return done.returncode, done.stdout, done.stderr


def _read_runs(runner, root: Path, sha: str) -> tuple[list | None, str | None]:
    """``(runs, error)`` for the head commit; an error is never an empty list."""
    args = ["gh", "run", "list", "--commit", sha, "--limit", str(RUN_LIMIT),
            "--json", RUN_FIELDS]
    try:
        returncode, stdout, stderr = runner(args, cwd=root)
    except Exception as exc:  # vnx-silent-except: a raising runner is a failed read
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"
    if returncode != 0:
        return None, f"rc={returncode}: {(stderr or '').strip()[:120]}"
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        return None, f"unparseable gh output: {str(exc)[:120]}"
    if not isinstance(data, list) or not all(isinstance(run, dict) for run in data):
        return None, "unparseable gh output: expected a list of objects"
    if len(data) >= RUN_LIMIT:
        # A full page proves neither completeness nor truncation: coverage is not
        # proven, so the run list is as unusable as a failed read.
        return None, f"run list reached the limit of {RUN_LIMIT}: coverage not proven"
    return data, None


def _span_seconds(runs: Sequence[dict[str, Any]]) -> float | None:
    """``max(updatedAt) - min(startedAt)``; ``None`` when any run is not usable."""
    starts: list[datetime] = []
    ends: list[datetime] = []
    for run in runs:
        if run.get("status") != "completed":
            return None
        start = parse_timestamp(run.get("startedAt"))
        end = parse_timestamp(run.get("updatedAt"))
        if start is None or end is None:
            return None
        starts.append(start)
        ends.append(end)
    return (max(ends) - min(starts)).total_seconds()


def _measure(row: dict[str, Any], root: Path, runner) -> tuple[str, float | None, int]:
    """``(kind, duration_seconds, gh_calls)`` for one merged PR."""
    sha = row.get("headRefOid")
    if not isinstance(sha, str) or SHA_PATTERN.fullmatch(sha) is None:
        return UNREADABLE, None, 0  # a short sha reads as zero rows: never call gh
    runs, error = _read_runs(runner, root, sha)
    if error is not None:
        return UNREADABLE, None, 1
    if not runs:
        return NO_RUN, None, 1
    duration = _span_seconds(runs)
    if duration is None:
        return UNREADABLE, None, 1
    return OK, duration, 1


def _unknown(reason: str, population: MergedPopulation | None = None, *,
             error: str | None = None, unreadable: list[int] | None = None,
             no_run: list[int] | None = None,
             unreadable_rows_without_number: int | None = None) -> ScalarMetric:
    details: dict[str, Any] = {}
    if population is not None:
        details["population"] = _population_details(population)
        if population.error is not None:
            details["population_error"] = population.error
    if error is not None:
        details["error"] = error
    if unreadable is not None:
        details["unreadable_prs"] = unreadable
    if unreadable_rows_without_number is not None:
        details["unreadable_rows_without_number"] = unreadable_rows_without_number
    if no_run is not None:
        details["no_run_prs"] = no_run
    return ScalarMetric(value=None, source=SOURCE, unknown_reason=reason,
                        sample_size=len(population.rows) if population else 0,
                        details=details)


def _number(row: dict[str, Any]) -> int | None:
    number = row.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def collect(ctx: CollectContext) -> dict[str, ScalarMetric]:
    """The CI-duration record for ``ctx.day`` (decision 22)."""
    try:
        project_root = ctx.project_root()
    except Exception as exc:  # vnx-silent-except: an unavailable checkout is a named unknown
        return {CI_DURATION: _unknown(GH_FAILED, error=f"{type(exc).__name__}: {exc}")}
    population = fetch_merged(ctx.day, ctx.day, project_root=project_root, now=ctx.now)
    if population.truncated:
        return {CI_DURATION: _unknown(TRUNCATED_FETCH, population)}
    if population.error is not None:
        # Fail-closed: every fetch error is unknown, and the code stays in details.
        return {CI_DURATION: _unknown(GH_FAILED, population)}
    if not population.rows:
        return {CI_DURATION: _unknown(NO_MERGES_ON_DAY, population)}

    durations: list[float] = []
    unreadable: list[int] = []  # rows whose number is usable enough to list
    unreadable_rows = 0  # every unreadable row, listed or not
    unreadable_without_number = 0
    no_run: list[int] = []
    calls = 0
    for row in population.rows:
        kind, duration, used = _measure(row, project_root, _default_runner)
        calls += used
        number = _number(row)
        if kind == OK:
            durations.append(duration)
        elif kind == NO_RUN:
            if number is not None:
                no_run.append(number)
        else:
            unreadable_rows += 1
            if number is not None:
                unreadable.append(number)
            else:
                unreadable_without_number += 1

    if unreadable_rows:
        reason = f"{UNREADABLE_PREFIX}{unreadable_rows}_prs"
        return {CI_DURATION: _unknown(
            reason, population, unreadable=sorted(unreadable), no_run=sorted(no_run),
            unreadable_rows_without_number=unreadable_without_number or None)}
    if not durations:
        return {CI_DURATION: _unknown(NO_RUNS_ON_ANY_HEAD, population,
                                      no_run=sorted(no_run))}
    details: dict[str, Any] = {
        "population": _population_details(population),
        "samples": durations[:SAMPLES_CAP], "p75": baseline.quantile(durations, 0.75),
        "no_run_prs": sorted(no_run), "gh_calls": calls,
    }
    if len(durations) > SAMPLES_CAP:
        details["samples_truncated"] = True
    return {CI_DURATION: ScalarMetric(value=baseline.quantile(durations, 0.5), source=SOURCE,
                                      sample_size=len(durations), details=details)}
