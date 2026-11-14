"""Characterisation tests for golf-schoon R07: generate_markdown_report and main
of scripts/llm_benchmark.py. They pin behaviour before and after extract-method."""

import hashlib
import json
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
# main
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    rd = tmp_path / "reports"
    monkeypatch.setattr(lb, "REPORT_DIR", rd)
    monkeypatch.setattr(lb, "PROGRESS_FILE", rd / "benchmark_progress.json")
    monkeypatch.setattr(lb, "datetime", _FrozenDatetime)
    monkeypatch.setattr(lb, "check_ollama_available", lambda: True)
    monkeypatch.setattr(lb, "list_ollama_models", lambda: ["qwen3.5:9b", "other"])
    calls = []

    def fake_ollama(model, prompt, no_think=False, timeout=0):
        calls.append((model, no_think, timeout))
        return lb.Result(
            task_id="", task_name="", category="", model=model, success=True,
            duration_seconds=2.0, tokens_generated=20, tokens_per_second=10.0,
            output="{}", output_length=2, format_compliant=True,
            fields_present=[], fields_missing=[], completeness_score=0.5,
            quality=None, error=None, timestamp="ts")

    monkeypatch.setattr(lb, "run_ollama", fake_ollama)
    monkeypatch.setattr(lb, "score_result", lambda r, t: r)
    monkeypatch.setattr(lb, "NUM_PREDICT", lb.NUM_PREDICT)
    monkeypatch.setattr(lb, "NUM_CTX", lb.NUM_CTX)
    return rd, calls


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["llm_benchmark.py", *argv])
    lb.main()


def test_main_ollama_down_exit_1(monkeypatch, env, capsys):
    monkeypatch.setattr(lb, "check_ollama_available", lambda: False)
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR: Ollama not available at " + lb.OLLAMA_URL in out
    assert "Start with: ollama serve" in out


def test_main_no_local_models_exit_1(monkeypatch, env, capsys):
    monkeypatch.setattr(lb, "list_ollama_models", lambda: [])
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "No local models found. Available: []" in out
    assert "Pull a model with: ollama pull qwen3.5:9b" in out


def test_main_no_tasks_exit_1(monkeypatch, env, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "--tasks", "nonexistent")
    assert e.value.code == 1
    assert "ERROR: No valid tasks selected" in capsys.readouterr().out


def test_main_happy_path_files_and_stdout(monkeypatch, env, capsys):
    rd, calls = env
    _run(monkeypatch, "--tasks", "session_analysis,test_generation",
         "--no-think", "--timeout", "7", "--num-predict", "111", "--num-ctx", "222")
    assert lb.NUM_PREDICT == 111 and lb.NUM_CTX == 222
    assert calls == [("qwen3.5:9b", True, 7)] * 2
    out = capsys.readouterr().out
    assert "  VNX LLM Benchmark (Coding/Technical)" in out
    assert "Models:  qwen3.5:9b" in out
    assert "Tasks:   session_analysis, test_generation (2 total)" in out
    assert "Est:     ~3 minutes" in out
    assert "Resume:" not in out
    assert "  [1/2] " in out and "OK (2.0s, 10.0 tok/s, fmt=Y, comp=50%)" in out
    assert "  qwen3.5:9b" in out and "2/2 ok" in out and "fmt 2/2  comp 50%" in out
    assert f"  Report:   {rd / 'benchmark_20261114_093015.md'}" in out
    assert f"  Raw data: {rd / 'benchmark_20261114_093015.json'}" in out
    assert f"  Progress: {lb.PROGRESS_FILE}" in out
    assert out.index("RESULTS") > out.index("OK (")
    md = (rd / "benchmark_20261114_093015.md").read_text(encoding="utf-8")
    assert "**Run ID**: 20261114_093015" in md and "**Mode**: no-think" in md
    raw = json.loads((rd / "benchmark_20261114_093015.json").read_text(encoding="utf-8"))
    assert raw["config"]["run_id"] == "20261114_093015"
    assert raw["config"]["models"] == ["qwen3.5:9b"]
    assert raw["config"]["no_think"] is True
    assert set(raw["results"]) == {"session_analysis__qwen3.5:9b",
                                   "test_generation__qwen3.5:9b"}


def test_main_marketing_quick_resume_prints_resume(monkeypatch, env, capsys):
    rd, calls = env
    _run(monkeypatch, "--marketing", "--quick", "--resume")
    out = capsys.readouterr().out
    assert "(Marketing/MKB)" in out
    assert f"Resume:  from {lb.PROGRESS_FILE}" in out
    assert len(calls) == len(lb.QUICK_MARKETING_IDS)


def test_main_claude_only_skips_ollama_check(monkeypatch, env, capsys):
    rd, calls = env
    monkeypatch.setattr(lb, "check_ollama_available", lambda: False)

    def fake_claude(prompt, model_key="claude-sonnet"):
        return lb.Result(
            task_id="", task_name="", category="", model=model_key, success=False,
            duration_seconds=1.0, tokens_generated=0, tokens_per_second=0.0,
            output="", output_length=0, format_compliant=False, fields_present=[],
            fields_missing=[], completeness_score=0.0, quality=None,
            error="boom", timestamp="ts")

    monkeypatch.setattr(lb, "run_claude", fake_claude)
    _run(monkeypatch, "--models", "none", "--include-claude", "--tasks", "session_analysis")
    out = capsys.readouterr().out
    assert "Models:  claude-haiku, claude-sonnet" in out
    assert "FAIL: boom" in out
    assert calls == []
    raw = json.loads((rd / "benchmark_20261114_093015.json").read_text(encoding="utf-8"))
    assert raw["config"]["models"] == [] and raw["config"]["include_claude"] is True


def test_main_no_results_exit_1(monkeypatch, env, capsys):
    monkeypatch.setattr(lb, "run_benchmark", lambda *a, **k: {})
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "--tasks", "session_analysis")
    assert e.value.code == 1
    assert "No results collected." in capsys.readouterr().out
    assert not list(env[0].glob("benchmark_2026*"))


def test_main_model_fallbacks(monkeypatch, env, capsys):
    monkeypatch.setattr(lb, "list_ollama_models", lambda: ["a", "Qwen-x", "b", "c", "d"])
    _run(monkeypatch, "--tasks", "session_analysis")
    assert "Models:  Qwen-x\n" in capsys.readouterr().out
    monkeypatch.setattr(lb, "list_ollama_models", lambda: ["a", "b", "c", "d", "e"])
    _run(monkeypatch, "--tasks", "session_analysis")
    assert "Models:  a, b, c, d\n" in capsys.readouterr().out
