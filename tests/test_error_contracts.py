#!/usr/bin/env python3
"""Regression tests for A2 error-contract hardening."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
PROJECT_ROOT = TESTS_DIR.parents[3]


def _env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    env["PROJECT_ROOT"] = str(PROJECT_ROOT)
    return env


def _run(tmp_path: Path, script: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *args],
        cwd=PROJECT_ROOT,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
    )


def _extract_json_line(output: str) -> dict:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise AssertionError(f"No JSON line found in output:\n{output}")


def test_intelligence_queries_unknown_command_returns_validation_json(tmp_path: Path):
    result = _run(tmp_path, "intelligence_queries.py", ["no_such_command"])

    assert result.returncode == 10
    payload = _extract_json_line(result.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "unknown_command"
    assert payload["error_msg"] == "Unknown command: no_such_command"


def test_intelligence_queries_dependency_failure_maps_to_exit_30(tmp_path: Path):
    result = _run(tmp_path, "intelligence_queries.py", ["pattern_usage", "missing-pattern"])

    assert result.returncode == 30
    payload = _extract_json_line(result.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] in {"quality_db_unavailable", "query_execution_failed"}


def test_check_intelligence_health_json_output_and_semantic_exit(tmp_path: Path):
    result = _run(tmp_path, "check_intelligence_health.py", [])

    payload = _extract_json_line(result.stdout)
    assert "health_status" in payload
    expected_code = 0 if payload.get("health_status") == "healthy" else 11
    assert result.returncode == expected_code


def test_shell_critical_paths_no_true_suppression_and_structured_failures_present():
    dispatcher = (SCRIPTS_DIR / "dispatcher_minimal.sh").read_text(encoding="utf-8")
    receipt_processor = (SCRIPTS_DIR / "receipt_processor.sh").read_text(encoding="utf-8")
    t0_brief = (SCRIPTS_DIR / "generate_t0_brief.sh").read_text(encoding="utf-8")

    assert "|| true" not in dispatcher
    assert "|| true" not in receipt_processor
    assert "|| true" not in t0_brief

    assert "log_structured_failure" in dispatcher
    assert "log_structured_failure" in receipt_processor
    assert "log_critical_failure" in t0_brief
    assert "log_non_critical_warning" in t0_brief

    assert 'validation_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" validate' in dispatcher
    assert 'intel_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" gather' in dispatcher
    assert "validation_rc=$?" in dispatcher
    assert "intel_rc=$?" in dispatcher
    assert 'local dispatch_id="$7"' in dispatcher
    assert 'generate_receipt_footer "$dispatch_file" "$track" "$phase" "$gate" "$task_id" "$cmd_id" "$dispatch_id"' in dispatcher
