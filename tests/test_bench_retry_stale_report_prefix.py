"""Regression test for run_field_tests._load_dnf_cells_from_csv stale-report matching.

F5 (PR #831, deferred 1.0.1): the DNF soft-signal used to prefix-match report
filenames on (lane_id, task_id, replication) only — `bench-{lane}-{task}-r{rep}-`.
That prefix has no timestamp discriminator, so ANY historical report for that
cell (e.g. left over from an earlier attempt in the same --max-retries loop, or
from a completely different prior run) counted as "report present" for THIS
row, even when the row's own dispatch never produced one. A genuinely-failed
short-wallclock cell was then silently excluded from --retry-from and its bad
score stayed in the consolidated output.

Fix: CellScore/raw.csv now carries this row's own dispatch_id (bench-<lane>-
<task>-r<rep>-<ts>), and the soft-signal check matches that exact dispatch_id's
report file instead of the coarse prefix. Legacy raw.csv files without a
dispatch_id column fall back to the old prefix heuristic.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark" / "field-tests" / "runners"),
)

import lane_adapter  # noqa: E402
import run_field_tests  # noqa: E402


RAW_CSV_FIELDS = [
    "lane_id", "task_id", "replication", "correctness", "completeness",
    "cost_efficiency", "wallclock_efficiency", "code_quality", "composite",
    "verify_evidence", "judge_reasoning", "cost_usd", "wallclock_seconds",
    "input_tokens", "output_tokens", "tokens_per_second", "dispatch_id",
]


def _write_raw_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in RAW_CSV_FIELDS})


def _base_row(**overrides) -> dict:
    row = {
        "lane_id": "claude-sonnet-4-6",
        "task_id": "01_yaml_config_refactor",
        "replication": 1,
        "correctness": 0.0,
        "completeness": 0.0,
        "cost_efficiency": 0.0,
        "wallclock_efficiency": 0.0,
        "code_quality": 0.0,
        "composite": 0.0,
        "verify_evidence": "",
        "judge_reasoning": "",
        "cost_usd": 0.0,
        "wallclock_seconds": 2.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tokens_per_second": 0.0,
        "dispatch_id": "",
    }
    row.update(overrides)
    return row


def test_stale_historical_report_no_longer_masks_a_real_dnf(tmp_path, monkeypatch):
    """A short-wallclock row with NO report of its own must be flagged DNF,
    even when an older report for the same (lane, task, rep) sits on disk."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", (reports_dir,))

    # Stale report from an EARLIER attempt at the same (lane, task, rep) — a
    # different timestamp, hence a different dispatch_id.
    (reports_dir / "bench-claude-sonnet-4-6-01_yaml_config_refactor-r1-20260601-000000.md").write_text(
        "stale earlier attempt", encoding="utf-8",
    )

    row = _base_row(
        wallclock_seconds=2.0,
        composite=0.0,
        dispatch_id="bench-claude-sonnet-4-6-01_yaml_config_refactor-r1-20260602-000000",
    )
    csv_path = tmp_path / "raw.csv"
    _write_raw_csv(csv_path, [row])

    dnf = run_field_tests._load_dnf_cells_from_csv(csv_path)

    assert ("claude-sonnet-4-6", "01_yaml_config_refactor", 1) in dnf


def test_row_with_its_own_report_is_not_flagged_dnf(tmp_path, monkeypatch):
    """A short-wallclock row whose OWN dispatch_id report exists must be
    treated as healthy (not re-run)."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", (reports_dir,))

    dispatch_id = "bench-claude-sonnet-4-6-01_yaml_config_refactor-r1-20260602-000000"
    (reports_dir / f"{dispatch_id}.md").write_text("real report", encoding="utf-8")

    row = _base_row(wallclock_seconds=2.0, composite=3.0, dispatch_id=dispatch_id)
    csv_path = tmp_path / "raw.csv"
    _write_raw_csv(csv_path, [row])

    dnf = run_field_tests._load_dnf_cells_from_csv(csv_path)

    assert ("claude-sonnet-4-6", "01_yaml_config_refactor", 1) not in dnf


def test_legacy_csv_without_dispatch_id_column_falls_back_to_prefix(tmp_path, monkeypatch):
    """Pre-fix raw.csv files have no dispatch_id column at all — the reader
    must not crash and must preserve the old prefix-based behaviour."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", (reports_dir,))

    (reports_dir / "bench-claude-sonnet-4-6-01_yaml_config_refactor-r1-20260601-000000.md").write_text(
        "legacy report", encoding="utf-8",
    )

    legacy_fields = [f for f in RAW_CSV_FIELDS if f != "dispatch_id"]
    row = _base_row(wallclock_seconds=2.0, composite=0.0)
    csv_path = tmp_path / "raw.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=legacy_fields)
        writer.writeheader()
        writer.writerow({k: row[k] for k in legacy_fields})

    dnf = run_field_tests._load_dnf_cells_from_csv(csv_path)

    # Old (imprecise) behaviour: any historical report matching the prefix
    # counts as present, so this cell is NOT flagged DNF.
    assert ("claude-sonnet-4-6", "01_yaml_config_refactor", 1) not in dnf
