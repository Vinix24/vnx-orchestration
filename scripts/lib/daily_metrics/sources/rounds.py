"""Source: rounds per merged PR, from the round ledger (doorloop PR-5b).

Decision 7: ``rounds_per_pr`` is measured over exactly the PRs ``merges``
counts, and a merged PR the round ledger does not cover is a coverage gap --
``value: None`` with ``rounds_missing_for_<n>_prs`` -- never 0 rounds.

The count is the fabric's own (decision 6, section "Punt 6"): a round is a
codex-seat verdict on its own head, so ``review_rounds.codex_rounds`` gives it.
Two verdicts on the same head are one round and a re-anchored line is none; a
non-codex seat adds nothing. ``opened`` proves coverage, it never counts.
"""

from __future__ import annotations

from typing import Any

from daily_metrics import baseline
from daily_metrics.collect import CollectContext
from daily_metrics.github_fetch import GH_FAILED, MergedPopulation, fetch_merged
from daily_metrics.metric import ScalarMetric
from project_root import resolve_central_data_dir
from review_rounds import RoundLedgerError, codex_rounds, ledger_path, read_ledger

SOURCE = "daily_metrics.sources.rounds"
METRIC_ID = "rounds_per_pr"
TRUNCATED_FETCH = "truncated_fetch"
NO_MERGES_ON_DAY = "no_merges_on_day"
ROUND_LEDGER_UNREADABLE = "round_ledger_unreadable"
SAMPLES_CAP = baseline.MAX_SAMPLES_PER_DAY


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


def _ledger_for(project_id: str):
    """The project's round ledger under the central state (the store the gate writes)."""
    return ledger_path(resolve_central_data_dir(project_id) / "state" / "review_gates")


def _number(row: dict[str, Any]) -> int | None:
    """A PR number that can key the ledger; anything else is no number at all."""
    number = row.get("number")
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    return None


def _counts(rows: tuple[dict[str, Any], ...], project_id: str, ledger: Any) -> tuple[list[int], list[int], int]:
    """``(covered round counts, missing PR numbers, rows with no number)``.

    Covered means opened AND at least one codex round. Every other PR is a
    coverage gap; a row that carries no usable number cannot be looked up and
    is a gap too, never silently dropped.
    """
    covered: list[int] = []
    missing: list[int] = []
    unnumbered = 0
    for row in rows:
        number = _number(row)
        if number is None:
            unnumbered += 1
            continue
        rounds = codex_rounds(ledger.for_pr(project_id, number)) if ledger.is_opened(project_id, number) else ()
        if rounds:
            covered.append(len(rounds))
        else:
            missing.append(number)
    return covered, missing, unnumbered


def _uncovered(population: MergedPopulation, covered: list[int], missing: list[int],
               unnumbered: int) -> ScalarMetric:
    """The gap record: no value, the gap named, the measured part informative only."""
    details: dict[str, Any] = {
        "population": _population_details(population),
        "missing_prs": sorted(missing),
        "partial_median": baseline.quantile(covered, 0.5) if covered else None,
        "partial_n": len(covered),
    }
    if unnumbered:
        details["unnumbered_rows"] = unnumbered
    return ScalarMetric(value=None, source=SOURCE, sample_size=len(population.rows),
                        unknown_reason=f"rounds_missing_for_{len(missing) + unnumbered}_prs",
                        details=details)


def collect(ctx: CollectContext) -> dict[str, ScalarMetric]:
    """``rounds_per_pr`` for ``ctx.day``: the median over the day's merged PRs."""
    try:
        project_root = ctx.project_root()
    except Exception as exc:  # noqa: BLE001 - an unavailable checkout is a named unknown
        return {METRIC_ID: _unknown(GH_FAILED, error=f"{type(exc).__name__}: {exc}")}
    population = fetch_merged(ctx.day, ctx.day, project_root=project_root, now=ctx.now)
    if population.truncated:
        return {METRIC_ID: _unknown(TRUNCATED_FETCH, population)}
    if population.error is not None:
        # Fail-closed: every fetch error is unknown, and the code stays in details.
        return {METRIC_ID: _unknown(GH_FAILED, population)}
    if not population.rows:
        return {METRIC_ID: _unknown(NO_MERGES_ON_DAY, population)}
    try:
        ledger = read_ledger(_ledger_for(ctx.project_id))
    except RoundLedgerError as exc:
        return {METRIC_ID: _unknown(ROUND_LEDGER_UNREADABLE, population, error=str(exc))}
    covered, missing, unnumbered = _counts(population.rows, ctx.project_id, ledger)
    if missing or unnumbered:
        return {METRIC_ID: _uncovered(population, covered, missing, unnumbered)}
    details: dict[str, Any] = {"population": _population_details(population),
                               "samples": covered[:SAMPLES_CAP]}
    if len(covered) > SAMPLES_CAP:
        details["samples_truncated"] = True
    return {METRIC_ID: ScalarMetric(value=baseline.quantile(covered, 0.5), source=SOURCE,
                                    sample_size=len(covered), details=details)}
