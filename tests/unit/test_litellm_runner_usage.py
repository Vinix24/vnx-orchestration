"""Unit tests for _litellm_runner usage serialization fallback (codex R1 fix).

Verifies that _emit_usage() emits a placeholder dict with warning log
instead of silently returning when usage serialization fails.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "lib" / "adapters"))

import _litellm_runner


class _BrokenUsage:
    """Object where dict() raises AttributeError (has keys() that raises AttributeError)."""
    def keys(self):
        raise AttributeError("usage object has no valid keys")


class _ModelDumpUsage:
    """Object with model_dump() — happy path."""
    def model_dump(self) -> dict:
        return {"input_tokens": 10, "output_tokens": 5}


class _DictUsage:
    """Object with dict() — happy path via .dict()."""
    def dict(self) -> dict:
        return {"input_tokens": 20, "output_tokens": 8}


class TestEmitUsageFallback:

    def test_litellm_runner_emits_placeholder_on_usage_failure(self, caplog):
        """_emit_usage emits placeholder dict + warning log when dict(usage) raises AttributeError."""
        emitted: list[dict] = []

        with patch.object(_litellm_runner, "_emit", side_effect=emitted.append):
            with caplog.at_level(logging.WARNING, logger="_litellm_runner"):
                _litellm_runner._emit_usage(_BrokenUsage())

        assert len(emitted) == 1, f"Expected 1 emitted event, got {len(emitted)}"
        event = emitted[0]
        assert event["event_type"] == "usage_complete"
        assert event["usage"]["usage_serialization_failed"] is True
        assert event["usage"]["input_tokens"] == 0
        assert event["usage"]["output_tokens"] == 0

    def test_litellm_runner_logs_warning_on_usage_failure(self, caplog):
        """_emit_usage logs a WARNING when usage serialization falls back to placeholder."""
        with patch.object(_litellm_runner, "_emit", lambda _: None):
            with caplog.at_level(logging.WARNING, logger="_litellm_runner"):
                _litellm_runner._emit_usage(_BrokenUsage())

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "Expected at least one WARNING log record"
        assert "usage serialization fallback" in warning_records[0].message

    def test_emit_usage_happy_path_model_dump(self):
        """_emit_usage emits real usage dict when model_dump() is available."""
        emitted: list[dict] = []

        with patch.object(_litellm_runner, "_emit", side_effect=emitted.append):
            _litellm_runner._emit_usage(_ModelDumpUsage())

        assert len(emitted) == 1
        assert emitted[0]["usage"]["input_tokens"] == 10
        assert emitted[0]["usage"]["output_tokens"] == 5

    def test_emit_usage_happy_path_dict_method(self):
        """_emit_usage emits real usage dict when .dict() is available."""
        emitted: list[dict] = []

        with patch.object(_litellm_runner, "_emit", side_effect=emitted.append):
            _litellm_runner._emit_usage(_DictUsage())

        assert len(emitted) == 1
        assert emitted[0]["usage"]["input_tokens"] == 20
        assert emitted[0]["usage"]["output_tokens"] == 8
