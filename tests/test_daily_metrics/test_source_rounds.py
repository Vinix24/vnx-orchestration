"""The rounds source: rounds per merged PR from the round ledger (PR-5b).

The population is the day's merges (the same call the merges source makes).
A merged PR the ledger does not cover is a coverage gap: ``value: None`` with
``rounds_missing_for_<n>_prs``, never 0 rounds (decision 7). A round is the
fabric's own count: one codex-seat verdict per distinct head.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

PROJECT = "proj"
DAY, NOW = date(2026, 11, 9), datetime(2026, 11, 10, 4, 10, tzinfo=timezone.utc)
UTC = timezone.utc
METRIC_ID = "rounds_per_pr"
LEDGER_REL = Path(".vnx-data") / PROJECT / "state" / "review_gates" / "rounds.ndjson"


def test_the_rounds_source_exists():
    assert importlib.util.find_spec("daily_metrics.sources.rounds") is not None


def test_the_sources_directory_publishes_the_rounds_source():
    """Discovery over the real package sources imports it without a failure."""
    from daily_metrics import collect as collect_mod

    modules, failures = collect_mod.discover_sources(collect_mod.sources_dir())
    assert failures == []
    assert "rounds" in [module.__name__.rsplit(".", 1)[-1] for module in modules]


@pytest.fixture()
def source():
    return importlib.import_module("daily_metrics.sources.rounds")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from daily_metrics.collect import CollectContext

    monkeypatch.setenv("HOME", str(tmp_path))
    context = CollectContext(PROJECT, DAY, NOW)
    context._project_root = tmp_path
    return context


@pytest.fixture()
def ledger(tmp_path, ctx):
    """The ledger path under a central data dir rooted at the test's HOME."""
    path = tmp_path / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _row(number, merged_at="2026-11-09T01:00:00Z"):
    return {"number": number, "createdAt": "2026-11-09T00:00:00Z", "mergedAt": merged_at}


def _population(rows, *, truncated=False, error=None):
    from daily_metrics.github_fetch import MergedPopulation

    return MergedPopulation(rows=tuple(rows), range_from=datetime(2026, 11, 9, tzinfo=UTC),
                            range_to=datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC),
                            fetched_at=NOW, truncated=truncated, error=error, calls=1)


def _verdict(pr, head, *, seat="codex_gate", at=1):
    return {
        "project_id": PROJECT, "pr_number": pr, "head_sha": head, "seat": seat,
        "reviewer": seat, "takeover_from": "", "verdict": "pass", "blocking_count": 0,
        "report_path": f"/r/{pr}-{at}.md", "dispatch_id": f"d-{pr}-{at}",
        "recorded_at": f"2026-11-09T{at:02d}:00:00Z", "branch": "b", "source": "gate",
    }


def _opened(pr):
    return {"kind": "opened", "project_id": PROJECT, "pr_number": pr, "basis": "new",
            "evidence": 0, "recorded_at": "2026-11-09T00:00:00Z", "source": "gate"}


def _write(path, lines):
    """Write ledger lines; a ``str`` line goes in raw (a malformed line as the file holds it)."""
    path.write_text(
        "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
        encoding="utf-8",
    )


def _collect(source, ctx, population, monkeypatch):
    monkeypatch.setattr(source, "fetch_merged", lambda *args, **kwargs: population)
    return source.collect(ctx)[METRIC_ID]


def test_three_prs_with_one_three_and_two_rounds_are_a_median_of_two(source, ctx, ledger, monkeypatch):
    _write(ledger, [
        _opened(101), _verdict(101, "a1", at=1),
        _opened(102), _verdict(102, "b1", at=1), _verdict(102, "b2", at=2), _verdict(102, "b3", at=3),
        _opened(103), _verdict(103, "c1", at=1), _verdict(103, "c2", at=2),
    ])
    record = _collect(source, ctx, _population([_row(101), _row(102), _row(103)]), monkeypatch)
    assert record.value == 2 and record.sample_size == 3
    assert record.details["samples"] == [1, 3, 2]
    assert record.details["population"]["n"] == 3
    assert record.details["population"]["truncated"] is False


def test_one_round_per_distinct_head_and_no_round_for_another_seat(source, ctx, ledger, monkeypatch):
    """Two codex verdicts on one head are one round; a kimi seat on a new head adds none."""
    _write(ledger, [
        _opened(201),
        _verdict(201, "x1", at=1), _verdict(201, "x1", at=2),
        _verdict(201, "x2", at=3, seat="kimi_gate"),
    ])
    record = _collect(source, ctx, _population([_row(201)]), monkeypatch)
    assert record.value == 1 and record.sample_size == 1
    assert record.details["samples"] == [1]


def test_a_merged_pr_without_an_opened_line_is_a_coverage_gap(source, ctx, ledger, monkeypatch):
    _write(ledger, [
        _opened(301), _verdict(301, "h1", at=1), _verdict(301, "h2", at=2),
        _verdict(302, "h3", at=3),
    ])
    record = _collect(source, ctx, _population([_row(301), _row(302)]), monkeypatch)
    assert record.value is None
    assert record.unknown_reason == "rounds_missing_for_1_prs"
    assert record.details["missing_prs"] == [302]
    assert record.details["partial_median"] == 2
    assert record.details["partial_n"] == 1
    assert record.details["population"]["n"] == 2


