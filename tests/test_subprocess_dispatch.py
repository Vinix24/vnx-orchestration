#!/usr/bin/env python3
"""Tests for subprocess_dispatch — event pipeline wiring."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import deliver_via_subprocess


@pytest.fixture
def mock_adapter():
    """Patch SubprocessAdapter and return the mock instance."""
    with patch("subprocess_dispatch.SubprocessAdapter") as cls:
        instance = MagicMock()
        instance.was_timed_out.return_value = False
        cls.return_value = instance
        yield instance


def _mock_observe(returncode=0):
    """Return a mock ObservationResult-like object with the given returncode."""
    obs = MagicMock()
    obs.transport_state = {"returncode": returncode}
    return obs


class TestDeliverViaSubprocess:
    def test_success_consumes_all_events(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([
            MagicMock(type="init"),
            MagicMock(type="text"),
            MagicMock(type="result"),
        ])
        mock_adapter.observe.return_value = _mock_observe(0)

        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-001")

        assert result.success is True
        call_args, call_kwargs = mock_adapter.deliver.call_args
        assert call_args == ("T1", "d-001")
        assert call_kwargs["model"] == "sonnet"
        assert "do stuff" in call_kwargs["instruction"]
        assert call_kwargs.get("cwd") is None
        mock_adapter.read_events_with_timeout.assert_called_once_with(
            "T1", chunk_timeout=300.0, total_deadline=900.0,
        )

    def test_failure_returns_false_without_reading(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=False)

        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-002")

        assert result.success is False
        mock_adapter.read_events_with_timeout.assert_not_called()

    def test_empty_event_stream_succeeds(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.observe.return_value = _mock_observe(0)

        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-003")

        assert result.success is True
        mock_adapter.read_events_with_timeout.assert_called_once_with(
            "T1", chunk_timeout=300.0, total_deadline=900.0,
        )
