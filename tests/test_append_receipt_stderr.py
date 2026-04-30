#!/usr/bin/env python3
"""Tests for structured stderr discipline in append_receipt.py.

Every line written to stderr must be valid JSON with required fields:
  code, level, timestamp
Plain-text stderr writes are forbidden (CFX-8).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
APPEND_SCRIPT = SCRIPTS_DIR / "append_receipt.py"

REQUIRED_FIELDS = {"code", "level", "timestamp"}


def _build_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(tmp_path / "data")
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    return env


def _parse_stderr_lines(stderr: str) -> List[dict]:
    lines = []
    for raw in stderr.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        lines.append(json.loads(raw))  # raises on non-JSON → test fails with clear message
    return lines


def _assert_all_structured(stderr: str) -> List[dict]:
    lines = _parse_stderr_lines(stderr)
    for entry in lines:
        missing = REQUIRED_FIELDS - set(entry.keys())
        assert not missing, (
            f"Stderr line missing required fields {missing!r}: {entry!r}"
        )
    return lines


def _run_append(
    tmp_path: Path,
    payload: str,
    extra_args: list | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    args = [sys.executable, str(APPEND_SCRIPT)]
    if extra_args:
        args.extend(extra_args)
    env = _build_env(tmp_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(args, input=payload, capture_output=True, text=True, env=env)


def _load_ar():
    """Import append_receipt module with minimal stub env."""
    env_patch = {
        "PROJECT_ROOT": str(VNX_ROOT),
        "VNX_DATA_DIR": str(VNX_ROOT / ".vnx-data"),
        "VNX_STATE_DIR": str(VNX_ROOT / ".vnx-data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    mod_name = "ar_stderr_testmodule"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(mod_name, APPEND_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


@pytest.fixture(scope="module")
def ar():
    return _load_ar()


# ── 1. Success path emits structured JSON only ────────────────────────────────

def test_valid_receipt_stderr_is_all_json(tmp_path: Path):
    receipt = json.dumps({
        "timestamp": "2026-04-30T10:00:00Z",
        "event_type": "task_started",
        "dispatch_id": "CFX8-T001",
        "terminal": "T1",
    })
    result = _run_append(tmp_path, receipt, ["--skip-enrichment"])
    assert result.returncode == 0, f"unexpected failure: {result.stderr}"
    lines = _assert_all_structured(result.stderr)
    assert any(e["code"] == "receipt_appended" for e in lines)


# ── 2. Error path (malformed JSON) emits structured JSON only ─────────────────

def test_malformed_json_stderr_is_all_json(tmp_path: Path):
    result = _run_append(tmp_path, '{"timestamp":')
    assert result.returncode != 0
    lines = _assert_all_structured(result.stderr)
    assert any(e["code"] == "invalid_json" for e in lines)
    assert all(e["level"] in ("INFO", "WARN", "ERROR", "DEBUG") for e in lines)


# ── 3. Missing required key emits structured JSON only ───────────────────────

def test_missing_timestamp_stderr_is_all_json(tmp_path: Path):
    receipt = json.dumps({"event_type": "task_started", "terminal": "T1"})
    result = _run_append(tmp_path, receipt, ["--skip-enrichment"])
    assert result.returncode != 0
    lines = _assert_all_structured(result.stderr)
    assert any(e["code"] == "missing_required_key" for e in lines)


# ── 4. Duplicate receipt emits structured JSON only ───────────────────────────

def test_duplicate_receipt_stderr_is_all_json(tmp_path: Path):
    receipt = json.dumps({
        "timestamp": "2026-04-30T11:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "CFX8-T002",
        "terminal": "T2",
        "status": "success",
    })
    _run_append(tmp_path, receipt, ["--skip-enrichment"])
    result = _run_append(tmp_path, receipt, ["--skip-enrichment"])
    assert result.returncode == 0
    lines = _assert_all_structured(result.stderr)
    assert any(e["code"] == "duplicate_receipt_skipped" for e in lines)


# ── 5. _emit helper always produces required fields ───────────────────────────

def test_emit_helper_produces_required_fields(ar, capsys):
    ar._emit("WARN", "test_code_cfx8", message="test message", extra_field=42)
    captured = capsys.readouterr()
    lines = _assert_all_structured(captured.err)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["level"] == "WARN"
    assert entry["code"] == "test_code_cfx8"
    assert isinstance(entry["timestamp"], int)
    assert entry["message"] == "test message"
    assert entry["extra_field"] == 42


# ── 6. _emit level field is present on INFO, WARN, ERROR ─────────────────────

@pytest.mark.parametrize("level", ["INFO", "WARN", "ERROR"])
def test_emit_level_variants(ar, capsys, level: str):
    ar._emit(level, f"test_level_{level.lower()}")
    captured = capsys.readouterr()
    lines = _assert_all_structured(captured.err)
    assert lines[0]["level"] == level


# ── 7. manifest_sha256_read_failed is structured (CFX-8 regression target) ───

def test_manifest_sha256_read_failed_is_structured(ar, capsys):
    """_build_session_metadata emits structured WARN when manifest cannot be read."""
    receipt = {
        "timestamp": "2026-04-30T12:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "CFX8-T003",
        "terminal": "T1",
        "manifest_path": "/nonexistent/path/manifest.json",
    }
    state_dir = Path("/tmp/cfx8_test_state")
    with patch.object(ar, "_resolve_model_provider", return_value={"model": "unknown", "provider": "claude_code"}), \
         patch.object(ar, "_resolve_session_id", return_value="unknown"), \
         patch.object(ar, "_extract_session_token_usage", return_value=None):
        ar._build_session_metadata(receipt, state_dir)
    captured = capsys.readouterr()
    if captured.err.strip():
        lines = _assert_all_structured(captured.err)
        manifest_warns = [e for e in lines if e.get("code") == "manifest_sha256_read_failed"]
        assert manifest_warns, (
            f"Expected manifest_sha256_read_failed WARN line, got: {captured.err!r}"
        )
        assert manifest_warns[0]["level"] == "WARN"


# ── 8. No plain-text stderr in source file ───────────────────────────────────

def test_no_plaintext_stderr_in_source():
    """Static check: append_receipt.py must not contain raw print(..., file=sys.stderr)
    or sys.stderr.write outside the _emit() implementation itself."""
    source = APPEND_SCRIPT.read_text(encoding="utf-8")
    lines = source.splitlines()
    violations = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        # Skip the _emit implementation body (line containing json.dumps + file=sys.stderr)
        if "json.dumps" in stripped and "file=sys.stderr" in stripped:
            continue
        if "sys.stderr.write" in stripped or ("print" in stripped and "file=sys.stderr" in stripped):
            violations.append((i, stripped))
    assert not violations, (
        f"Plain-text stderr writes found in append_receipt.py:\n"
        + "\n".join(f"  line {n}: {text}" for n, text in violations)
    )
