"""Source tests for gate duration per seat (doorloop PR-5a, decision 23).

Gate duration is not one number: each seat gets its own record, and every
result file that is no seat, has no duration or has no timestamp is counted
in ``details`` instead of dropped. The central data directory is reached
through a monkeypatched ``HOME``, so every fixture lives under ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from daily_metrics import metric
from daily_metrics.collect import CollectContext

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
PROJECT = "p"
METRIC_ID = "gate_duration_seconds_by_seat"
NO_RESULTS = "no_results_for_seat_on_day"
NO_DURATION = "no_duration_on_day"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point ``Path.home()`` at ``tmp_path`` (the source resolves state from HOME)."""
    monkeypatch.setenv("HOME", str(tmp_path))


def _context(day: date = DAY):
    return CollectContext(PROJECT, day, NOW)


def _results_dir(home: Path) -> Path:
    return home / ".vnx-data" / PROJECT / "state" / "review_gates" / "results"


def _write(home: Path, name: str, record: dict | str) -> Path:
    results = _results_dir(home)
    results.mkdir(parents=True, exist_ok=True)
    path = results / name
    path.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")
    return path


def _result(duration: float | None = 1.0, recorded: str | None = "2026-11-06T10:00:00Z") -> dict:
    record: dict = {}
    if recorded is not None:
        record["recorded_at"] = recorded
    if duration is not None:
        record["duration_seconds"] = duration
    return record


def _collect(context=None):
    from daily_metrics.sources import gates

    return gates.collect(context or _context())[METRIC_ID]


def test_gates_source_module_is_importable():
    """Red run: the first assert fails on the merge base, before the source lands."""
    assert importlib.util.find_spec("daily_metrics.sources.gates") is not None


def test_twelve_codex_results_carry_sample_size_and_median(tmp_path):
    for index, duration in enumerate(range(10, 130, 10)):
        _write(tmp_path, f"pr-{800 + index}-codex_gate.json", _result(float(duration)))

    record = _collect()

    entry = record.entries["codex_gate"]
    assert entry.sample_size == 12
    assert entry.value == pytest.approx(65.0)
    assert entry.details["p75"] == pytest.approx(92.5)
    assert len(entry.details["samples"]) == 12
    assert entry.details["samples_truncated"] is False
    assert entry.details["no_duration_n"] == 0


def test_seat_seen_in_the_lookback_but_not_on_the_day_keeps_its_entry(tmp_path):
    _write(tmp_path, "pr-700-codex_gate.json", _result(12.0))
    _write(tmp_path, "pr-701-glm_gate.json", _result(30.0, recorded="2026-11-03T09:00:00Z"))

    record = _collect()

    entry = record.entries["glm_gate"]
    assert entry.value is None
    assert entry.unknown_reason == NO_RESULTS
    assert record.entries["codex_gate"].sample_size == 1


def test_a_result_without_duration_is_counted_and_not_in_the_median(tmp_path):
    _write(tmp_path, "pr-1-codex_gate.json", _result(10.0))
    _write(tmp_path, "pr-2-codex_gate.json", _result(20.0))
    _write(tmp_path, "pr-3-codex_gate.json", _result(30.0))
    _write(tmp_path, "pr-4-codex_gate.json", _result(None))

    record = _collect()

    entry = record.entries["codex_gate"]
    assert entry.sample_size == 3
    assert entry.value == pytest.approx(20.0)
    assert entry.details["no_duration_n"] == 1
    assert record.details["no_duration_n"] == 1


def test_unrecognised_files_are_counted_and_never_become_an_entry(tmp_path):
    _write(tmp_path, "pr-703-codex_gate.json", _result(5.0))
    _write(tmp_path, "pr-1801-claude_github_optional-contract.json", _result(1.0))
    _write(tmp_path, "pr-9-kimi_gate-chain-exhausted.json", _result(2.0))

    record = _collect()

    assert record.details["unrecognised_files"] == 2
    assert "claude_github_optional-contract" not in record.entries
    assert "kimi_gate-chain-exhausted" not in record.entries
    assert set(record.entries) == {"codex_gate"}


def test_a_recognised_result_without_any_timestamp_is_unattributed(tmp_path):
    _write(tmp_path, "pr-704-codex_gate.json", _result(5.0))
    _write(tmp_path, "pr-705-gemini_review.json", {"duration_seconds": 3.0})

    record = _collect()

    assert record.details["unattributed_results"] == 1
    assert "gemini_review" not in record.entries


def test_a_single_result_stays_visible(tmp_path):
    _write(tmp_path, "pr-706-gemini_review.json", _result(274.0))

    record = _collect()

    entry = record.entries["gemini_review"]
    assert entry.sample_size == 1
    assert entry.value == pytest.approx(274.0)


def test_completed_at_attributes_a_result_when_recorded_at_is_missing(tmp_path):
    _write(
        tmp_path,
        "pr-707-deepseek_gate.json",
        {"duration_seconds": 332.0, "completed_at": "2026-11-06T23:30:00Z"},
    )

    record = _collect()

    assert record.entries["deepseek_gate"].sample_size == 1
    assert record.details["unattributed_results"] == 0


def test_an_empty_results_directory_falls_back_to_a_scalar(tmp_path):
    _results_dir(tmp_path).mkdir(parents=True, exist_ok=True)

    record = _collect()

    assert isinstance(record, metric.ScalarMetric)
    assert record.value is None
    assert record.unknown_reason == NO_RESULTS
    assert record.details == {
        "unrecognised_files": 0,
        "unattributed_results": 0,
        "unreadable_files": 0,
        "no_duration_n": 0,
    }


def test_a_seat_with_only_durationless_results_is_unknown_for_the_day(tmp_path):
    _write(tmp_path, "pr-708-glm_gate.json", _result(None))

    record = _collect()

    entry = record.entries["glm_gate"]
    assert entry.value is None
    assert entry.unknown_reason == NO_DURATION
    assert entry.details["p75"] is None
    assert entry.details["samples"] == []
    # Decision 20: ``no_duration_on_day`` is permanent, so the day stays final.
    assert metric.is_transient_reason(NO_DURATION) is False
    assert metric.day_status({METRIC_ID: record.to_dict()}) == "final"


def test_an_unreadable_file_is_counted(tmp_path):
    _write(tmp_path, "pr-709-codex_gate.json", _result(5.0))
    _write(tmp_path, "pr-710-glm_gate.json", "{not json")

    record = _collect()

    assert record.details["unreadable_files"] == 1
    assert set(record.entries) == {"codex_gate"}


def test_the_glm_case_keeps_the_day_final(tmp_path):
    _write(tmp_path, "pr-711-codex_gate.json", _result(5.0))
    _write(tmp_path, "pr-712-glm_gate.json", _result(30.0, recorded="2026-11-03T09:00:00Z"))

    record = _collect()

    assert record.entries["glm_gate"].unknown_reason == NO_RESULTS
    assert metric.day_status({METRIC_ID: record.to_dict()}) == "final"


def test_discovery_finds_gates_without_failures():
    from daily_metrics import collect as collect_mod

    modules, failures = collect_mod.discover_sources(collect_mod.sources_dir())
    names = {module.__name__.rsplit(".", 1)[-1] for module in modules}

    assert "gates" in names
    assert failures == []
