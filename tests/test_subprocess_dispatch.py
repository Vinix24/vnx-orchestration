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
        cls.return_value = instance
        yield instance


class TestDeliverViaSubprocess:
    def test_success_consumes_all_events(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events.return_value = iter([
            MagicMock(type="init"),
            MagicMock(type="text"),
            MagicMock(type="result"),
        ])

        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-001")

        assert result is True
        mock_adapter.deliver.assert_called_once_with(
            "T1", "d-001", instruction="do stuff", model="sonnet",
        )
        mock_adapter.read_events.assert_called_once_with("T1")

    def test_failure_returns_false_without_reading(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=False)

        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-002")

        assert result is False
        mock_adapter.read_events.assert_not_called()

    def test_empty_event_stream_succeeds(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events.return_value = iter([])

        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-003")

        assert result is True
        mock_adapter.read_events.assert_called_once_with("T1")
