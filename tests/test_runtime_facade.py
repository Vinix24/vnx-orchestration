#!/usr/bin/env python3
"""Runtime facade tests for PR-2: Runtime Launch and Observation Facade.

Covers:
  1. Launch/spawn through facade
  2. Observation through facade
  3. Health and session health through facade
  4. Deliver through facade
  5. Failure propagation (transport failures surface as RuntimeOutcome)
  6. Capability gating (unsupported ops return error)
  7. Attach, inspect, reheal through facade
  8. Stop through facade
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from runtime_facade import CANONICAL_TERMINALS, RuntimeFacade, RuntimeOutcome, get_adapter
from tmux_adapter import TmuxAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter(tmp_path: Path) -> TmuxAdapter:
    panes = {
        "T0": {"pane_id": "%0", "work_dir": "/project/.claude/terminals/T0"},
        "T1": {"pane_id": "%1", "work_dir": "/project/.claude/terminals/T1"},
        "T2": {"pane_id": "%2", "work_dir": "/project/.claude/terminals/T2"},
        "T3": {"pane_id": "%3", "work_dir": "/project/.claude/terminals/T3"},
    }
    (tmp_path / "panes.json").write_text(json.dumps(panes))
    return TmuxAdapter(tmp_path)


@pytest.fixture()
def facade(adapter: TmuxAdapter) -> RuntimeFacade:
    return RuntimeFacade(adapter)


def _mock_tmux_success():
    return patch("tmux_adapter._run_tmux",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="12345\n", stderr=""))


def _mock_tmux_failure():
    return patch("tmux_adapter._run_tmux",
        return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="pane not found"))


# ---------------------------------------------------------------------------
# 1. Launch through facade
# ---------------------------------------------------------------------------

class TestFacadeLaunch:

    def test_launch_existing_terminal(self, facade: RuntimeFacade) -> None:
        outcome = facade.launch("T0", {"session_name": "vnx-test"})
        assert outcome.success is True
        assert outcome.operation == "launch"
        assert outcome.details["transport_ref"] == "%0"

    def test_launch_missing_session_fails(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        f = RuntimeFacade(TmuxAdapter(tmp_path))
        outcome = f.launch("T9", {})
        assert outcome.success is False
        assert "session_name" in outcome.error


# ---------------------------------------------------------------------------
# 2. Observation through facade
# ---------------------------------------------------------------------------

class TestFacadeObservation:

    def test_observe_returns_transport_state(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            outcome = facade.observe("T0")
            assert outcome.success is True
            assert outcome.details["exists"] is True
            assert outcome.details["responsive"] is True

    def test_observe_missing_terminal(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        f = RuntimeFacade(TmuxAdapter(tmp_path))
        with _mock_tmux_success():
            outcome = f.observe("T9")
            assert outcome.success is False


# ---------------------------------------------------------------------------
# 3. Health through facade
# ---------------------------------------------------------------------------

class TestFacadeHealth:

    def test_health_healthy_terminal(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            outcome = facade.health("T0")
            assert outcome.success is True
            assert outcome.details["healthy"] is True
            assert outcome.details["process_alive"] is True

    def test_health_degraded_terminal(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_failure():
            outcome = facade.health("T0")
            assert outcome.success is False
            assert outcome.details["healthy"] is False

    def test_session_health_all_terminals(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            result = facade.session_health()
            assert result.ok is True
            summary = result.data
            assert summary["total"] == 4
            assert summary["healthy"] == 4
            assert summary["degraded"] == []

    def test_session_health_degraded(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_failure():
            result = facade.session_health(["T0", "T1"])
            assert result.ok is True
            assert len(result.data["degraded"]) == 2

    def test_session_health_custom_terminals(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            result = facade.session_health(["T2"])
            assert result.ok is True
            assert result.data["total"] == 1

    def test_session_health_empty_list_returns_zero(self, facade: RuntimeFacade) -> None:
        result = facade.session_health([])
        assert result.ok is True
        assert result.data["total"] == 0
        assert result.data["healthy"] == 0
        assert result.data["terminals"] == {}


# ---------------------------------------------------------------------------
# 4. Deliver through facade
# ---------------------------------------------------------------------------

class TestFacadeDeliver:

    def test_deliver_returns_outcome(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success(), patch("tmux_adapter._tmux_available", return_value=True):
            outcome = facade.deliver("T0", "20260402-120000-test")
            assert outcome.operation == "deliver"
            assert outcome.details["dispatch_id"] == "20260402-120000-test"

    def test_deliver_missing_terminal(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        f = RuntimeFacade(TmuxAdapter(tmp_path))
        with patch("tmux_adapter._tmux_available", return_value=True):
            outcome = f.deliver("T9", "20260402-120000-test")
            assert outcome.success is False
            assert outcome.error is not None


# ---------------------------------------------------------------------------
# 5. Failure propagation
# ---------------------------------------------------------------------------

class TestFailurePropagation:

    def test_transport_failure_surfaces_as_outcome(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_failure():
            outcome = facade.health("T0")
            assert outcome.success is False
            assert outcome.operation == "health"

    def test_tmux_missing_surfaces_as_outcome(self, facade: RuntimeFacade) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            outcome = facade.observe("T0")
            assert outcome.success is False
            assert outcome.error == "tmux not available"

    def test_no_exception_on_transport_failure(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_failure():
            outcome = facade.observe("T0")
            assert isinstance(outcome, RuntimeOutcome)

    def test_inspect_failure_propagates(self, facade: RuntimeFacade) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            outcome = facade.inspect("T0")
            assert outcome.success is False


# ---------------------------------------------------------------------------
# 6. Capability gating
# ---------------------------------------------------------------------------

class TestCapabilityGating:

    def test_unsupported_op_returns_error(self, facade: RuntimeFacade) -> None:
        with patch.dict("os.environ", {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            outcome = facade.launch("T0", {})
            assert outcome.success is False
            assert "not supported" in outcome.error

    def test_has_capability_check(self, facade: RuntimeFacade) -> None:
        assert facade.has_capability("SPAWN") is True
        assert facade.has_capability("NONEXISTENT") is False

    def test_adapter_type_exposed(self, facade: RuntimeFacade) -> None:
        assert facade.adapter_type == "tmux"

    def test_session_health_unsupported(self, facade: RuntimeFacade) -> None:
        with patch.dict("os.environ", {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            result = facade.session_health()
            assert result.ok is False
            assert result.error_code == "unsupported"


# ---------------------------------------------------------------------------
# 7. Attach, inspect, reheal
# ---------------------------------------------------------------------------

class TestOtherOperations:

    def test_attach_through_facade(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            outcome = facade.attach("T0")
            assert outcome.operation == "attach"

    def test_inspect_through_facade(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            outcome = facade.inspect("T0")
            assert outcome.success is True
            assert outcome.details["has_content"] is True

    def test_reheal_through_facade(self, facade: RuntimeFacade) -> None:
        outcome = facade.reheal("T0")
        assert outcome.operation == "reheal"


# ---------------------------------------------------------------------------
# 8. Stop through facade
# ---------------------------------------------------------------------------

class TestFacadeStop:

    def test_stop_running_terminal(self, facade: RuntimeFacade) -> None:
        with _mock_tmux_success():
            outcome = facade.stop("T0")
            assert outcome.success is True
            assert outcome.details["was_running"] is True

    def test_stop_missing_terminal(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        f = RuntimeFacade(TmuxAdapter(tmp_path))
        with _mock_tmux_success():
            outcome = f.stop("T9")
            assert outcome.success is True
            assert outcome.details["was_running"] is False


# ---------------------------------------------------------------------------
# 9. get_adapter factory
# ---------------------------------------------------------------------------

class TestGetAdapterFactory:

    def test_default_selects_tmux_adapter(self, tmp_path: Path) -> None:
        """No env var set — must return TmuxAdapter."""
        env = {"VNX_STATE_DIR": str(tmp_path)}
        # Ensure VNX_ADAPTER_T1 is absent so the default path is exercised.
        with patch.dict("os.environ", env):
            with patch.dict("os.environ", {}, clear=False):
                import os as _os
                _os.environ.pop("VNX_ADAPTER_T1", None)
                adapter = get_adapter("T1")
        assert isinstance(adapter, TmuxAdapter)

    def test_explicit_tmux_selects_tmux_adapter(self, tmp_path: Path) -> None:
        """VNX_ADAPTER_T1=tmux — must return TmuxAdapter."""
        with patch.dict("os.environ", {"VNX_ADAPTER_T1": "tmux", "VNX_STATE_DIR": str(tmp_path)}):
            adapter = get_adapter("T1")
        assert isinstance(adapter, TmuxAdapter)

    def test_subprocess_raises_not_implemented(self) -> None:
        """VNX_ADAPTER_T1=subprocess — must raise NotImplementedError (placeholder)."""
        with patch.dict("os.environ", {"VNX_ADAPTER_T1": "subprocess"}):
            with pytest.raises(NotImplementedError, match="SubprocessAdapter"):
                get_adapter("T1")

    def test_invalid_value_raises_value_error(self) -> None:
        """Unrecognised value raises ValueError with the offending key."""
        with patch.dict("os.environ", {"VNX_ADAPTER_T1": "docker"}):
            with pytest.raises(ValueError, match="VNX_ADAPTER_T1"):
                get_adapter("T1")
