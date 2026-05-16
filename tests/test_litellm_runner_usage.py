#!/usr/bin/env python3
"""test_litellm_runner_usage.py — OI-1481: _litellm_runner stream_options usage tests.

Verifies that:
  - litellm.completion is always called with stream_options={"include_usage": True}
  - usage_complete event is emitted when a usage chunk is present in the stream
  - usage_complete event is NOT emitted when no usage chunk arrives
  - _provider_prefix extracts the prefix correctly
  - _emit_usage serializes usage objects via model_dump, dict, and fallback
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib" / "adapters"))

import _litellm_runner as runner


# ---------------------------------------------------------------------------
# OI-1481: stream_options always included
# ---------------------------------------------------------------------------

def test_completion_call_includes_stream_options_include_usage(monkeypatch):
    """litellm.completion must receive stream_options={"include_usage": True} for every provider."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")

    payload = {"model": "deepseek/v4-pro", "messages": [{"role": "user", "content": "hi"}]}
    stdin_data = json.dumps(payload)

    captured_kwargs: list[dict] = []

    def _fake_completion(**kwargs):
        captured_kwargs.append(kwargs)
        return iter([])

    litellm_mock = MagicMock()
    litellm_mock.completion.side_effect = _fake_completion
    litellm_mock.suppress_debug_info = False

    monkeypatch.setattr(sys, "stdin", StringIO(stdin_data))
    captured_stdout = StringIO()
    monkeypatch.setattr(sys, "stdout", captured_stdout)

    with patch.dict("sys.modules", {"litellm": litellm_mock}):
        runner.main()

    assert captured_kwargs, "litellm.completion was not called"
    assert "stream_options" in captured_kwargs[0], "stream_options missing from completion call"
    assert captured_kwargs[0]["stream_options"] == {"include_usage": True}


def test_completion_call_includes_stream_options_for_non_deepseek(monkeypatch):
    """stream_options must also be passed for providers not in the old frozenset."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "fake-key")

    payload = {"model": "moonshot/v1-8k", "messages": [{"role": "user", "content": "hi"}]}

    captured_kwargs: list[dict] = []

    def _fake_completion(**kwargs):
        captured_kwargs.append(kwargs)
        return iter([])

    litellm_mock = MagicMock()
    litellm_mock.completion.side_effect = _fake_completion
    litellm_mock.suppress_debug_info = False

    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", StringIO())

    with patch.dict("sys.modules", {"litellm": litellm_mock}):
        runner.main()

    assert captured_kwargs, "litellm.completion was not called"
    assert captured_kwargs[0].get("stream_options") == {"include_usage": True}


# ---------------------------------------------------------------------------
# OI-1481: usage event propagation
# ---------------------------------------------------------------------------

def test_usage_event_propagated_when_present(monkeypatch):
    """When stream yields a chunk with usage, a usage_complete event is emitted to stdout."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")

    usage_obj = MagicMock()
    usage_obj.model_dump.return_value = {
        "prompt_tokens": 215,
        "completion_tokens": 47,
        "total_tokens": 262,
    }

    chunk_with_usage = MagicMock()
    chunk_with_usage.usage = usage_obj
    chunk_with_usage.model_dump.return_value = {
        "choices": [],
        "usage": {"prompt_tokens": 215, "completion_tokens": 47, "total_tokens": 262},
    }

    payload = {"model": "deepseek/v4-pro", "messages": [{"role": "user", "content": "hi"}]}

    litellm_mock = MagicMock()
    litellm_mock.completion.return_value = iter([chunk_with_usage])
    litellm_mock.suppress_debug_info = False

    out = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", out)

    with patch.dict("sys.modules", {"litellm": litellm_mock}):
        rc = runner.main()

    assert rc == 0
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    event_types = [json.loads(l).get("event_type") for l in lines if "event_type" in l]
    assert "usage_complete" in event_types, f"usage_complete missing from: {event_types}"

    usage_line = next(l for l in lines if json.loads(l).get("event_type") == "usage_complete")
    usage_data = json.loads(usage_line)["usage"]
    assert usage_data["prompt_tokens"] == 215
    assert usage_data["completion_tokens"] == 47


def test_usage_event_not_emitted_when_absent(monkeypatch):
    """When no chunk carries usage, no usage_complete event is written."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")

    chunk_no_usage = MagicMock()
    chunk_no_usage.usage = None
    chunk_no_usage.model_dump.return_value = {
        "choices": [{"delta": {"content": "hi"}, "finish_reason": None}]
    }

    payload = {"model": "deepseek/v4-pro", "messages": [{"role": "user", "content": "hi"}]}

    litellm_mock = MagicMock()
    litellm_mock.completion.return_value = iter([chunk_no_usage])
    litellm_mock.suppress_debug_info = False

    out = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", out)

    with patch.dict("sys.modules", {"litellm": litellm_mock}):
        rc = runner.main()

    assert rc == 0
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    event_types = [json.loads(l).get("event_type") for l in lines]
    assert "usage_complete" not in event_types


# ---------------------------------------------------------------------------
# _provider_prefix unit tests
# ---------------------------------------------------------------------------

def test_provider_prefix_extracts_from_slash_model():
    assert runner._provider_prefix("deepseek/v4-pro") == "deepseek"


def test_provider_prefix_returns_empty_when_no_slash():
    assert runner._provider_prefix("plain-model") == ""


# ---------------------------------------------------------------------------
# _emit_usage serialization paths
# ---------------------------------------------------------------------------

def test_emit_usage_uses_model_dump_when_available(monkeypatch, capsys):
    usage = MagicMock()
    usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 5}
    runner._emit_usage(usage)
    captured = capsys.readouterr()
    obj = json.loads(captured.out.strip())
    assert obj["event_type"] == "usage_complete"
    assert obj["usage"]["prompt_tokens"] == 10


def test_emit_usage_fallback_to_dict_method(monkeypatch, capsys):
    class _LegacyUsage:
        def dict(self):
            return {"prompt_tokens": 20, "completion_tokens": 8}

    # Ensure model_dump is absent (hasattr returns False)
    assert not hasattr(_LegacyUsage(), "model_dump")

    usage = _LegacyUsage()
    runner._emit_usage(usage)
    captured = capsys.readouterr()
    obj = json.loads(captured.out.strip())
    assert obj["usage"]["prompt_tokens"] == 20
