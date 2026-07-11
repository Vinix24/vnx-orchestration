"""Tests for scripts/benchmark/ suite: loader, runner, judge, analyzer."""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers to import benchmark modules without installed package
# ---------------------------------------------------------------------------

import sys
BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "scripts" / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import run_benchmark
import judge_quality
import analyze_results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_prompts(tmp_path: Path) -> Path:
    p = tmp_path / "prompts"
    p.mkdir()
    (p / "01_alpha.txt").write_text("Do task alpha.")
    (p / "02_beta.txt").write_text("Do task beta.")
    return p


@pytest.fixture()
def tmp_models_yaml(tmp_path: Path) -> Path:
    data = {
        "models": [
            {
                "id": "claude-sonnet-4-6",
                "provider": "claude",
                "model_arg": "sonnet",
                "cost_input_mtok": 3.00,
                "cost_output_mtok": 15.00,
            },
            {
                "id": "deepseek-v4-pro",
                "provider": "litellm:deepseek",
                "model_arg": "deepseek-v4-pro",
                "cost_input_mtok": 0.435,
                "cost_output_mtok": 0.87,
            },
        ]
    }
    path = tmp_path / "models.yaml"
    path.write_text(yaml.dump(data))
    return path


@pytest.fixture()
def tmp_results(tmp_path: Path) -> Path:
    d = tmp_path / "results"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# test_load_models_yaml
# ---------------------------------------------------------------------------

def test_load_models_yaml_valid(tmp_models_yaml: Path):
    models = run_benchmark.load_models(tmp_models_yaml)
    assert len(models) == 2
    assert models[0]["id"] == "claude-sonnet-4-6"
    assert models[1]["cost_input_mtok"] == 0.435


def test_load_models_yaml_fields_present(tmp_models_yaml: Path):
    models = run_benchmark.load_models(tmp_models_yaml)
    required = {"id", "provider", "model_arg", "cost_input_mtok", "cost_output_mtok"}
    for m in models:
        assert required.issubset(m.keys()), f"Missing fields in {m}"


