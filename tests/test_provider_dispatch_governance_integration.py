"""test_provider_dispatch_governance_integration.py — Integration tests for
provider_dispatch.py governance emit hooks (Wave 7 PR-7.6).

Each test mocks the spawn function and verifies that:
  - t0_receipts.ndjson gets a new line with the correct `provider` field
  - unified_reports/<dispatch_id>_report.md is created
  - Failure/timeout dispatches produce a receipt with status != "success"
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_dispatch


# ---------------------------------------------------------------------------
# Shared result stub
# ---------------------------------------------------------------------------

@dataclass
class _SpawnResult:
    returncode: int = 0
    completion_text: str = "OK"
    events_written: int = 5
    session_id: Optional[str] = None
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


def _make_args(provider, dispatch_id="test-integ-001", data_root=None):
    args = MagicMock()
    args.provider = provider
    args.dispatch_id = dispatch_id
    args.terminal_id = "T1"
    args.instruction = "Do the integration test thing"
    args.model = "sonnet"
    args.pr_id = None
    args.dispatch_paths = ""
    args.no_auto_commit = True
    args.max_retries = 1
    args.gate = ""
    args.role = None
    return args


# ---------------------------------------------------------------------------
# Parametrized fixture to redirect state / data dirs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_env_dirs(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    return state_dir, data_dir


# ---------------------------------------------------------------------------
# Helper: read last receipt from ndjson
# ---------------------------------------------------------------------------

def _last_receipt(tmp_path):
    state_dir = tmp_path / "state"
    receipt_path = state_dir / "t0_receipts.ndjson"
    if not receipt_path.exists():
        return None
    lines = [l for l in receipt_path.read_text().splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def _report_exists(tmp_path, dispatch_id):
    data_dir = tmp_path / "data"
    return (data_dir / "unified_reports" / f"{dispatch_id}_report.md").exists()


# ---------------------------------------------------------------------------
# Claude dispatch test (subprocess_dispatch delegation)
# ---------------------------------------------------------------------------

def test_claude_dispatch_produces_receipt(tmp_path):
    args = _make_args("claude", dispatch_id="claude-integ-001")
    with patch("subprocess_dispatch.deliver_with_recovery", return_value=True):
        rc = provider_dispatch._dispatch_claude(args)
    assert rc == 0
    receipt = _last_receipt(tmp_path)
    assert receipt is not None
    assert receipt["provider"] == "claude"
    assert receipt["status"] == "success"


def test_claude_dispatch_failure_produces_receipt(tmp_path):
    args = _make_args("claude", dispatch_id="claude-fail-001")
    with patch("subprocess_dispatch.deliver_with_recovery", return_value=False):
        rc = provider_dispatch._dispatch_claude(args)
    assert rc == 1
    receipt = _last_receipt(tmp_path)
    assert receipt is not None
    assert receipt["provider"] == "claude"
    assert receipt["status"] == "failure"


# ---------------------------------------------------------------------------
# Codex dispatch tests
# ---------------------------------------------------------------------------

def test_codex_dispatch_produces_receipt(tmp_path):
    args = _make_args("codex", dispatch_id="codex-integ-001")
    result = _SpawnResult(completion_text="codex output")
    with patch("provider_spawns.codex_spawn.spawn_codex", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        rc = provider_dispatch._dispatch_codex(args)
    assert rc == 0
    receipt = _last_receipt(tmp_path)
    assert receipt is not None
    assert receipt["provider"] == "codex"


def test_codex_dispatch_failure_emits_receipt_with_status_failure(tmp_path):
    args = _make_args("codex", dispatch_id="codex-fail-001")
    result = _SpawnResult(returncode=1, error="codex blew up")
    with patch("provider_spawns.codex_spawn.spawn_codex", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        rc = provider_dispatch._dispatch_codex(args)
    assert rc == 1
    receipt = _last_receipt(tmp_path)
    assert receipt["provider"] == "codex"
    assert receipt["status"] == "failure"


# ---------------------------------------------------------------------------
# Gemini dispatch tests
# ---------------------------------------------------------------------------

def test_gemini_dispatch_produces_receipt(tmp_path):
    args = _make_args("gemini", dispatch_id="gemini-integ-001")
    result = _SpawnResult(completion_text="gemini output")
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        rc = provider_dispatch._dispatch_gemini(args)
    assert rc == 0
    receipt = _last_receipt(tmp_path)
    assert receipt is not None
    assert receipt["provider"] == "gemini"


def test_gemini_dispatch_timeout_produces_receipt(tmp_path):
    args = _make_args("gemini", dispatch_id="gemini-timeout-001")
    result = _SpawnResult(returncode=1, timed_out=True)
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        rc = provider_dispatch._dispatch_gemini(args)
    assert rc == 1
    receipt = _last_receipt(tmp_path)
    assert receipt["provider"] == "gemini"
    assert receipt["status"] == "timeout"


# ---------------------------------------------------------------------------
# LiteLLM dispatch tests
# ---------------------------------------------------------------------------

def _mock_litellm_env(monkeypatch, sub="deepseek"):
    monkeypatch.setenv("VNX_LITELLM_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")


def test_litellm_deepseek_dispatch_produces_receipt(tmp_path, monkeypatch):
    _mock_litellm_env(monkeypatch)
    args = _make_args("litellm:deepseek", dispatch_id="litellm-ds-001")
    result = _SpawnResult(
        completion_text="deepseek response",
        token_usage={"input_tokens": 100, "output_tokens": 40, "cache_read_tokens": 0},
    )
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=result), \
         patch("event_store.EventStore", MagicMock()), \
         patch("providers.behavior_contracts.get_contract", side_effect=KeyError):
        rc = provider_dispatch._dispatch_litellm(args)
    assert rc == 0
    receipt = _last_receipt(tmp_path)
    assert receipt is not None
    assert receipt["provider"] == "litellm:deepseek"


def test_receipt_contains_correct_provider_field_for_each_provider(tmp_path, monkeypatch):
    _mock_litellm_env(monkeypatch)
    dispatch_id = "litellm-provider-field-001"
    args = _make_args("litellm:deepseek", dispatch_id=dispatch_id)
    result = _SpawnResult()
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=result), \
         patch("event_store.EventStore", MagicMock()), \
         patch("providers.behavior_contracts.get_contract", side_effect=KeyError):
        provider_dispatch._dispatch_litellm(args)
    receipt = _last_receipt(tmp_path)
    assert receipt["provider"] == "litellm:deepseek"
    assert receipt["dispatch_id"] == dispatch_id


# ---------------------------------------------------------------------------
# Unified report tests
# ---------------------------------------------------------------------------

def test_unified_report_created_for_claude(tmp_path):
    args = _make_args("claude", dispatch_id="claude-report-001")
    with patch("subprocess_dispatch.deliver_with_recovery", return_value=True):
        provider_dispatch._dispatch_claude(args)
    assert _report_exists(tmp_path, "claude-report-001")


def test_unified_report_created_for_each_provider(tmp_path, monkeypatch):
    _mock_litellm_env(monkeypatch)
    dispatch_id = "litellm-report-001"
    args = _make_args("litellm:deepseek", dispatch_id=dispatch_id)
    result = _SpawnResult(completion_text="litellm response text")
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=result), \
         patch("event_store.EventStore", MagicMock()), \
         patch("providers.behavior_contracts.get_contract", side_effect=KeyError):
        provider_dispatch._dispatch_litellm(args)
    assert _report_exists(tmp_path, dispatch_id)
    report_path = tmp_path / "data" / "unified_reports" / f"{dispatch_id}_report.md"
    content = report_path.read_text()
    assert "Provider: litellm:deepseek" in content
    assert "litellm response text" in content


def test_dispatch_failure_emits_receipt_with_status_failure_gemini(tmp_path):
    args = _make_args("gemini", dispatch_id="gemini-fail-002")
    result = _SpawnResult(returncode=1, error="gemini failed")
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        rc = provider_dispatch._dispatch_gemini(args)
    assert rc == 1
    receipt = _last_receipt(tmp_path)
    assert receipt["status"] == "failure"
    assert receipt["provider"] == "gemini"


# ---------------------------------------------------------------------------
# Token usage extraction tests
# ---------------------------------------------------------------------------

def test_litellm_dispatch_extracts_token_usage(tmp_path, monkeypatch):
    """LiteLLM usage uses OpenAI field names: prompt_tokens / completion_tokens."""
    _mock_litellm_env(monkeypatch)
    args = _make_args("litellm:deepseek", dispatch_id="litellm-usage-001")
    result = _SpawnResult(
        token_usage={
            "prompt_tokens": 350,
            "completion_tokens": 120,
            "total_tokens": 470,
            "prompt_tokens_details": {"cached_tokens": 50},
        }
    )
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=result), \
         patch("event_store.EventStore", MagicMock()), \
         patch("providers.behavior_contracts.get_contract", side_effect=KeyError):
        provider_dispatch._dispatch_litellm(args)
    receipt = _last_receipt(tmp_path)
    assert receipt["token_usage"]["input"] == 350
    assert receipt["token_usage"]["output"] == 120
    assert receipt["token_usage"]["cache_hit"] == 50


def test_litellm_dispatch_extracts_token_usage_fallback_cache_field(tmp_path, monkeypatch):
    """LiteLLM cache_hit falls back to top-level prompt_cache_hit_tokens when details absent."""
    _mock_litellm_env(monkeypatch)
    args = _make_args("litellm:deepseek", dispatch_id="litellm-usage-002")
    result = _SpawnResult(
        token_usage={
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "prompt_cache_hit_tokens": 30,
        }
    )
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=result), \
         patch("event_store.EventStore", MagicMock()), \
         patch("providers.behavior_contracts.get_contract", side_effect=KeyError):
        provider_dispatch._dispatch_litellm(args)
    receipt = _last_receipt(tmp_path)
    assert receipt["token_usage"]["input"] == 200
    assert receipt["token_usage"]["output"] == 80
    assert receipt["token_usage"]["cache_hit"] == 30


def test_codex_dispatch_extracts_token_usage(tmp_path):
    """Codex token_usage uses input_tokens / output_tokens / cache_read_tokens."""
    args = _make_args("codex", dispatch_id="codex-usage-001")
    result = _SpawnResult(
        token_usage={
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_tokens": 100,
        }
    )
    with patch("provider_spawns.codex_spawn.spawn_codex", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        provider_dispatch._dispatch_codex(args)
    receipt = _last_receipt(tmp_path)
    assert receipt["token_usage"]["input"] == 500
    assert receipt["token_usage"]["output"] == 200
    assert receipt["token_usage"]["cache_hit"] == 100


def test_gemini_dispatch_extracts_token_usage(tmp_path):
    """Gemini token_usage uses input_tokens / output_tokens (from usageMetadata)."""
    args = _make_args("gemini", dispatch_id="gemini-usage-001")
    result = _SpawnResult(
        token_usage={
            "input_tokens": 420,
            "output_tokens": 160,
            "cache_read_tokens": 0,
        }
    )
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=result), \
         patch("event_store.EventStore", MagicMock()):
        provider_dispatch._dispatch_gemini(args)
    receipt = _last_receipt(tmp_path)
    assert receipt["token_usage"]["input"] == 420
    assert receipt["token_usage"]["output"] == 160


def test_claude_dispatch_extracts_token_usage(tmp_path):
    """Claude token_usage is None (subprocess_dispatch does not return usage); receipt gets zeros."""
    args = _make_args("claude", dispatch_id="claude-usage-001")
    with patch("subprocess_dispatch.deliver_with_recovery", return_value=True):
        provider_dispatch._dispatch_claude(args)
    receipt = _last_receipt(tmp_path)
    assert receipt["token_usage"]["input"] == 0
    assert receipt["token_usage"]["output"] == 0


def test_compute_cost_returns_none_when_tokens_zero():
    """_compute_cost returns None when both input and output are 0."""
    cost = provider_dispatch._compute_cost(
        "litellm:deepseek", "deepseek/deepseek-v4-pro", {"input": 0, "output": 0, "cache_hit": 0}
    )
    assert cost is None


def test_compute_cost_uses_yaml_pricing():
    """_compute_cost reads wave7_models.yaml and returns a non-zero float for valid tokens."""
    token_usage = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 0}
    cost = provider_dispatch._compute_cost("litellm:deepseek", "deepseek/deepseek-v4-pro", token_usage)
    # deepseek-v4-pro: input $0.435/Mtok + output $0.87/Mtok = $1.305 for 1M+1M
    assert cost is not None
    assert cost > 0


def test_litellm_dispatch_cost_usd_in_receipt(tmp_path, monkeypatch):
    """When token counts are non-zero, cost_usd must be a positive float in the receipt."""
    _mock_litellm_env(monkeypatch)
    args = _make_args("litellm:deepseek", dispatch_id="litellm-cost-001")
    result = _SpawnResult(
        token_usage={
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
        }
    )
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=result), \
         patch("event_store.EventStore", MagicMock()), \
         patch("providers.behavior_contracts.get_contract", side_effect=KeyError):
        provider_dispatch._dispatch_litellm(args)
    receipt = _last_receipt(tmp_path)
    assert receipt.get("cost_usd") is not None
    assert receipt["cost_usd"] > 0


def test_warning_logged_when_extraction_fails(caplog):
    """_extract_token_usage logs a warning when token_usage is None."""
    import logging

    class _NullResult:
        token_usage = None

    with caplog.at_level(logging.WARNING, logger="provider_dispatch"):
        usage = provider_dispatch._extract_token_usage(_NullResult(), "litellm:deepseek")
    assert usage == {"input": 0, "output": 0, "cache_hit": 0}
    assert any("token_usage extraction returned 0" in r.message for r in caplog.records)
