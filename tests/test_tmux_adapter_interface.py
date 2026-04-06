#!/usr/bin/env python3
"""TmuxAdapter RuntimeAdapter interface and direct-coupling freeze tests (PR-1, Feature 16).

Covers:
  1. Capability declaration matches contract
  2. All RuntimeAdapter operations are callable
  3. Spawn/stop idempotency
  4. Observe/health read-only behavior
  5. Session health aggregation
  6. Error hierarchy structure
  7. Direct-coupling freeze guard
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

from adapter_types import (
    CAPABILITY_ATTACH,
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
    CAPABILITY_INSPECT,
    CAPABILITY_OBSERVE,
    CAPABILITY_REHEAL,
    CAPABILITY_SESSION_HEALTH,
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    AttachResult,
    HealthResult,
    InspectionResult,
    ObservationResult,
    RehealResult,
    RuntimeAdapterError,
    SessionHealthResult,
    SpawnResult,
    StopResult,
    UnsupportedCapability,
)
from tmux_adapter import (
    TMUX_CAPABILITIES,
    AdapterConfigError,
    AdapterTransportError,
    TmuxAdapter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter(tmp_path: Path) -> TmuxAdapter:
    """Create a TmuxAdapter with a temp state dir and mock panes.json."""
    panes = {
        "T0": {"pane_id": "%0", "work_dir": "/project/.claude/terminals/T0"},
        "T1": {"pane_id": "%1", "work_dir": "/project/.claude/terminals/T1"},
        "T2": {"pane_id": "%2", "work_dir": "/project/.claude/terminals/T2"},
        "T3": {"pane_id": "%3", "work_dir": "/project/.claude/terminals/T3"},
    }
    (tmp_path / "panes.json").write_text(json.dumps(panes))
    return TmuxAdapter(tmp_path)


# ---------------------------------------------------------------------------
# 1. Capability declaration
# ---------------------------------------------------------------------------

class TestCapabilityDeclaration:

    def test_tmux_adapter_supports_all_capabilities(self, adapter: TmuxAdapter) -> None:
        caps = adapter.capabilities()
        assert CAPABILITY_SPAWN in caps
        assert CAPABILITY_STOP in caps
        assert CAPABILITY_DELIVER in caps
        assert CAPABILITY_ATTACH in caps
        assert CAPABILITY_OBSERVE in caps
        assert CAPABILITY_INSPECT in caps
        assert CAPABILITY_HEALTH in caps
        assert CAPABILITY_SESSION_HEALTH in caps
        assert CAPABILITY_REHEAL in caps

    def test_disabled_adapter_has_no_capabilities(self, adapter: TmuxAdapter) -> None:
        with patch.dict("os.environ", {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            caps = adapter.capabilities()
            assert len(caps) == 0

    def test_adapter_type_is_tmux(self, adapter: TmuxAdapter) -> None:
        assert adapter.adapter_type() == "tmux"

    def test_capability_set_matches_contract(self) -> None:
        required = {CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER,
                     CAPABILITY_OBSERVE, CAPABILITY_HEALTH, CAPABILITY_SESSION_HEALTH}
        assert required.issubset(TMUX_CAPABILITIES)


# ---------------------------------------------------------------------------
# 2. Spawn and stop idempotency
# ---------------------------------------------------------------------------

class TestSpawnStopIdempotency:

    def test_spawn_existing_terminal_returns_success(self, adapter: TmuxAdapter) -> None:
        result = adapter.spawn("T0", {"session_name": "vnx-test"})
        assert result.success is True
        assert result.transport_ref == "%0"

    def test_stop_nonexistent_terminal_returns_success(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        result = a.stop("T9")
        assert result.success is True
        assert result.was_running is False

    def test_spawn_missing_session_name_fails(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        result = a.spawn("T5", {})
        assert result.success is False
        assert "session_name" in result.error


# ---------------------------------------------------------------------------
# 3. Observe and health
# ---------------------------------------------------------------------------

class TestObserveAndHealth:

    def test_observe_missing_terminal_returns_not_exists(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        result = a.observe("T9")
        assert result.exists is False

    def test_health_missing_terminal_returns_unhealthy(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        result = a.health("T9")
        assert result.healthy is False
        assert result.surface_exists is False

    def test_attach_missing_terminal_returns_failure(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        result = a.attach("T9")
        assert result.success is False


# ---------------------------------------------------------------------------
# 4. Session health aggregation
# ---------------------------------------------------------------------------

class TestSessionHealth:

    def test_session_health_reports_all_terminals(self, adapter: TmuxAdapter) -> None:
        # Mock _run_tmux to simulate panes being gone (no tmux in CI)
        with patch("tmux_adapter._run_tmux") as mock_tmux:
            mock_tmux.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="pane not found",
            )
            result = adapter.session_health(["T0", "T1", "T2", "T3"])
            assert len(result.terminals) == 4
            assert len(result.degraded_terminals) == 4
            assert result.session_exists is False

    def test_session_health_with_healthy_terminal(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._run_tmux") as mock_tmux:
            mock_tmux.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="12345\n", stderr="",
            )
            result = adapter.session_health(["T0"])
            assert result.terminals["T0"].healthy is True
            assert result.session_exists is True
            assert result.degraded_terminals == []


# ---------------------------------------------------------------------------
# 5. Error hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:

    def test_all_errors_inherit_from_runtime_adapter_error(self) -> None:
        assert issubclass(AdapterConfigError, RuntimeAdapterError)
        assert issubclass(AdapterTransportError, RuntimeAdapterError)
        assert issubclass(UnsupportedCapability, RuntimeAdapterError)

    def test_unsupported_capability_carries_operation(self) -> None:
        err = UnsupportedCapability("ATTACH", "headless")
        assert err.operation == "ATTACH"
        assert err.adapter_type == "headless"

    def test_transport_error_carries_detail(self) -> None:
        err = AdapterTransportError("pane dead", transport_detail="returncode=1")
        assert err.transport_detail == "returncode=1"


# ---------------------------------------------------------------------------
# 6. Result dataclass structure
# ---------------------------------------------------------------------------

class TestResultDataclasses:

    def test_spawn_result_defaults(self) -> None:
        r = SpawnResult(success=True)
        assert r.transport_ref == ""
        assert r.error is None

    def test_observation_result_defaults(self) -> None:
        r = ObservationResult(exists=True)
        assert r.responsive is False
        assert r.transport_state == {}

    def test_health_result_defaults(self) -> None:
        r = HealthResult(healthy=False)
        assert r.surface_exists is False
        assert r.process_alive is False

    def test_session_health_result_defaults(self) -> None:
        r = SessionHealthResult(session_exists=False)
        assert r.terminals == {}
        assert r.degraded_terminals == []

    def test_reheal_result_defaults(self) -> None:
        r = RehealResult(rehealed=False)
        assert r.strategy == ""

    def test_shutdown_is_noop(self, adapter: TmuxAdapter) -> None:
        adapter.shutdown(graceful=True)
        adapter.shutdown(graceful=False)


# ---------------------------------------------------------------------------
# 7. Direct-coupling freeze guard
# ---------------------------------------------------------------------------

class TestDirectCouplingFreeze:
    """Ensure no new direct tmux subprocess calls exist outside tmux_adapter.py."""

    PROTECTED_PATH = Path(__file__).parent.parent / "scripts" / "lib"
    ADAPTER_FILES = {"tmux_adapter.py", "tmux_session_profile.py"}

    def test_no_direct_tmux_in_protected_modules(self) -> None:
        """No scripts/lib/*.py file (except adapter files) should call tmux directly."""
        violations: list[str] = []
        for py_file in sorted(self.PROTECTED_PATH.glob("*.py")):
            if py_file.name in self.ADAPTER_FILES:
                continue
            content = py_file.read_text(encoding="utf-8")
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if "subprocess" in line and "tmux" in line:
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
        assert violations == [], (
            f"Direct tmux coupling found outside adapter:\n" +
            "\n".join(violations)
        )

    def test_adapter_is_sole_tmux_entry_point(self) -> None:
        """tmux_adapter.py must contain the subprocess+tmux calls."""
        adapter_path = self.PROTECTED_PATH / "tmux_adapter.py"
        content = adapter_path.read_text(encoding="utf-8")
        tmux_calls = [
            line.strip() for line in content.splitlines()
            if "subprocess" in line and "tmux" in line and not line.lstrip().startswith("#")
        ]
        assert len(tmux_calls) >= 1, "TmuxAdapter must contain tmux subprocess calls"


# ---------------------------------------------------------------------------
# 8. tmux-missing guard (OI-569)
# ---------------------------------------------------------------------------

class TestTmuxMissingGuard:
    """All non-delivery methods return error results when tmux is absent."""

    def test_stop_without_tmux(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            result = adapter.stop("T0")
            assert result.success is True
            assert result.was_running is False
            assert result.error == "tmux not available"

    def test_attach_without_tmux(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            result = adapter.attach("T0")
            assert result.success is False
            assert result.error == "tmux not available"

    def test_observe_without_tmux(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            result = adapter.observe("T0")
            assert result.exists is False
            assert result.error == "tmux not available"

    def test_inspect_without_tmux(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            result = adapter.inspect("T0")
            assert result.exists is False
            assert result.error == "tmux not available"

    def test_health_without_tmux(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            result = adapter.health("T0")
            assert result.healthy is False
            assert result.error == "tmux not available"

    def test_session_health_without_tmux(self, adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter._tmux_available", return_value=False):
            result = adapter.session_health(["T0", "T1"])
            assert result.session_exists is False
            assert len(result.degraded_terminals) == 2
            for tid in ("T0", "T1"):
                assert result.terminals[tid].error == "tmux not available"
