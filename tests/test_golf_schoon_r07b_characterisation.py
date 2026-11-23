"""Characterisation tests for golf-schoon R07b: generate_markdown_report
of scripts/llm_benchmark.py. They pin behaviour before and after extract-method."""

import hashlib
import sys
from datetime import datetime as real_datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import llm_benchmark as lb


GOLDEN_FULL = "3f913f2eebb3ea391b4cbe873dc0c51a08e76e7e12005806b9fe3e5c26f57f5a"


class _FrozenDatetime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 14, 9, 30, 15)


def _entry(task_id, task_name, category, model, success=True, tps=10.0, dur=2.0,
           fmt=True, comp=1.0, missing=None, output="line1\nline2"):
    return {
        "task_id": task_id, "task_name": task_name, "category": category,
        "model": model, "success": success, "duration_seconds": dur,
        "tokens_generated": 10, "tokens_per_second": tps, "output": output,
        "output_length": len(output), "format_compliant": fmt,
        "fields_present": [], "fields_missing": missing or [],
        "completeness_score": comp, "quality": None, "error": None,
        "timestamp": "t",
    }


def _results():
    return {
        "t1__m1": _entry("t1", "Task One", "code", "m1", tps=20.0, dur=1.5),
        "t1__m2": _entry("t1", "Task One", "code", "m2", success=False, tps=0.0,
                         dur=9.0, fmt=False, comp=0.5, missing=["a", "b"]),
        "t2__m1": _entry("t2", "Task Two", "text", "m1", tps=30.0, comp=0.75,
                         output="x" * 700),
        "t3__m2": _entry("t3", "Task Three", "text", "m2", tps=5.0, fmt=False,
                         comp=0.25),
    }


def _config(no_think=False):
    return lb.BenchmarkConfig(models=["m1", "m2"], task_ids=["t1", "t2", "t3"],
                              include_claude=False, run_id="RUN1",
                              started_at="s", no_think=no_think)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(lb, "datetime", _FrozenDatetime)


def test_report_exact_snapshot(frozen):
    out = lb.generate_markdown_report(_results(), ["m1", "m2", "m3"], _config())
    assert hashlib.sha256(out.encode()).hexdigest() == GOLDEN_FULL


def test_report_header_and_sections(frozen):
    out = lb.generate_markdown_report(_results(), ["m1", "m2"], _config(no_think=True))
    lines = out.split("\n")
    assert lines[0] == "# LLM Benchmark Report -- 2026-11-14 09:30"
    assert "**Run ID**: RUN1" in lines
    assert "**Mode**: no-think" in lines
    assert "**Tasks**: 3" in lines
    assert "**Total runs**: 4" in lines
    assert "| m1 | 2/2 | 25.0 | 2/2 | 88% | 1.8s |" in lines
    assert "| m2 | 1/2 | 2.5 | 0/2 | 38% | 5.5s |" in lines
    assert "### Task One (`code`)" in lines
    assert "| Duration | 1.5s | FAIL |" in lines
    assert "| Duration | 2.0s | - |" in lines
    assert "| Missing fields | none | a, b |" in lines
    assert lines[-1] == "*Quality scores are null (reserved for manual 1-5 review).*"
    assert out.count("<details>") == 3


def test_report_default_mode_and_empty_model_skipped(frozen):
    out = lb.generate_markdown_report(_results(), ["m1", "ghost"], _config())
    assert "**Mode**: thinking (default)" in out
    assert "| ghost |" not in out.split("## Speed")[0]
    assert "  ghost" in out  # speed chart still lists it with 0.0


def test_report_preview_truncated_and_quoted(frozen):
    out = lb.generate_markdown_report(_results(), ["m1"], _config())
    assert "> " + "x" * 600 + "\n" in out
    assert "x" * 601 not in out
    assert "> line1\n> line2" in out


def test_report_recommendations(frozen):
    out = lb.generate_markdown_report(_results(), ["m1", "m2"], _config())
    assert "| code | **m1** | 1.50 | Avg 20 tok/s, 100% format compliance, composite 1.50/1.5 |" in out
    assert "| text | **m2** |" not in out
    assert "| text | **m1** | 1.25 | Avg 30 tok/s, 100% format compliance, composite 1.25/1.5 |" in out


def test_report_empty_results(frozen):
    out = lb.generate_markdown_report({}, ["m1"], _config())
    assert "**Tasks**: 0" in out and "**Total runs**: 0" in out
    assert "  m1 |" + "." * 40 + "| 0.0 tok/s" in out


# ---------------------------------------------------------------------------
