"""Regression tests for the 3 benchmark blockers + 2 quick-wins.

Dispatch-ID: 20260517-fix-benchmark-cluster

Covers:
  1. run_benchmark.run_single uses 'BENCH' terminal-id (not 'T2')
  2. judge_quality._call_judge streams prompt via stdin (not CLI arg)
  3. judge_quality.anonymize_model_id produces stable anonymized label
  4. models.yaml no longer contains duplicate kimi-k2-0905 entry
  5. prompts/02_code_review.txt no longer leaks expected issue count
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

import sys
BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "scripts" / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import run_benchmark
import judge_quality


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_prompts(tmp_path: Path) -> Path:
    p = tmp_path / "prompts"
    p.mkdir()
    (p / "01_alpha.txt").write_text("Do task alpha.")
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
        ]
    }
    path = tmp_path / "models.yaml"
    path.write_text(yaml.dump(data))
    return path


# ---------------------------------------------------------------------------
# BLOCKER 1: terminal-id defaults to BENCH, not T2
# ---------------------------------------------------------------------------

class TestTerminalIdBench:
    def test_run_single_uses_bench_terminal_id_by_default(
        self, tmp_path: Path, tmp_prompts: Path, tmp_models_yaml: Path,
    ):
        model = run_benchmark.load_models(tmp_models_yaml)[0]
        task = run_benchmark.load_tasks(tmp_prompts)[0]
        dispatch_id = "bench-fix-tid-001"

        receipts_dir = tmp_path / ".vnx-data" / "receipts"
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "t0_receipts.ndjson").write_text("")

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = ""
        fake_proc.stderr = ""

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(run_benchmark, "REPO_ROOT", tmp_path):
            run_benchmark.run_single(model, task, dispatch_id)

        tid_idx = captured_cmd.index("--terminal-id")
        assert captured_cmd[tid_idx + 1] == "BENCH"

    def test_run_single_accepts_custom_terminal_id(
        self, tmp_path: Path, tmp_prompts: Path, tmp_models_yaml: Path,
    ):
        model = run_benchmark.load_models(tmp_models_yaml)[0]
        task = run_benchmark.load_tasks(tmp_prompts)[0]
        dispatch_id = "bench-fix-tid-002"

        receipts_dir = tmp_path / ".vnx-data" / "receipts"
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "t0_receipts.ndjson").write_text("")

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = ""
        fake_proc.stderr = ""

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(run_benchmark, "REPO_ROOT", tmp_path):
            run_benchmark.run_single(model, task, dispatch_id, terminal_id="CUSTOM")

        tid_idx = captured_cmd.index("--terminal-id")
        assert captured_cmd[tid_idx + 1] == "CUSTOM"

    def test_t2_never_appears_in_default_cmd(
        self, tmp_path: Path, tmp_prompts: Path, tmp_models_yaml: Path,
    ):
        model = run_benchmark.load_models(tmp_models_yaml)[0]
        task = run_benchmark.load_tasks(tmp_prompts)[0]
        dispatch_id = "bench-fix-tid-003"

        receipts_dir = tmp_path / ".vnx-data" / "receipts"
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "t0_receipts.ndjson").write_text("")

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = ""
        fake_proc.stderr = ""

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(run_benchmark, "REPO_ROOT", tmp_path):
            run_benchmark.run_single(model, task, dispatch_id)

        assert "T2" not in captured_cmd


# ---------------------------------------------------------------------------
# BLOCKER 2: judge prompt via stdin (not CLI arg)
# ---------------------------------------------------------------------------

class TestJudgeStdinPrompt:
    def test_call_judge_uses_stdin_not_argv(self):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ('{"quality_score": 8}', "")
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = judge_quality._call_judge("test prompt content", "opus", 120)

        args_list = mock_popen.call_args[0][0]
        assert "test prompt content" not in args_list
        assert args_list == ["claude", "-p", "--model", "opus"]

        mock_proc.communicate.assert_called_once_with(
            input="test prompt content", timeout=120,
        )

    def test_call_judge_stdin_prevents_process_table_exposure(self):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ('{"result": "ok"}', "")
        mock_proc.returncode = 0

        large_prompt = "A" * 300_000

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            judge_quality._call_judge(large_prompt, "opus", 120)

        args_list = mock_popen.call_args[0][0]
        for arg in args_list:
            assert len(arg) < 1000

    def test_call_judge_handles_broken_pipe(self):
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = BrokenPipeError("pipe gone")

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="BrokenPipeError"):
                judge_quality._call_judge("prompt", "opus", 120)

    def test_call_judge_handles_timeout(self):
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd="claude", timeout=120,
        )
        mock_proc.kill = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(subprocess.TimeoutExpired):
                judge_quality._call_judge("prompt", "opus", 120)
        mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# BLOCKER 3: anonymized model_id in judge prompt
# ---------------------------------------------------------------------------

class TestAnonymizedModelId:
    def test_anonymize_produces_stable_hash(self):
        label1 = judge_quality.anonymize_model_id("claude-opus-4-6")
        label2 = judge_quality.anonymize_model_id("claude-opus-4-6")
        assert label1 == label2

    def test_anonymize_hides_real_model_name(self):
        label = judge_quality.anonymize_model_id("claude-opus-4-6")
        assert "claude" not in label.lower()
        assert "opus" not in label.lower()

    def test_anonymize_format_matches_spec(self):
        label = judge_quality.anonymize_model_id("deepseek-v4-pro")
        assert re.match(r"^X-anon-[0-9a-f]{8}$", label)

    def test_different_models_get_different_labels(self):
        a = judge_quality.anonymize_model_id("claude-opus-4-6")
        b = judge_quality.anonymize_model_id("deepseek-v4-pro")
        assert a != b

    def test_judge_prompt_template_contains_anon_not_model_id(self):
        assert "{anon_label}" in judge_quality.JUDGE_PROMPT_TEMPLATE
        assert "{model_id}" not in judge_quality.JUDGE_PROMPT_TEMPLATE

    def test_judge_result_file_stores_real_model_id(self, tmp_path: Path):
        result_data = {
            "model_id": "claude-opus-4-6",
            "task_id": "01_alpha",
            "response": "some response text",
        }
        result_file = tmp_path / "claude-opus-4-6__01_alpha.json"
        result_file.write_text(json.dumps(result_data))

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "01_alpha.txt").write_text("Do task alpha.")

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (
            '{"quality_score": 7, "correctness": true, "completeness_score": 6, "notable_issues": ""}',
            "",
        )
        mock_proc.returncode = 0

        captured_stdin = []

        def fake_popen(cmd, **kwargs):
            return mock_proc

        original_communicate = mock_proc.communicate

        def capture_communicate(input=None, timeout=None):
            captured_stdin.append(input)
            return original_communicate(input=input, timeout=timeout)

        mock_proc.communicate = capture_communicate

        with patch("subprocess.Popen", side_effect=fake_popen):
            score = judge_quality.judge_result_file(result_file, prompts_dir, "opus", 120)

        assert len(captured_stdin) == 1
        prompt_sent = captured_stdin[0]
        assert "claude-opus-4-6" not in prompt_sent
        assert "X-anon-" in prompt_sent

        saved = json.loads(result_file.read_text())
        assert saved["model_id"] == "claude-opus-4-6"
        assert saved["judge_scores"]["quality_score"] == 7


# ---------------------------------------------------------------------------
# QUICK-WIN: kimi-k2-0905 duplicate dropped from models.yaml
# ---------------------------------------------------------------------------

class TestModelsYamlDeduplicated:
    def test_no_kimi_k2_0905_entry(self):
        models = run_benchmark.load_models()
        ids = [m["id"] for m in models]
        assert "kimi-k2-0905" not in ids

    def test_kimi_k2_6_still_present(self):
        models = run_benchmark.load_models()
        ids = [m["id"] for m in models]
        assert "kimi-k2-6" in ids

    def test_total_model_count_after_dedup(self):
        models = run_benchmark.load_models()
        assert len(models) == 7


# ---------------------------------------------------------------------------
# QUICK-WIN: count-leak removed from prompts/02_code_review.txt
# ---------------------------------------------------------------------------

class TestCountLeakRemoved:
    def test_no_count_leak_in_code_review_prompt(self):
        prompt_path = BENCHMARK_DIR / "prompts" / "02_code_review.txt"
        content = prompt_path.read_text()
        assert "5 deliberately planted" not in content
        assert "Do not miss any of the" not in content

    def test_code_review_prompt_still_requests_issues(self):
        prompt_path = BENCHMARK_DIR / "prompts" / "02_code_review.txt"
        content = prompt_path.read_text()
        assert "blocking issues" in content
        assert "line number" in content
