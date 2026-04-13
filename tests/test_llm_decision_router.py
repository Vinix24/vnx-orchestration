"""Tests for llm_decision_router.py — F48-PR1."""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from llm_decision_router import (
    Backend,
    DecisionResult,
    DecisionRouter,
    _build_prompt,
    _parse_llm_response,
    _rule_based_decision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def router_dryrun(tmp_path):
    return DecisionRouter(data_dir=tmp_path, backend="dry-run")


@pytest.fixture()
def failed_receipt_context():
    return {"receipt": {"status": "failed", "dispatch_id": "test-123"}}


@pytest.fixture()
def silent_terminal_context():
    return {"terminal_silence_seconds": 1200, "terminal": "T1"}


# ---------------------------------------------------------------------------
# test_dry_run_returns_rule_based
# ---------------------------------------------------------------------------

def test_dry_run_returns_rule_based(router_dryrun, failed_receipt_context):
    """dry-run backend returns decisions without LLM."""
    result = router_dryrun.decide(failed_receipt_context, "re_dispatch")
    assert isinstance(result, DecisionResult)
    assert result.backend_used == "dry-run"
    assert result.action == "re_dispatch"
    assert 0.0 <= result.confidence <= 1.0


def test_dry_run_failed_receipt_returns_redispatch(tmp_path):
    router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
    ctx = {"receipt": {"status": "failed"}}
    result = router.decide(ctx, "re_dispatch")
    assert result.action == "re_dispatch"
    assert result.confidence == 0.8


def test_dry_run_silent_terminal_escalates(tmp_path):
    router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
    ctx = {"terminal_silence_seconds": 1800}
    result = router.decide(ctx, "escalate")
    assert result.action == "escalate"
    assert result.confidence == 0.6


def test_dry_run_normal_state_skips(tmp_path):
    router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
    ctx = {"receipt": {"status": "done"}, "terminal_silence_seconds": 30}
    result = router.decide(ctx, "skip")
    assert result.action == "skip"
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# test_ollama_backend_timeout_fallback
# ---------------------------------------------------------------------------

def test_ollama_backend_timeout_fallback(tmp_path):
    """Mock Ollama timeout → falls back to dry-run."""
    router = DecisionRouter(data_dir=tmp_path, backend="ollama", timeout=5)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = TimeoutError("connection timed out")
        ctx = {"receipt": {"status": "failed"}}
        result = router.decide(ctx, "re_dispatch")

    assert "dry-run" in result.backend_used
    assert result.action == "re_dispatch"   # rule-based fallback applies


def test_ollama_url_error_falls_back(tmp_path):
    """URLError (Ollama not running) falls back to rule-based."""
    router = DecisionRouter(data_dir=tmp_path, backend="ollama")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        ctx = {}
        result = router.decide(ctx, "skip")

    assert "dry-run" in result.backend_used
    assert result.action == "skip"


# ---------------------------------------------------------------------------
# test_decision_logged_to_ndjson
# ---------------------------------------------------------------------------

def test_decision_logged_to_ndjson(tmp_path):
    """Every decision appended to events/decisions.ndjson."""
    router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
    ctx = {"receipt": {"status": "done"}}
    router.decide(ctx, "skip")

    log_path = tmp_path / "events" / "decisions.ndjson"
    assert log_path.exists(), "decisions.ndjson not created"

    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["action"] == "skip"
    assert record["backend_used"] == "dry-run"
    assert "timestamp" in record
    assert "context_hash" in record
    assert "latency_ms" in record


def test_multiple_decisions_all_logged(tmp_path):
    """Multiple calls → multiple log entries."""
    router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
    router.decide({}, "skip")
    router.decide({"receipt": {"status": "failed"}}, "re_dispatch")
    router.decide({"terminal_silence_seconds": 2000}, "escalate")

    log_path = tmp_path / "events" / "decisions.ndjson"
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3


# ---------------------------------------------------------------------------
# test_backend_selection_via_env
# ---------------------------------------------------------------------------

def test_backend_selection_via_env_dryrun(tmp_path, monkeypatch):
    """VNX_DECISION_BACKEND=dry-run selects dry-run backend."""
    monkeypatch.setenv("VNX_DECISION_BACKEND", "dry-run")
    router = DecisionRouter(data_dir=tmp_path)
    assert router.backend == Backend.DRY_RUN


def test_backend_selection_via_env_ollama(tmp_path, monkeypatch):
    """VNX_DECISION_BACKEND=ollama selects ollama backend."""
    monkeypatch.setenv("VNX_DECISION_BACKEND", "ollama")
    router = DecisionRouter(data_dir=tmp_path)
    assert router.backend == Backend.OLLAMA


def test_backend_selection_via_env_claude_cli(tmp_path, monkeypatch):
    """VNX_DECISION_BACKEND=claude-cli selects claude-cli backend."""
    monkeypatch.setenv("VNX_DECISION_BACKEND", "claude-cli")
    router = DecisionRouter(data_dir=tmp_path)
    assert router.backend == Backend.CLAUDE_CLI


def test_backend_default_is_dryrun(tmp_path, monkeypatch):
    """Unset VNX_DECISION_BACKEND → dry-run."""
    monkeypatch.delenv("VNX_DECISION_BACKEND", raising=False)
    router = DecisionRouter(data_dir=tmp_path)
    assert router.backend == Backend.DRY_RUN


# ---------------------------------------------------------------------------
# test_structured_context_to_prompt
# ---------------------------------------------------------------------------

def test_structured_context_to_prompt():
    """Context dict serialized correctly for LLM prompt."""
    ctx = {
        "terminal": "T1",
        "receipt": {"status": "failed", "dispatch_id": "abc-123"},
        "terminal_silence_seconds": 45,
    }
    prompt = _build_prompt(ctx, "re_dispatch")

    # All keys present in output
    assert "T1" in prompt
    assert "failed" in prompt
    assert "abc-123" in prompt
    assert "re_dispatch" in prompt


def test_build_prompt_handles_nested_objects():
    """Nested context serializes without errors."""
    ctx = {
        "nested": {"deeply": {"value": 42}},
        "list_field": [1, 2, 3],
    }
    prompt = _build_prompt(ctx, "analyze_failure")
    assert "deeply" in prompt
    assert "analyze_failure" in prompt


# ---------------------------------------------------------------------------
# test_parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_llm_response_bare_json():
    raw = '{"action": "re_dispatch", "reasoning": "failed", "confidence": 0.8}'
    result = _parse_llm_response(raw)
    assert result is not None
    assert result["action"] == "re_dispatch"


def test_parse_llm_response_markdown_fences():
    raw = '```json\n{"action": "skip", "reasoning": "normal", "confidence": 1.0}\n```'
    result = _parse_llm_response(raw)
    assert result is not None
    assert result["action"] == "skip"


def test_parse_llm_response_embedded_json():
    raw = 'Here is the decision:\n{"action": "escalate", "reasoning": "stuck", "confidence": 0.6}\nEnd.'
    result = _parse_llm_response(raw)
    assert result is not None
    assert result["action"] == "escalate"


def test_parse_llm_response_returns_none_on_garbage():
    assert _parse_llm_response("not json at all") is None


# ---------------------------------------------------------------------------
# test_ollama_successful_response
# ---------------------------------------------------------------------------

def test_ollama_successful_response(tmp_path):
    """Ollama returning valid JSON → DecisionResult uses ollama backend."""
    router = DecisionRouter(data_dir=tmp_path, backend="ollama",
                            ollama_model="gemma3:27b")

    ollama_body = json.dumps({
        "response": '{"action": "skip", "reasoning": "all good", "confidence": 0.95}'
    }).encode()

    # urlopen is used as a context manager: `with urlopen(req, timeout=t) as resp:`
    mock_resp = MagicMock()
    mock_resp.read.return_value = ollama_body
    mock_urlopen = MagicMock()
    mock_urlopen.return_value.__enter__.return_value = mock_resp
    mock_urlopen.return_value.__exit__.return_value = False

    with patch("urllib.request.urlopen", mock_urlopen):
        result = router.decide({}, "skip")

    assert result.action == "skip"
    assert "ollama" in result.backend_used
    assert result.confidence == pytest.approx(0.95)
