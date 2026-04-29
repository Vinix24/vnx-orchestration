#!/usr/bin/env python3
"""Integration tests for provider token tracking in receipts.

Covers Cases A–F from the dispatch specification:
  A. Codex CLI output with text token line → adapter returns dict
  B. Codex output without token info → adapter returns None
  C. Gemini JSON output with usageMetadata → adapter returns dict
  D. Gemini malformed/missing metadata → returns None
  E. Receipt enrichment: codex/gemini token_usage merged into session field
  F. Claude path unchanged — existing session JSONL extraction still works
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
APPEND_SCRIPT = SCRIPTS_DIR / "append_receipt.py"

sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.codex_adapter import CodexAdapter
from adapters.gemini_adapter import GeminiAdapter


# ---------------------------------------------------------------------------
# Case A — Codex: text token line → adapter returns {input:1200, output:350}
# ---------------------------------------------------------------------------

class TestCaseA_CodexTextTokenLine:
    def test_text_token_line_parsed(self):
        raw = "Reviewing code...\nTokens: 1200 input / 350 output\nDone."
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result == {
            "input_tokens": 1200,
            "output_tokens": 350,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def test_json_token_usage_event_parsed(self):
        events = [
            {"type": "start"},
            {"type": "token_usage", "input_tokens": 1200, "output_tokens": 350},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 1200
        assert result["output_tokens"] == 350

    def test_openai_usage_block_parsed(self):
        raw = json.dumps({"usage": {"prompt_tokens": 1200, "completion_tokens": 350}})
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 1200
        assert result["output_tokens"] == 350


# ---------------------------------------------------------------------------
# Case B — Codex: no token info → adapter returns None
# ---------------------------------------------------------------------------

class TestCaseB_CodexNoTokenInfo:
    def test_empty_output_returns_none(self):
        assert CodexAdapter._parse_token_usage_from_output("") is None

    def test_plain_text_returns_none(self):
        raw = "The code looks fine. No critical issues found."
        assert CodexAdapter._parse_token_usage_from_output(raw) is None

    def test_json_without_usage_returns_none(self):
        raw = json.dumps({"type": "result", "content": "findings"})
        assert CodexAdapter._parse_token_usage_from_output(raw) is None


# ---------------------------------------------------------------------------
# Case C — Gemini: JSON with usageMetadata → adapter returns dict
# ---------------------------------------------------------------------------

class TestCaseC_GeminiUsageMetadata:
    def test_top_level_usage_metadata(self):
        raw = json.dumps({
            "response": "Review complete.",
            "usageMetadata": {
                "promptTokenCount": 800,
                "candidatesTokenCount": 250,
            },
        })
        result = GeminiAdapter._parse_token_usage_from_response(raw)
        assert result == {
            "input_tokens": 800,
            "output_tokens": 250,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def test_ndjson_stream_usage_metadata(self):
        lines = [
            json.dumps({"text": "partial output"}),
            json.dumps({"usageMetadata": {"promptTokenCount": 600, "candidatesTokenCount": 180}}),
        ]
        raw = "\n".join(lines)
        result = GeminiAdapter._parse_token_usage_from_response(raw)
        assert result is not None
        assert result["input_tokens"] == 600
        assert result["output_tokens"] == 180


# ---------------------------------------------------------------------------
# Case D — Gemini: missing/malformed metadata → returns None
# ---------------------------------------------------------------------------

class TestCaseD_GeminiMissingMetadata:
    def test_empty_response_returns_none(self):
        assert GeminiAdapter._parse_token_usage_from_response("") is None

    def test_plain_text_returns_none(self):
        assert GeminiAdapter._parse_token_usage_from_response("Here are findings.") is None

    def test_json_without_usage_metadata_returns_none(self):
        raw = json.dumps({"response": "no tokens here"})
        assert GeminiAdapter._parse_token_usage_from_response(raw) is None

    def test_usage_metadata_missing_fields_returns_none(self):
        raw = json.dumps({"usageMetadata": {"totalTokenCount": 100}})
        assert GeminiAdapter._parse_token_usage_from_response(raw) is None

    def test_all_zero_counts_returns_none(self):
        raw = json.dumps({"usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0}})
        assert GeminiAdapter._parse_token_usage_from_response(raw) is None


# ---------------------------------------------------------------------------
# Case E — Receipt enrichment: adapter get_token_usage merged into session
# ---------------------------------------------------------------------------

def _build_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    return env


def _run_append(tmp_path: Path, payload: str) -> subprocess.CompletedProcess:
    env = _build_env(tmp_path)
    return subprocess.run(
        [sys.executable, str(APPEND_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def _build_receipt(terminal: str = "T1", event_type: str = "task_complete") -> dict:
    return {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": event_type,
        "event": event_type,
        "dispatch_id": "20260428-t5-pr6-test",
        "task_id": "TASK-001",
        "terminal": terminal,
        "status": "success",
        "source": "pytest",
    }


class TestCaseE_ReceiptEnrichmentCodex:
    def test_codex_terminal_token_usage_merged(self, tmp_path: Path):
        env = _build_env(tmp_path)
        state_dir = Path(env["VNX_STATE_DIR"])

        # Pre-populate token cache as if CodexAdapter.execute() ran
        usage = {"input_tokens": 1500, "output_tokens": 400, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        cache_dir = state_dir / "token_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "CODEX-1_usage.json").write_text(json.dumps(usage))

        receipt = _build_receipt(terminal="CODEX-1")
        result = subprocess.run(
            [sys.executable, str(APPEND_SCRIPT)],
            input=json.dumps(receipt),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr

        # Verify token_usage written into the receipt ndjson
        receipts_file = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
        assert receipts_file.exists()
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        saved = json.loads(lines[-1])
        session = saved.get("session", {})
        assert "token_usage" in session, f"token_usage missing from session: {session}"
        assert session["token_usage"]["input_tokens"] == 1500
        assert session["token_usage"]["output_tokens"] == 400

    def test_gemini_terminal_token_usage_merged(self, tmp_path: Path):
        env = _build_env(tmp_path)
        state_dir = Path(env["VNX_STATE_DIR"])

        usage = {"input_tokens": 900, "output_tokens": 300, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        cache_dir = state_dir / "token_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "GEMINI-1_usage.json").write_text(json.dumps(usage))

        receipt = _build_receipt(terminal="GEMINI-1")
        result = subprocess.run(
            [sys.executable, str(APPEND_SCRIPT)],
            input=json.dumps(receipt),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr

        receipts_file = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        saved = json.loads(lines[-1])
        session = saved.get("session", {})
        assert "token_usage" in session, f"token_usage missing from session: {session}"
        assert session["token_usage"]["input_tokens"] == 900

    def test_no_cache_means_no_token_usage_key(self, tmp_path: Path):
        env = _build_env(tmp_path)
        # No token cache file written — codex terminal with no token data
        receipt = _build_receipt(terminal="CODEX-1")
        result = subprocess.run(
            [sys.executable, str(APPEND_SCRIPT)],
            input=json.dumps(receipt),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        receipts_file = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        saved = json.loads(lines[-1])
        session = saved.get("session", {})
        # token_usage may be absent or None — either is acceptable; must not crash
        assert session.get("token_usage") is None


# ---------------------------------------------------------------------------
# Case F — Claude path unchanged
# ---------------------------------------------------------------------------

class TestCaseF_ClaudePathUnchanged:
    def test_claude_terminal_no_token_usage_without_session_file(self, tmp_path: Path):
        env = _build_env(tmp_path)
        receipt = _build_receipt(terminal="T1")
        result = subprocess.run(
            [sys.executable, str(APPEND_SCRIPT)],
            input=json.dumps(receipt),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        receipts_file = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        saved = json.loads(lines[-1])
        session = saved.get("session", {})
        # Without a real Claude session JSONL, token_usage is absent — no crash
        assert "token_usage" not in session or session["token_usage"] is None

    def test_claude_terminal_provider_is_claude_code(self, tmp_path: Path):
        env = _build_env(tmp_path)
        receipt = _build_receipt(terminal="T1")
        result = subprocess.run(
            [sys.executable, str(APPEND_SCRIPT)],
            input=json.dumps(receipt),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        receipts_file = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        saved = json.loads(lines[-1])
        session = saved.get("session", {})
        assert session.get("provider") == "claude_code"
