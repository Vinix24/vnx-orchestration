#!/usr/bin/env python3
"""Tests for OllamaAdapter and capability-based dispatch routing.

Covers:
  - OllamaAdapter.capabilities() returns {DECISION, DIGEST}
  - is_available() / execute() failure paths when Ollama is unreachable
  - Timeout handling
  - Successful DECISION response parsing
  - Dispatch routing: backend-developer → CODE, reviewer → REVIEW
  - Dispatch routing: no capable terminal found
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# Make scripts/lib importable
LIB_DIR = Path(__file__).parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from provider_adapter import Capability, AdapterResult
from adapters.ollama_adapter import OllamaAdapter
from headless_dispatch_daemon import (
    _classify_dispatch,
    _find_capable_terminal,
    DispatchMeta,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_meta(
    role: str = "backend-developer",
    track: str = "A",
    gate: str = "f53-pr3",
    dispatch_id: str = "test-dispatch",
    target_terminal: str = "T1",
) -> DispatchMeta:
    return DispatchMeta(
        dispatch_id=dispatch_id,
        target_terminal=target_terminal,
        track=track,
        role=role,
        gate=gate,
        raw_instruction="## Instruction\nDo the thing.",
    )


def _ollama_json_response(response_text: str) -> bytes:
    """Build a minimal Ollama non-streaming response body."""
    return json.dumps({
        "model": "gemma3:27b",
        "response": response_text,
        "done": True,
    }).encode("utf-8")


class _FakeHTTPResponse:
    """Minimal urllib HTTP response mock — supports both read() and line iteration."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        """Yield body as NDJSON lines for streaming drain compatibility."""
        for line in self._body.split(b"\n"):
            if line:
                yield line + b"\n"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# OllamaAdapter unit tests
# ---------------------------------------------------------------------------

class TestOllamaCapabilities:
    def test_capabilities_returns_decision_and_digest(self):
        adapter = OllamaAdapter("T1")
        caps = adapter.capabilities()
        assert Capability.DECISION in caps
        assert Capability.DIGEST in caps

    def test_capabilities_does_not_include_code(self):
        adapter = OllamaAdapter("T1")
        assert Capability.CODE not in adapter.capabilities()

    def test_capabilities_does_not_include_review(self):
        adapter = OllamaAdapter("T1")
        assert Capability.REVIEW not in adapter.capabilities()

    def test_name_is_ollama(self):
        assert OllamaAdapter("T1").name() == "ollama"


class TestOllamaUnavailableReturnsFailed:
    def test_execute_returns_failed_when_connection_refused(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            result = adapter.execute("tell me something", {"capability": "digest"})

        assert result.status == "failed"
        assert result.output == "ollama_unavailable"
        assert result.provider == "ollama"

    def test_execute_returns_failed_on_oserror(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("network unreachable")
            result = adapter.execute("hello", {})

        assert result.status == "failed"
        assert result.output == "ollama_unavailable"

    def test_is_available_returns_false_when_unreachable(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("refused")
            assert adapter.is_available() is False

    def test_is_available_returns_true_when_reachable(self):
        adapter = OllamaAdapter("T1")
        fake_resp = _FakeHTTPResponse(b'{"models":[]}', status=200)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            assert adapter.is_available() is True


class TestOllamaTimeoutHandling:
    def test_execute_returns_failed_on_timeout_error(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError("timed out")
            result = adapter.execute("question", {"timeout": 1})

        assert result.status == "failed"
        assert result.output == "ollama_unavailable"

    def test_execute_passes_timeout_to_urlopen(self):
        adapter = OllamaAdapter("T1")
        body = _ollama_json_response("hello")
        fake_resp = _FakeHTTPResponse(body)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp) as mock_urlopen:
            adapter.execute("prompt", {"timeout": 42})
            _, call_kwargs = mock_urlopen.call_args
            assert call_kwargs.get("timeout") == 42

    def test_execute_uses_env_var_timeout_when_context_missing(self):
        adapter = OllamaAdapter("T1")
        body = _ollama_json_response("hello")
        fake_resp = _FakeHTTPResponse(body)
        with mock.patch.dict(os.environ, {"VNX_OLLAMA_TIMEOUT": "99"}):
            with mock.patch("urllib.request.urlopen", return_value=fake_resp) as mock_urlopen:
                adapter.execute("prompt", {})
                _, call_kwargs = mock_urlopen.call_args
                assert call_kwargs.get("timeout") == 99


class TestOllamaParseDecisionResponse:
    def _run_execute(self, response_text: str, capability: str = "decision") -> AdapterResult:
        adapter = OllamaAdapter("T1")
        body = _ollama_json_response(response_text)
        fake_resp = _FakeHTTPResponse(body)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            return adapter.execute("decide something", {"capability": capability})

    def test_execute_returns_done_on_valid_json_response(self):
        verdict = json.dumps({"action": "skip", "reasoning": "all clear", "confidence": 0.9})
        result = self._run_execute(verdict)
        assert result.status == "done"
        assert "skip" in result.output

    def test_execute_output_contains_response_text(self):
        verdict = json.dumps({"action": "re_dispatch", "reasoning": "retry", "confidence": 0.8})
        result = self._run_execute(verdict)
        assert "re_dispatch" in result.output

    def test_execute_returns_raw_text_for_digest(self):
        result = self._run_execute("This is a narrative summary.", capability="digest")
        assert result.status == "done"
        assert "narrative summary" in result.output

    def test_execute_extracts_response_field_from_wrapper(self):
        """Ensure OllamaAdapter unwraps {"response": "..."} correctly."""
        adapter = OllamaAdapter("T1")
        wrapped = json.dumps({
            "model": "gemma3:27b",
            "response": '{"action": "escalate"}',
            "done": True,
        }).encode("utf-8")
        fake_resp = _FakeHTTPResponse(wrapped)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            result = adapter.execute("decide", {})
        assert result.status == "done"
        assert "escalate" in result.output

    def test_execute_returns_done_with_correct_provider_and_model(self):
        body = _ollama_json_response("result text")
        fake_resp = _FakeHTTPResponse(body)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            result = OllamaAdapter("T1").execute("prompt", {})
        assert result.provider == "ollama"
        assert result.model == "gemma3:27b"  # default

    def test_execute_uses_custom_model_from_env(self):
        body = _ollama_json_response("ok")
        fake_resp = _FakeHTTPResponse(body)
        with mock.patch.dict(os.environ, {"VNX_OLLAMA_MODEL": "llama3:8b"}):
            adapter = OllamaAdapter("T1")
            with mock.patch("urllib.request.urlopen", return_value=fake_resp):
                result = adapter.execute("prompt", {})
        assert result.model == "llama3:8b"


# ---------------------------------------------------------------------------
# _classify_dispatch routing tests
# ---------------------------------------------------------------------------

class TestDispatchRoutingCodeToClaude:
    """Track A dispatches with code roles require CODE capability."""

    def test_backend_developer_requires_code(self):
        meta = _make_meta(role="backend-developer", track="A")
        caps = _classify_dispatch(meta)
        assert Capability.CODE in caps
        assert Capability.REVIEW not in caps

    def test_frontend_developer_requires_code(self):
        meta = _make_meta(role="frontend-developer", track="A")
        caps = _classify_dispatch(meta)
        assert Capability.CODE in caps

    def test_test_engineer_requires_code(self):
        meta = _make_meta(role="test-engineer", track="B")
        caps = _classify_dispatch(meta)
        assert Capability.CODE in caps

    def test_track_a_with_unknown_role_defaults_to_code(self):
        meta = _make_meta(role="unknown-role", track="A")
        caps = _classify_dispatch(meta)
        assert Capability.CODE in caps

    def test_track_b_requires_code(self):
        meta = _make_meta(role="test-engineer", track="B")
        caps = _classify_dispatch(meta)
        assert Capability.CODE in caps


class TestDispatchRoutingReviewToGemini:
    """Track C dispatches and review roles require REVIEW capability."""

    def test_reviewer_role_requires_review(self):
        meta = _make_meta(role="reviewer", track="C")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps
        assert Capability.CODE not in caps

    def test_architect_role_requires_review(self):
        meta = _make_meta(role="architect", track="C")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps

    def test_code_reviewer_requires_review(self):
        meta = _make_meta(role="code-reviewer", track="C")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps

    def test_security_engineer_requires_review(self):
        meta = _make_meta(role="security-engineer", track="C")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps

    def test_track_c_without_role_requires_review(self):
        meta = _make_meta(role=None, track="C")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps

    def test_gate_containing_review_adds_review_cap(self):
        meta = _make_meta(role="backend-developer", track="A", gate="f53-codex-review")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps
        assert Capability.CODE in caps

    def test_gate_containing_gate_adds_review_cap(self):
        meta = _make_meta(role="backend-developer", track="A", gate="f53-review-gate")
        caps = _classify_dispatch(meta)
        assert Capability.REVIEW in caps


# ---------------------------------------------------------------------------
# _find_capable_terminal tests
# ---------------------------------------------------------------------------

class TestDispatchRoutingNoCapableTerminal:
    """_find_capable_terminal returns None when no terminal can handle requirements."""

    def test_returns_none_when_no_headless_terminals(self, tmp_path):
        """All terminals non-headless → no capable terminal."""
        with mock.patch.dict(os.environ, {}, clear=True):
            # Remove all VNX_ADAPTER_TX vars
            for k in ("VNX_ADAPTER_T1", "VNX_ADAPTER_T2", "VNX_ADAPTER_T3"):
                os.environ.pop(k, None)
            result = _find_capable_terminal({Capability.CODE}, tmp_path)
        assert result is None

    def test_returns_none_when_all_excluded(self, tmp_path):
        """All matching terminals are in the exclude set → None."""
        with mock.patch.dict(os.environ, {
            "VNX_ADAPTER_T1": "subprocess",
            "VNX_PROVIDER_T1": "claude",
        }):
            with mock.patch(
                "headless_dispatch_daemon._is_terminal_available", return_value=True
            ):
                result = _find_capable_terminal(
                    {Capability.CODE}, tmp_path, exclude={"T1", "T2", "T3"}
                )
        assert result is None

    def test_returns_none_when_all_terminals_leased(self, tmp_path):
        """All headless terminals are leased → None."""
        with mock.patch.dict(os.environ, {
            "VNX_ADAPTER_T1": "subprocess",
            "VNX_ADAPTER_T2": "subprocess",
        }):
            with mock.patch(
                "headless_dispatch_daemon._is_terminal_available", return_value=False
            ):
                result = _find_capable_terminal({Capability.CODE}, tmp_path)
        assert result is None

    def test_returns_terminal_when_capable_and_idle(self, tmp_path):
        """A headless, idle, CODE-capable terminal is returned."""
        with mock.patch.dict(os.environ, {
            "VNX_ADAPTER_T1": "subprocess",
            "VNX_PROVIDER_T1": "claude",
        }):
            with mock.patch(
                "headless_dispatch_daemon._is_terminal_available", return_value=True
            ):
                result = _find_capable_terminal({Capability.CODE}, tmp_path)
        assert result == "T1"

    def test_skips_incapable_terminal_returns_next(self, tmp_path):
        """First terminal (Ollama, DECISION-only) lacks CODE → falls through to T2 (Claude)."""
        with mock.patch.dict(os.environ, {
            "VNX_ADAPTER_T1": "subprocess",
            "VNX_PROVIDER_T1": "ollama",
            "VNX_ADAPTER_T2": "subprocess",
            "VNX_PROVIDER_T2": "claude",
        }):
            with mock.patch(
                "headless_dispatch_daemon._is_terminal_available", return_value=True
            ):
                result = _find_capable_terminal({Capability.CODE}, tmp_path)
        assert result == "T2"
