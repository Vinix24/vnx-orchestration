#!/usr/bin/env python3
"""Adapter conformance tests for PR-3: Headless Transport Abstraction.

Proves that both TmuxAdapter and HeadlessAdapter conform to the
RuntimeAdapter protocol and contract requirements:
  1. Protocol conformance (isinstance check)
  2. Required capability declaration
  3. Unsupported operation semantics
  4. Spawn/stop idempotency
  5. Observe safety (read-only, no crash)
  6. Capability declaration matches behavior
  7. Tmux remains default adapter
  8. HeadlessAdapter-specific behavior
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from adapter_protocol import (
    REQUIRED_CAPABILITIES,
    RuntimeAdapter,
    validate_required_capabilities,
)
from headless_transport_adapter import (
    HEADLESS_CAPABILITIES,
    HeadlessAdapter,
)
from tmux_adapter import (
    CAPABILITY_ATTACH,
    CAPABILITY_REHEAL,
    TMUX_CAPABILITIES,
    TmuxAdapter,
    UnsupportedCapability,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmux_adapter(tmp_path: Path) -> TmuxAdapter:
    panes = {f"T{i}": {"pane_id": f"%{i}", "work_dir": f"/project/T{i}"} for i in range(4)}
    (tmp_path / "panes.json").write_text(json.dumps(panes))
    return TmuxAdapter(tmp_path)


@pytest.fixture()
def headless_adapter() -> HeadlessAdapter:
    return HeadlessAdapter()


# ---------------------------------------------------------------------------
# 1. Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:

    def test_tmux_adapter_is_runtime_adapter(self, tmux_adapter: TmuxAdapter) -> None:
        assert isinstance(tmux_adapter, RuntimeAdapter)

    def test_headless_adapter_is_runtime_adapter(self, headless_adapter: HeadlessAdapter) -> None:
        assert isinstance(headless_adapter, RuntimeAdapter)

    def test_both_have_adapter_type(self, tmux_adapter: TmuxAdapter, headless_adapter: HeadlessAdapter) -> None:
        assert tmux_adapter.adapter_type() == "tmux"
        assert headless_adapter.adapter_type() == "headless"


# ---------------------------------------------------------------------------
# 2. Required capability declaration
# ---------------------------------------------------------------------------

class TestRequiredCapabilities:

    def test_tmux_has_all_required(self, tmux_adapter: TmuxAdapter) -> None:
        missing = validate_required_capabilities(tmux_adapter)
        assert missing == []

    def test_headless_has_all_required(self, headless_adapter: HeadlessAdapter) -> None:
        missing = validate_required_capabilities(headless_adapter)
        assert missing == []

    def test_tmux_capabilities_superset_of_required(self) -> None:
        assert REQUIRED_CAPABILITIES.issubset(TMUX_CAPABILITIES)

    def test_headless_capabilities_superset_of_required(self) -> None:
        assert REQUIRED_CAPABILITIES.issubset(HEADLESS_CAPABILITIES)


# ---------------------------------------------------------------------------
# 3. Unsupported operation semantics
# ---------------------------------------------------------------------------

class TestUnsupportedOperations:

    def test_headless_attach_raises_unsupported(self, headless_adapter: HeadlessAdapter) -> None:
        with pytest.raises(UnsupportedCapability) as exc_info:
            headless_adapter.attach("T0")
        assert exc_info.value.operation == "ATTACH"
        assert exc_info.value.adapter_type == "headless"

    def test_headless_reheal_raises_unsupported(self, headless_adapter: HeadlessAdapter) -> None:
        with pytest.raises(UnsupportedCapability) as exc_info:
            headless_adapter.reheal("T0")
        assert exc_info.value.operation == "REHEAL"

    def test_headless_does_not_declare_attach(self, headless_adapter: HeadlessAdapter) -> None:
        assert CAPABILITY_ATTACH not in headless_adapter.capabilities()

    def test_headless_does_not_declare_reheal(self, headless_adapter: HeadlessAdapter) -> None:
        assert CAPABILITY_REHEAL not in headless_adapter.capabilities()

    def test_tmux_supports_attach(self, tmux_adapter: TmuxAdapter) -> None:
        assert CAPABILITY_ATTACH in tmux_adapter.capabilities()

    def test_tmux_supports_reheal(self, tmux_adapter: TmuxAdapter) -> None:
        assert CAPABILITY_REHEAL in tmux_adapter.capabilities()


# ---------------------------------------------------------------------------
# 4. Spawn/stop idempotency
# ---------------------------------------------------------------------------

class TestSpawnStopIdempotency:

    def test_tmux_spawn_idempotent(self, tmux_adapter: TmuxAdapter) -> None:
        r1 = tmux_adapter.spawn("T0", {"session_name": "test"})
        r2 = tmux_adapter.spawn("T0", {"session_name": "test"})
        assert r1.success and r2.success
        assert r1.transport_ref == r2.transport_ref

    def test_headless_stop_nonexistent_succeeds(self, headless_adapter: HeadlessAdapter) -> None:
        result = headless_adapter.stop("T9")
        assert result.success is True
        assert result.was_running is False

    def test_tmux_stop_nonexistent_succeeds(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        result = a.stop("T9")
        assert result.success is True
        assert result.was_running is False


# ---------------------------------------------------------------------------
# 5. Observe safety
# ---------------------------------------------------------------------------

class TestObserveSafety:

    def test_headless_observe_missing_terminal(self, headless_adapter: HeadlessAdapter) -> None:
        result = headless_adapter.observe("T9")
        assert result.exists is False

    def test_tmux_observe_missing_terminal(self, tmp_path: Path) -> None:
        (tmp_path / "panes.json").write_text("{}")
        a = TmuxAdapter(tmp_path)
        with patch("tmux_adapter._tmux_available", return_value=True):
            result = a.observe("T9")
            assert result.exists is False

    def test_headless_health_missing_terminal(self, headless_adapter: HeadlessAdapter) -> None:
        result = headless_adapter.health("T9")
        assert result.healthy is False


# ---------------------------------------------------------------------------
# 6. Capability declaration matches behavior
# ---------------------------------------------------------------------------

class TestCapabilityDeclarationAccuracy:

    def test_headless_inspect_declared(self, headless_adapter: HeadlessAdapter) -> None:
        """HeadlessAdapter declares INSPECT (partial support)."""
        from tmux_adapter import CAPABILITY_INSPECT
        assert CAPABILITY_INSPECT in headless_adapter.capabilities()

    def test_headless_inspect_returns_result(self, headless_adapter: HeadlessAdapter) -> None:
        result = headless_adapter.inspect("T0")
        assert result.exists is False  # no process tracked

    def test_disabled_tmux_has_no_capabilities(self, tmux_adapter: TmuxAdapter) -> None:
        with patch.dict("os.environ", {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            assert len(tmux_adapter.capabilities()) == 0


# ---------------------------------------------------------------------------
# 7. Tmux remains default adapter
# ---------------------------------------------------------------------------

class TestTmuxDefault:

    def test_tmux_is_default_type(self, tmux_adapter: TmuxAdapter) -> None:
        assert tmux_adapter.adapter_type() == "tmux"

    def test_tmux_has_more_capabilities_than_headless(self) -> None:
        assert len(TMUX_CAPABILITIES) > len(HEADLESS_CAPABILITIES)

    def test_tmux_supports_attach_headless_does_not(self) -> None:
        assert CAPABILITY_ATTACH in TMUX_CAPABILITIES
        assert CAPABILITY_ATTACH not in HEADLESS_CAPABILITIES


# ---------------------------------------------------------------------------
# 8. HeadlessAdapter behavior
# ---------------------------------------------------------------------------

class TestHeadlessBehavior:

    def test_deliver_without_process_fails(self, headless_adapter: HeadlessAdapter) -> None:
        result = headless_adapter.deliver("T0", "20260402-120000-test")
        assert result.success is False
        assert "No active process" in result.failure_reason

    def test_session_health_empty(self, headless_adapter: HeadlessAdapter) -> None:
        result = headless_adapter.session_health([])
        assert result.session_exists is False
        assert result.terminals == {}

    def test_shutdown_is_safe(self, headless_adapter: HeadlessAdapter) -> None:
        headless_adapter.shutdown(graceful=True)
        headless_adapter.shutdown(graceful=False)

    def test_validate_required_on_headless(self, headless_adapter: HeadlessAdapter) -> None:
        missing = validate_required_capabilities(headless_adapter)
        assert missing == [], f"Missing required: {missing}"
