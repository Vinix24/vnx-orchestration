"""R2 (OI-2043, OI-2061): the analyzer beacon says degraded when nothing was analysed, and a
failed CLI call keeps its cause (full stderr, stdout JSON ``is_error`` / ``result``).

No test starts a network call or a real claude process: the harness is a stand-in.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

_import_env = {
    "VNX_HOME": tempfile.mkdtemp(),
    "VNX_STATE_DIR": tempfile.mkdtemp(),
    "PROJECT_ROOT": tempfile.mkdtemp(),
}
with patch.dict(os.environ, _import_env):
    import conversation_analyzer.deep_analyzer as da_module
    from conversation_analyzer import DeepAnalyzer, RunStats, deep_none_attempted
    from conversation_analyzer.deep_analyzer import (
        CLI_CAUSE_LIMIT, CLI_STDERR_LOG_LIMIT, LLMOutcome, describe_cli_failure,
    )

from provider_spawns.deepseek_harness_spawn import build_harness_argv, build_harness_cli_args

NOTICE = "[claude-code:unrecognized_model] {\"model\":\"deepseek-flash\",\"query_source\":\"sdk\"}"


def _load_cli():
    spec = importlib.util.spec_from_file_location("analyzer_cli_r2", SCRIPTS / "conversation_analyzer.py")
    with patch.dict(os.environ, _import_env):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _beacon(tmp_path, stats, status="ok", error=None):
    cli = _load_cli()
    cli.PATHS = {"VNX_DATA_DIR": str(tmp_path)}
    cli._write_heartbeat(argparse.Namespace(max_sessions=50, dry_run=False), stats, status, error)
    return json.loads((tmp_path / "health" / "conversation_analyzer.json").read_text())


# --- beacon (OI-2043) --------------------------------------------------------------------

def test_zero_attempts_with_deferred_sessions_is_degraded_not_ok(tmp_path):
    stats = RunStats(deep_attempts=0, deep_restricted_deferred=51)
    beacon = _beacon(tmp_path, stats)
    assert beacon["status"] == "degraded"
    assert beacon["details"]["reason"] == "sessions_due_none_analysed"
    assert beacon["details"]["deep_restricted_deferred"] == 51


def test_zero_attempts_with_config_skips_is_degraded(tmp_path):
    beacon = _beacon(tmp_path, RunStats(deep_attempts=0, deep_config_skips=3))
    assert beacon["status"] == "degraded"
    assert beacon["details"]["deep_config_skips"] == 3


def test_a_night_with_nothing_due_stays_ok(tmp_path):
    assert _beacon(tmp_path, RunStats())["status"] == "ok"


def test_a_night_with_an_attempt_stays_ok_despite_deferrals(tmp_path):
    stats = RunStats(deep_attempts=2, deep_restricted_deferred=5)
    assert _beacon(tmp_path, stats)["status"] == "ok"
    assert deep_none_attempted(stats) is False


def test_all_failed_stays_fail_and_carries_the_causes(tmp_path):
    stats = RunStats(deep_attempts=2, deep_failures=2,
                     deep_failure_causes={"API Error: 402 Insufficient Balance": 2})
    beacon = _beacon(tmp_path, stats, status="fail", error="deep analysis failed on all 2 attempts")
    assert beacon["status"] == "fail"
    assert beacon["details"]["failure_causes"] == {"API Error: 402 Insufficient Balance": 2}


def test_switched_off_keeps_its_own_reason(tmp_path):
    stats = RunStats(deep_disabled_skips=2, deep_open_sessions=2)
    assert _beacon(tmp_path, stats)["details"]["reason"] == "provider_switched_off"


def test_deep_none_attempted_is_false_without_stats():
    assert deep_none_attempted(None) is False


# --- recorded cause (OI-2061) ------------------------------------------------------------

def test_describe_reads_is_error_and_result_from_stdout_json():
    stdout = json.dumps({"is_error": True, "result": "API Error: 402 Insufficient Balance\nmore"})
    detail, cause = describe_cli_failure(1, stdout, NOTICE + "\nauto mode on")
    assert cause == "API Error: 402 Insufficient Balance"
    assert "is_error=True" in detail and "402 Insufficient Balance" in detail
    assert "unrecognized_model" in detail


def test_describe_skips_the_startup_notice_when_picking_the_stderr_cause():
    _, cause = describe_cli_failure(1, "", NOTICE + "\nauto mode enabled\nreal error")
    assert cause == "auto mode enabled"
    _, cause = describe_cli_failure(1, "", NOTICE)
    assert cause == "exit code 1 without a message"


def test_describe_bounds_stderr_and_cause():
    detail, cause = describe_cli_failure(1, "not json", "x" * 50000)
    assert len(cause) == CLI_CAUSE_LIMIT
    assert len(detail) < CLI_STDERR_LOG_LIMIT + 200


def _fail_run(stdout, stderr="", rc=1):
    return MagicMock(returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-0123456789abcdef")
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight_cache", None, raising=False)
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight",
                        classmethod(lambda cls, key: LLMOutcome("ok")))
    monkeypatch.setattr(da_module, "build_harness_child_env", lambda key: {})
    monkeypatch.setattr(da_module, "resolve_claude_cli", lambda: "claude")


def test_harness_failure_keeps_stdout_result_in_log_and_outcome(lane, monkeypatch):
    logs = []
    monkeypatch.setattr(da_module, "log", lambda level, msg: logs.append((level, msg)))
    stdout = json.dumps({"is_error": True, "result": "API Error: 402 Insufficient Balance"})
    monkeypatch.setattr(da_module.subprocess, "run",
                        MagicMock(return_value=_fail_run(stdout, NOTICE + "\n" + "pad " * 100)))
    outcome = DeepAnalyzer._try_deepseek_harness("prompt")
    assert outcome.status == "cli_failed" and outcome.returncode == 1
    assert outcome.cause == "API Error: 402 Insufficient Balance"
    assert len(outcome.stderr) > 200
    text = " ".join(m for _, m in logs)
    assert "is_error=True" in text and "402 Insufficient Balance" in text


def test_claude_max_failure_keeps_stdout_result(monkeypatch):
    monkeypatch.setattr(da_module, "resolve_claude_cli", lambda: "claude")
    stdout = json.dumps({"is_error": True, "result": "Credit balance is too low"})
    monkeypatch.setattr(da_module.subprocess, "run", MagicMock(return_value=_fail_run(stdout)))
    outcome = DeepAnalyzer._try_claude_max("prompt")
    assert outcome.status == "cli_failed" and outcome.cause == "Credit balance is too low"


def test_failure_cause_reaches_the_run_counters(lane, monkeypatch):
    stdout = json.dumps({"is_error": True, "result": "API Error: 402 Insufficient Balance"})
    monkeypatch.setattr(da_module.subprocess, "run", MagicMock(return_value=_fail_run(stdout)))
    monkeypatch.setattr(da_module, "LLM_STRATEGY", "deepseek-harness")
    analyzer = DeepAnalyzer()
    outcome = analyzer._launch_lane(analyzer._try_deepseek_harness, "p", "vnx-dev")
    analyzer._record_failure(outcome.status, outcome.cause)
    assert analyzer.deep_failure_causes == {"API Error: 402 Insufficient Balance": 1}


# --- one launch builder (OI-2061, cause 3) -------------------------------------------------

def test_analyzer_argv_comes_from_the_shared_builder(lane, monkeypatch):
    run = MagicMock(return_value=MagicMock(returncode=0, stdout=json.dumps({"result": "{}"}), stderr=""))
    monkeypatch.setattr(da_module.subprocess, "run", run)
    DeepAnalyzer._try_deepseek_harness("prompt")
    argv = run.call_args.args[0]
    assert argv == build_harness_argv("claude", da_module.DEEPSEEK_HARNESS_MODEL,
                                      output_format="json", print_flags=["--max-turns", "1"])
    assert argv[-len(build_harness_cli_args()):] == build_harness_cli_args()
    assert argv[argv.index("--model") + 1] == "deepseek-flash"


# --- causes only for counted failures (R2 round 2, deepseek info) --------------------------

def test_restricted_deferral_with_claude_down_records_no_failure_cause(monkeypatch):
    monkeypatch.setattr(da_module, "resolve_claude_cli", lambda: "claude")
    stdout = json.dumps({"is_error": True, "result": "API Error: 402 Insufficient Balance"})
    monkeypatch.setattr(da_module.subprocess, "run", MagicMock(return_value=_fail_run(stdout)))
    analyzer = DeepAnalyzer()
    assert analyzer._analyze_restricted("prompt") is None
    assert analyzer.deep_failures == 0
    assert analyzer.deep_failure_causes == {}
    assert analyzer.deep_restricted_deferred == 1