def test_load_models_yaml_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        run_benchmark.load_models(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# test_load_tasks_from_prompts_dir
# ---------------------------------------------------------------------------

def test_load_tasks_from_prompts_dir_count(tmp_prompts: Path):
    tasks = run_benchmark.load_tasks(tmp_prompts)
    assert len(tasks) == 2


def test_load_tasks_from_prompts_dir_ids(tmp_prompts: Path):
    tasks = run_benchmark.load_tasks(tmp_prompts)
    ids = [t["id"] for t in tasks]
    assert "01_alpha" in ids
    assert "02_beta" in ids


def test_load_tasks_from_prompts_dir_prompt_content(tmp_prompts: Path):
    tasks = run_benchmark.load_tasks(tmp_prompts)
    by_id = {t["id"]: t for t in tasks}
    assert "Do task alpha." in by_id["01_alpha"]["prompt"]


def test_load_tasks_from_prompts_dir_sorted(tmp_prompts: Path):
    tasks = run_benchmark.load_tasks(tmp_prompts)
    assert tasks[0]["id"] == "01_alpha"
    assert tasks[1]["id"] == "02_beta"


# ---------------------------------------------------------------------------
# test_run_single_captures_receipt
# ---------------------------------------------------------------------------

def _make_receipt_ndjson(dispatch_id: str) -> str:
    record = {
        "dispatch_id": dispatch_id,
        "input_tokens": 1000,
        "output_tokens": 500,
        "exit_code": 0,
    }
    return json.dumps(record) + "\n"


def test_run_single_captures_receipt(tmp_path: Path, tmp_prompts: Path, tmp_models_yaml: Path):
    model = run_benchmark.load_models(tmp_models_yaml)[0]
    task = run_benchmark.load_tasks(tmp_prompts)[0]
    dispatch_id = "bench-test-dispatch-123"

    receipts_dir = tmp_path / ".vnx-data" / "receipts"
    receipts_dir.mkdir(parents=True)
    receipt_path = receipts_dir / "t0_receipts.ndjson"
    receipt_path.write_text(_make_receipt_ndjson(dispatch_id))

    reports_dir = tmp_path / ".vnx-data" / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_path = reports_dir / f"{dispatch_id}_report.md"
    report_path.write_text("## Response\nThis is the model output.\n## Metadata\nsome meta")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = ""
    fake_proc.stderr = ""

    with patch("subprocess.run", return_value=fake_proc) as mock_run, \
         patch.object(run_benchmark, "REPO_ROOT", tmp_path):
        result = run_benchmark.run_single(model, task, dispatch_id)

    assert result["dispatch_id"] == dispatch_id
    assert result["model_id"] == model["id"]
    assert result["task_id"] == task["id"]
    assert result["receipt"]["input_tokens"] == 1000
    assert "This is the model output." in result["response"]
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# test_extract_response_from_report
# ---------------------------------------------------------------------------

def test_extract_response_from_report_basic():
    report = "# Title\n## Response\nHello world.\n## Metadata\nsome info"
    result = run_benchmark._extract_response_from_report(report)
    assert result == "Hello world."


def test_extract_response_from_report_no_section():
    report = "# Title\nNo response header here."
    result = run_benchmark._extract_response_from_report(report)
    assert result == ""


def test_extract_response_from_report_trailing_content():
    report = "## Response\nLine 1.\nLine 2.\n## NextSection\nother"
    result = run_benchmark._extract_response_from_report(report)
    assert "Line 1." in result
    assert "other" not in result


# ---------------------------------------------------------------------------
# test_judge_quality_parses_opus_response
# ---------------------------------------------------------------------------

def test_judge_quality_parses_valid_json():
    raw = '{"quality_score": 8, "correctness": true, "completeness_score": 7, "notable_issues": ""}'
    score = judge_quality._parse_judge_response(raw)
    assert score["quality_score"] == 8
    assert score["correctness"] is True
    assert score["completeness_score"] == 7
    assert score["notable_issues"] == ""


def test_judge_quality_parses_fenced_json():
    raw = '```json\n{"quality_score": 5, "correctness": false, "completeness_score": 6, "notable_issues": "missing examples"}\n```'
    score = judge_quality._parse_judge_response(raw)
    assert score["quality_score"] == 5
    assert score["correctness"] is False


def test_judge_quality_fallback_on_invalid():
    score = judge_quality._parse_judge_response("not valid json at all {{{")
    assert score["quality_score"] == 0
    assert score["correctness"] is False
    assert "judge failed" in score["notable_issues"]


def test_judge_quality_missing_fields_defaults():
    raw = '{"quality_score": 9}'
    score = judge_quality._parse_judge_response(raw)
    assert score["quality_score"] == 9
    assert score["completeness_score"] == 0


# ---------------------------------------------------------------------------
# test_analyze_results_writes_markdown
# ---------------------------------------------------------------------------

def _make_result_record(model_id: str, task_id: str, cost: float, score: float) -> dict:
    return {
        "model_id": model_id,
        "task_id": task_id,
        "duration_seconds": 10.0,
        "cost_usd": cost,
        "response": "some response",
        "receipt": {},
        "judge_scores": {
            "quality_score": int(score),
            "correctness": True,
            "completeness_score": int(score),
            "notable_issues": "",
        },
    }


def test_analyze_results_writes_markdown(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    output_dir = tmp_path / "claudedocs"

    records = [
        _make_result_record("claude-sonnet-4-6", "01_code_gen", 0.01, 8),
        _make_result_record("deepseek-v4-pro", "01_code_gen", 0.001, 6),
    ]
    for r in records:
        path = results_dir / f"{r['model_id']}__{r['task_id']}.json"
        path.write_text(json.dumps(r))

    with patch.object(analyze_results, "ROUTING_OUTPUT", tmp_path / "routing.yaml"), \
         patch.object(analyze_results, "CLAUDEDOCS_DIR", output_dir):
        result = analyze_results.main.__wrapped__ if hasattr(analyze_results.main, "__wrapped__") else None

    loaded = analyze_results.load_results(results_dir)
    summary = analyze_results.build_task_summary(loaded)
    pareto = analyze_results.build_pareto_frontier(loaded)
    report = analyze_results.render_markdown_report(loaded, summary, pareto)

    assert "# Benchmark Model Comparison Report" in report
    assert "claude-sonnet-4-6" in report
    assert "deepseek-v4-pro" in report
    assert "01_code_gen" in report


# ---------------------------------------------------------------------------
# test_analyze_results_writes_yaml_recommendations
# ---------------------------------------------------------------------------

def test_analyze_results_writes_yaml_recommendations(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    records = [
        _make_result_record("claude-sonnet-4-6", "01_alpha", 0.01, 8),
        _make_result_record("deepseek-v4-pro", "01_alpha", 0.001, 6),
        _make_result_record("claude-sonnet-4-6", "02_beta", 0.02, 9),
    ]
    for r in records:
        path = results_dir / f"{r['model_id']}__{r['task_id']}.json"
        path.write_text(json.dumps(r))

    loaded = analyze_results.load_results(results_dir)
    summary = analyze_results.build_task_summary(loaded)
    routing = analyze_results.build_routing_recommendations(summary)

    routing_path = tmp_path / "routing.yaml"
    analyze_results._atomic_write(routing_path, yaml.dump(routing, default_flow_style=False, allow_unicode=True))

    assert routing_path.exists()
    data = yaml.safe_load(routing_path.read_text())
    assert "routing_by_task" in data
    assert "01_alpha" in data["routing_by_task"]
    # Highest score model should rank first
    first = data["routing_by_task"]["01_alpha"][0]
    assert first["model_id"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# test_pareto_frontier
# ---------------------------------------------------------------------------

def test_pareto_frontier_excludes_dominated():
    records = [
        _make_result_record("model-a", "task1", 0.01, 9),
        _make_result_record("model-b", "task1", 0.01, 7),  # dominated: same cost, lower score
    ]
    pareto = analyze_results.build_pareto_frontier(records)
    model_ids = {p["model_id"] for p in pareto}
    assert "model-a" in model_ids
    # model-b is dominated by model-a (same cost, lower score) — must not appear
    assert "model-b" not in model_ids


def test_pareto_frontier_empty_when_no_costs():
    records = [_make_result_record("model-a", "task1", 0.0, 8)]
    records[0]["cost_usd"] = None
    pareto = analyze_results.build_pareto_frontier(records)
    assert pareto == []


# ---------------------------------------------------------------------------
# test_atomic_write_creates_tmp_then_replace
# ---------------------------------------------------------------------------

def test_atomic_write_creates_tmp_then_replace(tmp_path: Path):
    target = tmp_path / "result.json"
    obj = {"model_id": "claude-sonnet-4-6", "task_id": "01_alpha", "cost_usd": 0.01}
    run_benchmark._atomic_write_json(target, obj)
    assert target.exists()
    # tmp file must be gone after replace
    assert not (tmp_path / "result.json.tmp").exists()
    loaded = json.loads(target.read_text())
    assert loaded["model_id"] == "claude-sonnet-4-6"


def test_atomic_write_overwrites_existing(tmp_path: Path):
    target = tmp_path / "result.json"
    target.write_text(json.dumps({"old": True}))
    run_benchmark._atomic_write_json(target, {"new": True})
    loaded = json.loads(target.read_text())
    assert loaded == {"new": True}


# ---------------------------------------------------------------------------
# test_litellm_model_arg_passed_via_env
# ---------------------------------------------------------------------------

def test_litellm_model_arg_passed_via_env(tmp_path: Path, tmp_prompts: Path, tmp_models_yaml: Path):
    model = run_benchmark.load_models(tmp_models_yaml)[1]  # deepseek-v4-pro, provider=litellm:deepseek
    assert model["provider"] == "litellm:deepseek"
    task = run_benchmark.load_tasks(tmp_prompts)[0]
    dispatch_id = "bench-test-litellm-456"

    receipts_dir = tmp_path / ".vnx-data" / "receipts"
    receipts_dir.mkdir(parents=True)
    (receipts_dir / "t0_receipts.ndjson").write_text("")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = ""
    fake_proc.stderr = ""

    captured_env = {}

    def fake_run(cmd, env=None, **kwargs):
        captured_env.update(env or {})
        return fake_proc

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(run_benchmark, "REPO_ROOT", tmp_path):
        run_benchmark.run_single(model, task, dispatch_id)

    assert "VNX_LITELLM_MODEL" in captured_env
    assert captured_env["VNX_LITELLM_MODEL"] == "deepseek/deepseek-v4-pro"


def test_claude_model_arg_passed_via_cmd_not_env(tmp_path: Path, tmp_prompts: Path, tmp_models_yaml: Path):
    model = run_benchmark.load_models(tmp_models_yaml)[0]  # claude-sonnet-4-6
    assert model["provider"] == "claude"
    task = run_benchmark.load_tasks(tmp_prompts)[0]
    dispatch_id = "bench-test-claude-789"

    receipts_dir = tmp_path / ".vnx-data" / "receipts"
    receipts_dir.mkdir(parents=True)
    (receipts_dir / "t0_receipts.ndjson").write_text("")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = ""
    fake_proc.stderr = ""

    captured_cmd = []
    captured_env = {}

    def fake_run(cmd, env=None, **kwargs):
        captured_cmd.extend(cmd)
        captured_env.update(env or {})
        return fake_proc

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(run_benchmark, "REPO_ROOT", tmp_path):
        run_benchmark.run_single(model, task, dispatch_id)

    assert "--model" in captured_cmd
    assert "VNX_LITELLM_MODEL" not in captured_env


# ---------------------------------------------------------------------------
# test_glm_uses_zai_provider
# ---------------------------------------------------------------------------

def test_glm_uses_zai_provider():
    models = run_benchmark.load_models()
    glm = next((m for m in models if m["id"] == "glm-5-1"), None)
    assert glm is not None, "glm-5-1 not found in models.yaml"
    # glm-5-1 was corrected in models.yaml to map to the REAL GLM-5.1 (litellm:zai:glm-5.1 /
    # model_arg glm-5.1), not the base glm-5 alias. The zai sub-provider prefix is what satisfies
    # zai-via-openrouter-only; the :glm-5.1 suffix selects the real model.
    assert glm["provider"].startswith("litellm:zai"), f"Expected litellm:zai*, got {glm['provider']}"
    assert glm["model_arg"] == "glm-5.1", f"Expected glm-5.1, got {glm['model_arg']}"


# ---------------------------------------------------------------------------
# test_analyze_warns_on_malformed_files
# ---------------------------------------------------------------------------

def test_analyze_warns_on_malformed_files(tmp_path: Path, capsys):
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    # One valid file
    good = _make_result_record("claude-sonnet-4-6", "01_alpha", 0.01, 8)
    (results_dir / "claude-sonnet-4-6__01_alpha.json").write_text(json.dumps(good))

    # One malformed file
    (results_dir / "broken__01_alpha.json").write_text("this is not valid json {{{")

    records = analyze_results.load_results(results_dir)

    captured = capsys.readouterr()
    assert len(records) == 1
    assert records[0]["model_id"] == "claude-sonnet-4-6"
    assert "WARNING" in captured.err
    assert "1 unreadable result" in captured.err


def test_analyze_warns_on_unreadable_file(tmp_path: Path, capsys):
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    good = _make_result_record("model-a", "task1", 0.01, 7)
    (results_dir / "model-a__task1.json").write_text(json.dumps(good))

    bad_path = results_dir / "bad__task1.json"
    bad_path.write_text("")  # OSError equivalent: empty → JSONDecodeError

    records = analyze_results.load_results(results_dir)
    captured = capsys.readouterr()

    assert len(records) == 1
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# OI-225: --tasks-dir / --models-file CLI + VNX_BENCH_* env fallback
# (bring-your-own-tasks generalization seam — decouples the runner from the
# bundled repo-specific prompts/models.yaml without packaging the harness)
# ---------------------------------------------------------------------------

def test_dry_run_uses_custom_tasks_dir_and_models_file(
    tmp_prompts: Path, tmp_models_yaml: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(sys, "argv", [
        "run_benchmark.py", "--dry-run",
        "--tasks-dir", str(tmp_prompts),
        "--models-file", str(tmp_models_yaml),
    ])
    rc = run_benchmark.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "claude-sonnet-4-6 x 01_alpha" in out
    assert "deepseek-v4-pro x 02_beta" in out
    # bundled repo seeds must not leak in when a custom source is given
    assert "01_code_generation" not in out
    assert "claude-opus-4-8" not in out


def test_dry_run_env_var_fallback_for_tasks_and_models(
    tmp_prompts: Path, tmp_models_yaml: Path, monkeypatch, capsys,
):
    monkeypatch.setenv("VNX_BENCH_TASKS_DIR", str(tmp_prompts))
    monkeypatch.setenv("VNX_BENCH_MODELS_FILE", str(tmp_models_yaml))
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--dry-run"])

    rc = run_benchmark.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "claude-sonnet-4-6 x 01_alpha" in out


def test_dry_run_cli_flag_overrides_env_var(
    tmp_prompts: Path, tmp_models_yaml: Path, tmp_path: Path, monkeypatch, capsys,
):
    # A second, distinct task source set via env — the explicit CLI flag must win.
    other_prompts = tmp_path / "other_prompts"
    other_prompts.mkdir()
    (other_prompts / "99_gamma.txt").write_text("Do task gamma.")

    monkeypatch.setenv("VNX_BENCH_TASKS_DIR", str(other_prompts))
    monkeypatch.setattr(sys, "argv", [
        "run_benchmark.py", "--dry-run",
        "--tasks-dir", str(tmp_prompts),
        "--models-file", str(tmp_models_yaml),
    ])

    rc = run_benchmark.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "01_alpha" in out
    assert "99_gamma" not in out


def test_dry_run_defaults_to_bundled_seeds_when_unset(monkeypatch, capsys):
    monkeypatch.delenv("VNX_BENCH_TASKS_DIR", raising=False)
    monkeypatch.delenv("VNX_BENCH_MODELS_FILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--dry-run"])

    rc = run_benchmark.main()
    out = capsys.readouterr().out

    assert rc == 0
    # bundled prompt/model still reachable when no override is given (backward-compat)
    assert "01_code_generation" in out
    assert "claude-opus-4-8" in out


def test_judge_quality_tasks_dir_cli_overrides_bundled_prompts(
    tmp_path: Path, monkeypatch,
):
    custom_prompts = tmp_path / "prompts"
    custom_prompts.mkdir()
    (custom_prompts / "01_alpha.txt").write_text("Custom bring-your-own task alpha.")

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    record = {
        "model_id": "claude-sonnet-4-6",
        "task_id": "01_alpha",
        "response": "some response",
    }
    (results_dir / "claude-sonnet-4-6__01_alpha.json").write_text(json.dumps(record))

    captured_prompts = []

    def fake_call_judge(prompt, model, timeout):
        captured_prompts.append(prompt)
        return '{"quality_score": 5, "correctness": true, "completeness_score": 5, "notable_issues": ""}'

    monkeypatch.setattr(sys, "argv", [
        "judge_quality.py",
        "--results-dir", str(results_dir),
        "--tasks-dir", str(custom_prompts),
    ])

    with patch.object(judge_quality, "_call_judge", side_effect=fake_call_judge):
        rc = judge_quality.main()

    assert rc == 0
    assert len(captured_prompts) == 1
    assert "Custom bring-your-own task alpha." in captured_prompts[0]
