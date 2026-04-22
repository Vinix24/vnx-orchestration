#!/usr/bin/env python3
"""F32-R3 claim-verification tests: subprocess adapter must fail-closed when
the subprocess exits with a non-zero code, even when output was successfully parsed.

Scenario matrix:
  exit=0,  events parsed  → success=True   (happy path)
  exit=1,  events parsed  → success=False  (fail-closed — the core regression)
  exit=2,  events parsed  → success=False  (any non-zero code)
  exit=0,  no events      → success=True
  exit=1,  no events      → success=False
  deliver() fails         → success=False  (pre-existing path, unaffected)
  exit=None (process gone after timeout kill) → success=True (None treated as clean)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import deliver_via_subprocess


def _make_observe(returncode):
    """Construct a mock that satisfies obs.transport_state.get('returncode')."""
    obs = MagicMock()
    obs.transport_state = {"returncode": returncode}
    return obs


@pytest.fixture
def mock_adapter():
    with patch("subprocess_dispatch.SubprocessAdapter") as cls:
        instance = MagicMock()
        cls.return_value = instance
        yield instance


class TestFailClosedOnNonZeroExit:
    """Core claim: non-zero exit → success=False, regardless of parsed events."""

    def test_exit0_events_parsed_returns_success(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([
            MagicMock(type="init"),
            MagicMock(type="result"),
        ])
        mock_adapter.observe.return_value = _make_observe(0)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-100")

        assert result.success is True
        assert result.event_count == 2

    def test_exit1_events_parsed_returns_failure(self, mock_adapter):
        """Primary regression: exit=1 with parsed events must be fail-closed."""
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([
            MagicMock(type="init"),
            MagicMock(type="text"),
            MagicMock(type="result"),
        ])
        mock_adapter.observe.return_value = _make_observe(1)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-101")

        assert result.success is False
        assert result.event_count == 3  # events were consumed, but still failure

    def test_exit2_events_parsed_returns_failure(self, mock_adapter):
        """Any non-zero code must fail-close, not just exit=1."""
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([
            MagicMock(type="text"),
        ])
        mock_adapter.observe.return_value = _make_observe(2)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-102")

        assert result.success is False
        assert result.event_count == 1

    def test_exit0_no_events_returns_success(self, mock_adapter):
        """Clean exit with no events (e.g. empty dispatch) is still success."""
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.observe.return_value = _make_observe(0)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-103")

        assert result.success is True
        assert result.event_count == 0

    def test_exit1_no_events_returns_failure(self, mock_adapter):
        """Non-zero exit with no events: already expected to fail."""
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.observe.return_value = _make_observe(1)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-104")

        assert result.success is False
        assert result.event_count == 0

    def test_deliver_failure_skips_observe(self, mock_adapter):
        """deliver() failure returns early — observe() must not be called."""
        mock_adapter.deliver.return_value = MagicMock(success=False)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-105")

        assert result.success is False
        mock_adapter.observe.assert_not_called()
        mock_adapter.read_events_with_timeout.assert_not_called()

    def test_exit_none_treated_as_clean(self, mock_adapter):
        """returncode=None (process removed after timeout kill) is treated as clean.

        The timeout path in read_events_with_timeout calls stop() which removes
        the process from _processes.  observe() then returns a transport_state
        without 'returncode', so .get('returncode') returns None.  None must NOT
        trigger fail-closed — the caller sees an empty event stream and handles
        the timeout separately.
        """
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        obs = MagicMock()
        obs.transport_state = {}  # no 'returncode' key → .get returns None
        mock_adapter.observe.return_value = obs

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-106")

        assert result.success is True

    def test_event_count_preserved_on_fail_closed(self, mock_adapter):
        """Event count in _SubprocessResult must reflect parsed events even on failure."""
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([
            MagicMock(type="init"),
            MagicMock(type="thinking"),
            MagicMock(type="tool_use"),
            MagicMock(type="tool_result"),
            MagicMock(type="text"),
        ])
        mock_adapter.observe.return_value = _make_observe(1)

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-107")

        assert result.success is False
        assert result.event_count == 5

    def test_session_id_preserved_on_fail_closed(self, mock_adapter):
        """Session ID extracted from init event must be returned even on fail-closed."""
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([
            MagicMock(type="init"),
        ])
        mock_adapter.observe.return_value = _make_observe(1)
        mock_adapter.get_session_id.return_value = "ses_xyz"

        result = deliver_via_subprocess("T1", "do work", "sonnet", "d-108")

        assert result.success is False
        assert result.session_id == "ses_xyz"


class TestObserveCallOnEventCompletion:
    """observe() must be called on the correct terminal after events are drained."""

    def test_observe_called_with_terminal_id(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.observe.return_value = _make_observe(0)

        deliver_via_subprocess("T2", "do work", "sonnet", "d-200")

        mock_adapter.observe.assert_called_once_with("T2")

    def test_observe_not_called_when_deliver_fails(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=False)

        deliver_via_subprocess("T2", "do work", "sonnet", "d-201")

        mock_adapter.observe.assert_not_called()
