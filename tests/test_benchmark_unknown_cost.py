"""Benchmark readers of report ``cost_usd`` treat an unknown price as unknown.

Dispatch 20261020-kp-k1-r3 (OI-2194): a provider-lane report now carries
``cost_usd: null`` next to an ``unavailable:`` source when the price is unknown
(ADR-040). The benchmark harnesses read that field straight from the report
frontmatter and sum it per lane. Each of them must leave the null out of the
sum, never count it as 0, and say how many it left out.
"""

from __future__ import annotations

import csv
import importlib.util
import logging
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
RUNNERS = REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "runners"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(RUNNERS))

import reporter  # noqa: E402
import run_field_tests  # noqa: E402
import scorer  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_lane_bench = _load("run_lane_bench", REPO_ROOT / "scripts" / "benchmarks" / "run_lane_bench.py")
export_routing_matrix = _load(
    "export_routing_matrix", REPO_ROOT / "scripts" / "benchmark" / "export_routing_matrix.py"
)


def _write_report(path: Path, cost_usd, cost_source=None) -> Path:
    fm = {"schema_version": 1, "dispatch_id": path.stem, "cost_usd": cost_usd}
    if cost_source:
        fm["cost_source"] = cost_source
    # yaml.dump is what governance_emit.emit_unified_report writes.
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Response\n\nok\n", encoding="utf-8")
    return path


def _cell(lane: str, cost_usd, composite: float = 4.0) -> scorer.CellScore:
    return scorer.CellScore(
        lane_id=lane, task_id="t1", replication=1,
        correctness=5.0, completeness=5.0, cost_efficiency=0.0,
        wallclock_efficiency=5.0, code_quality=0.0, composite=composite,
        verify_evidence="", judge_reasoning="", cost_usd=cost_usd, wallclock_seconds=30.0,
    )


# ---------------------------------------------------------------------------
# scripts/benchmarks/run_lane_bench.py
# ---------------------------------------------------------------------------

def test_lane_bench_summary_counts_null_cost_instead_of_crashing(tmp_path):
    """Before this round ``float("null")`` raised and the summary was never written."""
    unpriced = run_lane_bench.parse_receipt(
        _write_report(tmp_path / "a.md", None, "unavailable:no_price:gpt-4o-mini")
    )
    assert unpriced["cost_usd"] == "null"
    priced = run_lane_bench.parse_receipt(_write_report(tmp_path / "b.md", 0.012, "m@2026-09-14"))

    def run(cost):
        return {"lane": "glm", "task": "codegen", "exit_code": 0, "wall_seconds": 10.0,
                "report_duration": "9.0", "quality_score": 5, "cost_usd": cost}

    runs = [run(unpriced["cost_usd"]), run(priced["cost_usd"]), run(""), run("0.0")]
    assert run_lane_bench._lane_cost_total(runs) == (pytest.approx(0.012), 2)

    out = tmp_path / "summary.md"
    run_lane_bench.write_summary(runs, out, Namespace(n=4))
    row = next(line for line in out.read_text().splitlines() if line.startswith("| glm |"))
    assert row.rstrip().endswith("| 0.01200 | 2 |")


# ---------------------------------------------------------------------------
# scripts/benchmark/field-tests/runners — scorer, reporter, CSV round trip
# ---------------------------------------------------------------------------

def test_scorer_reads_null_cost_as_unknown(tmp_path):
    assert scorer._extract_cost_from_report(
        _write_report(tmp_path / "null.md", None, "unavailable:no_price:x")
    ) is None
    assert scorer._extract_cost_from_report(
        _write_report(tmp_path / "priced.md", 0.0123, "m@2026-09-14")
    ) == pytest.approx(0.0123)
    assert scorer._extract_cost_from_report(_write_report(tmp_path / "free.md", 0.0)) == 0.0


def test_unknown_price_earns_no_free_run_cost_efficiency():
    assert scorer._compute_cost_efficiency(None, "t1_trivial") == 0.0
    assert scorer._compute_cost_efficiency(0.0, "t1_trivial") == 5.0


def test_reporter_sums_known_costs_and_counts_unknown(tmp_path):
    cells = [_cell("glm", 0.25), _cell("glm", None), _cell("glm", 0.05)]
    assert reporter._known_cost(cells) == (pytest.approx(0.30), 1)

    summary = tmp_path / "summary.md"
    reporter.write_summary_md(cells, summary, tasks_meta={"t1": {"tier": "t1_trivial"}})
    row = next(line for line in summary.read_text().splitlines() if line.startswith("| glm | 0.3000 |"))
    assert row.split("|")[3].strip() == "1"

    per_lane = tmp_path / "per_lane.md"
    reporter.write_per_lane_md(cells, per_lane)
    assert "Total cost: $0.3000 (known prices; 1 cell(s) unknown)" in per_lane.read_text()


def test_raw_csv_round_trip_keeps_unknown_cost_unknown(tmp_path):
    raw = tmp_path / "raw.csv"
    reporter.write_raw_csv([_cell("glm", None), _cell("kimi", 0.02)], raw)
    loaded = run_field_tests._load_prior_scores(raw, exclude=set())
    assert [c.cost_usd for c in loaded] == [None, pytest.approx(0.02)]
    assert run_field_tests._fmt_cost(None) == "unknown"
    assert run_field_tests._fmt_cost(0.02) == "$0.0200"


# ---------------------------------------------------------------------------
# scripts/benchmark/export_routing_matrix.py — reads the field-test raw.csv
# ---------------------------------------------------------------------------

def test_routing_matrix_keeps_unknown_cost_out_of_the_mean_and_counts_it(tmp_path, monkeypatch, caplog):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    rows = [
        {"lane_id": "glm-5-2", "task_id": "02_rls_policy", "composite": "4.0",
         "wallclock_seconds": "60", "cost_usd": ""},
        {"lane_id": "glm-5-2", "task_id": "03_dotenv_script", "composite": "3.0",
         "wallclock_seconds": "60", "cost_usd": "0.04"},
    ]
    with (run_dir / "raw.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(export_routing_matrix, "RESULTS", tmp_path)

    with caplog.at_level(logging.INFO, logger="export_routing_matrix"):
        cells = export_routing_matrix.aggregate()
    cell = cells[("01_code_generation", "glm-5.2", "flat-runner")]
    assert cell["costs"] == [pytest.approx(0.04)]
    assert cell["cost_unknown"] == 1
    assert "1 genuine run(s) carry an unknown cost_usd" in caplog.text