def test_a_truncated_population_is_unknown(source, ctx, ledger, monkeypatch):
    _write(ledger, [_opened(401), _verdict(401, "h1")])
    record = _collect(source, ctx, _population([_row(401)], truncated=True), monkeypatch)
    assert record.value is None and record.unknown_reason == "truncated_fetch"
    assert record.details["population"]["truncated"] is True


def test_a_day_without_merges_is_unknown_not_zero(source, ctx, ledger, monkeypatch):
    record = _collect(source, ctx, _population([]), monkeypatch)
    assert record.value is None and record.unknown_reason == "no_merges_on_day"


def test_a_fetch_error_is_gh_failed(source, ctx, ledger, monkeypatch):
    record = _collect(source, ctx, _population([_row(501)], error="gh_failed"), monkeypatch)
    assert record.value is None and record.unknown_reason == "gh_failed"
    assert record.details["population_error"] == "gh_failed"


def test_an_unavailable_checkout_is_gh_failed(source, monkeypatch):
    from daily_metrics.collect import CollectContext

    context = CollectContext(PROJECT, DAY, NOW)

    def _refuse():
        raise RuntimeError("no checkout")

    monkeypatch.setattr(context, "project_root", _refuse)
    record = source.collect(context)[METRIC_ID]
    assert record.value is None and record.unknown_reason == "gh_failed"
    assert record.details["error"] == "RuntimeError: no checkout"


def test_an_unreadable_ledger_is_a_named_unknown(source, ctx, tmp_path, monkeypatch):
    """A ledger path that is a directory is no ledger (RoundLedgerError)."""
    path = tmp_path / LEDGER_REL
    path.mkdir(parents=True)
    record = _collect(source, ctx, _population([_row(601)]), monkeypatch)
    assert record.value is None and record.unknown_reason == "round_ledger_unreadable"
    assert record.details["error"]


def test_an_opened_pr_with_a_bad_line_naming_it_is_unreadable(source, ctx, ledger, monkeypatch):
    """Two valid rounds plus a malformed line for the PR: no count from partial history."""
    _write(ledger, [
        _opened(801), _verdict(801, "h1", at=1), _verdict(801, "h2", at=2),
        '{"pr_number": 801, "seat": "codex_gate"',
    ])
    record = _collect(source, ctx, _population([_row(801)]), monkeypatch)
    assert record.value is None
    assert record.unknown_reason == "round_ledger_unreadable_for_1_prs"
    assert record.details["unreadable_prs"] == [801]
    assert record.details["unreadable_text"]


def test_a_bad_line_naming_no_pr_makes_every_merged_pr_unreadable(source, ctx, ledger, monkeypatch):
    """A line that names no PR refuses every PR (the fabric's rule)."""
    _write(ledger, [
        _opened(811), _verdict(811, "h1", at=1),
        _opened(812), _verdict(812, "g1", at=2),
        "this is not a ledger line",
    ])
    record = _collect(source, ctx, _population([_row(811), _row(812)]), monkeypatch)
    assert record.value is None
    assert record.unknown_reason == "round_ledger_unreadable_for_2_prs"
    assert record.details["unreadable_prs"] == [811, 812]


def test_a_bad_line_naming_another_pr_leaves_this_days_count(source, ctx, ledger, monkeypatch):
    """A bad line for PR 999 does not touch PR 821's count."""
    _write(ledger, [
        _opened(821), _verdict(821, "h1", at=1), _verdict(821, "h2", at=2),
        '{"pr_number": 999, "seat": "codex_gate"',
    ])
    record = _collect(source, ctx, _population([_row(821)]), monkeypatch)
    assert record.value == 2 and record.sample_size == 1
    assert record.unknown_reason is None


def test_unreadable_takes_precedence_and_still_names_uncovered_prs(source, ctx, ledger, monkeypatch):
    """Unreadable wins over the coverage gap; the uncovered PRs are still named."""
    _write(ledger, [
        _opened(831), _verdict(831, "h1", at=1),
        _opened(832), _verdict(832, "h2", at=2),
        '{"pr_number": 831, "seat": "codex_gate"',
    ])
    record = _collect(source, ctx, _population([_row(831), _row(832), _row(833)]), monkeypatch)
    assert record.value is None
    assert record.unknown_reason == "round_ledger_unreadable_for_1_prs"
    assert record.details["unreadable_prs"] == [831]
    assert record.details["missing_prs"] == [833]


def test_an_unreadable_pr_stays_final(source, ctx, ledger, monkeypatch):
    """A bad ledger line is never rewritten, so the day is not retried as incomplete."""
    from daily_metrics import metric

    _write(ledger, [
        _opened(841), _verdict(841, "h1", at=1),
        '{"pr_number": 841, "seat": "codex_gate"',
    ])
    record = _collect(source, ctx, _population([_row(841)]), monkeypatch)
    assert record.unknown_reason == "round_ledger_unreadable_for_1_prs"
    assert metric.day_status({METRIC_ID: record.to_dict()}) == "final"


def test_an_uncovered_day_stays_final(source, ctx, ledger, monkeypatch):
    """Decision 7: a coverage gap is permanent, so the day is not retried as incomplete."""
    from daily_metrics import metric

    _write(ledger, [_verdict(701, "h1")])
    record = _collect(source, ctx, _population([_row(701)]), monkeypatch)
    assert record.unknown_reason == "rounds_missing_for_1_prs"
    assert metric.day_status({METRIC_ID: record.to_dict()}) == "final"
