#!/usr/bin/env python3
"""PR-4 certification tests for Feature 16: Runtime Adapter Formalization.

Certifies that:
  1. Adapter-backed parity: launch/attach/stop/inspect all route through adapter
  2. Canonical runtime truth: adapter-backed state reporting aligns with DB
  3. No new direct tmux coupling in protected path
  4. Headless abstraction is explicit and bounded
  5. Contract-to-implementation alignment
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set
from unittest.mock import MagicMock, patch

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
    REQUIRED_CAPABILITIES,
    AttachResult,
    DeliveryResult,
    HealthResult,
    InspectionResult,
    ObservationResult,
    RehealResult,
    RuntimeAdapter,
    SessionHealthResult,
    SpawnResult,
    StopResult,
    UnsupportedCapability,
    validate_required_capabilities,
)
from headless_transport_adapter import HEADLESS_CAPABILITIES, HeadlessAdapter
from tmux_adapter import (
    TMUX_CAPABILITIES,
    TmuxAdapter,
)

LIB_DIR = Path(__file__).parent.parent / "scripts" / "lib"


def _get_imported_names(filepath: Path) -> Set[str]:
    """Parse a Python file's AST and return all imported names."""
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return names


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmux_adapter(tmp_path: Path) -> TmuxAdapter:
    panes = {
        "session": "vnx-test",
        "T0": {"pane_id": "%0", "provider": "claude_code", "role": "orchestrator",
               "work_dir": str(tmp_path / "T0")},
        "T1": {"pane_id": "%1", "provider": "claude_code", "track": "A",
               "work_dir": str(tmp_path / "T1")},
        "T2": {"pane_id": "%2", "provider": "claude_code", "track": "B",
               "work_dir": str(tmp_path / "T2")},
        "T3": {"pane_id": "%3", "provider": "claude_code", "track": "C",
               "work_dir": str(tmp_path / "T3")},
        "tracks": {
            "A": {"pane_id": "%1", "track": "A"},
            "B": {"pane_id": "%2", "track": "B"},
            "C": {"pane_id": "%3", "track": "C"},
        },
    }
    (tmp_path / "panes.json").write_text(__import__("json").dumps(panes))
    return TmuxAdapter(str(tmp_path))


@pytest.fixture()
def headless_adapter() -> HeadlessAdapter:
    return HeadlessAdapter()


# ===================================================================
# Section 1: Adapter-Backed Parity
# ===================================================================

class TestAdapterBackedParity:
    """Certify that launch, attach, stop, and inspect route through adapter."""

    def test_tmux_adapter_has_all_contract_operations(self) -> None:
        """Every contract operation exists as a method on TmuxAdapter."""
        required_methods = [
            "spawn", "stop", "deliver", "attach", "observe",
            "inspect", "health", "session_health", "reheal",
            "adapter_type", "capabilities", "shutdown",
        ]
        for method in required_methods:
            assert hasattr(TmuxAdapter, method), f"TmuxAdapter missing {method}"
            assert callable(getattr(TmuxAdapter, method)), f"TmuxAdapter.{method} not callable"

    def test_spawn_returns_spawn_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.spawn("T1", {"command": "echo test", "work_dir": "/tmp"})
        assert isinstance(result, SpawnResult)

    def test_stop_returns_stop_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.stop("T1")
        assert isinstance(result, StopResult)

    def test_attach_returns_attach_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.attach("T1")
        assert isinstance(result, AttachResult)

    def test_observe_returns_observation_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.observe("T1")
        assert isinstance(result, ObservationResult)

    def test_inspect_returns_inspection_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.inspect("T1")
        assert isinstance(result, InspectionResult)

    def test_health_returns_health_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.health("T1")
        assert isinstance(result, HealthResult)

    def test_session_health_returns_session_health_result(
        self, tmux_adapter: TmuxAdapter
    ) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.session_health(["T1", "T2", "T3"])
        assert isinstance(result, SessionHealthResult)

    def test_reheal_returns_reheal_result(self, tmux_adapter: TmuxAdapter) -> None:
        with patch("tmux_adapter.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = tmux_adapter.reheal("T1")
        assert isinstance(result, RehealResult)


# ===================================================================
# Section 2: Canonical Runtime Truth Alignment
# ===================================================================

class TestCanonicalTruthAlignment:
    """Certify adapter does not write canonical state."""

    def test_adapter_does_not_import_lease_write_functions(self) -> None:
        """TmuxAdapter must not import lease mutation functions (AST-checked)."""
        import tmux_adapter as mod
        imported = _get_imported_names(Path(mod.__file__))
        lease_mutators = {"acquire_lease", "release_lease", "renew_lease", "expire_lease"}
        found = imported & lease_mutators
        assert found == set(), f"tmux_adapter.py imports lease mutators: {found}"

    def test_adapter_does_not_import_dispatch_transition(self) -> None:
        """TmuxAdapter must not import dispatch state transition (AST-checked)."""
        import tmux_adapter as mod
        imported = _get_imported_names(Path(mod.__file__))
        assert "transition_dispatch" not in imported, \
            "tmux_adapter.py imports transition_dispatch"

    def test_headless_adapter_does_not_import_coordination_db(self) -> None:
        """HeadlessAdapter must not import runtime_coordination (AST-checked)."""
        import headless_transport_adapter as mod
        imported = _get_imported_names(Path(mod.__file__))
        assert "runtime_coordination" not in imported, \
            "headless_transport_adapter.py imports runtime_coordination"

    def test_facade_does_not_mutate_canonical_state(self) -> None:
        """RuntimeFacade must not import lease or dispatch mutators (AST-checked)."""
        import runtime_facade as mod
        imported = _get_imported_names(Path(mod.__file__))
        forbidden = {"acquire_lease", "release_lease", "transition_dispatch",
                      "expire_lease", "recover_lease"}
        found = imported & forbidden
        assert found == set(), f"runtime_facade.py imports mutators: {found}"


# ===================================================================
# Section 3: No New Direct Tmux Coupling
# ===================================================================

class TestDirectCouplingFreeze:
    """Certify no scripts/lib/ module (except adapter files) calls tmux directly."""

    ADAPTER_FILES = {"tmux_adapter.py", "tmux_session_profile.py"}

    def test_no_direct_tmux_subprocess_in_protected_modules(self) -> None:
        violations: list[str] = []
        for py_file in sorted(LIB_DIR.glob("*.py")):
            if py_file.name in self.ADAPTER_FILES:
                continue
            content = py_file.read_text(encoding="utf-8")
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if "subprocess" in line and "tmux" in line:
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
        # Known pre-existing violations (predating Feature 16):
        known_preexisting = {
            "dashboard_actions.py",
            "terminal_snapshot.py",
            "terminal_state_reconciler.py",
        }
        new_violations = [
            v for v in violations
            if not any(v.startswith(k) for k in known_preexisting)
        ]
        assert new_violations == [], (
            f"NEW direct tmux coupling in protected path:\n"
            + "\n".join(new_violations)
        )

    def test_count_preexisting_violations_stable(self) -> None:
        """Pre-existing violations count must not increase."""
        known_files = {
            "dashboard_actions.py",
            "terminal_snapshot.py",
            "terminal_state_reconciler.py",
        }
        found: set[str] = set()
        for py_file in sorted(LIB_DIR.glob("*.py")):
            if py_file.name in self.ADAPTER_FILES:
                continue
            content = py_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if "subprocess" in line and "tmux" in line:
                    found.add(py_file.name)
                    break
        assert found <= known_files, (
            f"New files with direct tmux coupling: {found - known_files}"
        )


# ===================================================================
# Section 4: Headless Abstraction Explicit And Bounded
# ===================================================================

class TestHeadlessAbstractionBounds:
    """Certify headless adapter is explicit, bounded, and not pretending production."""

    def test_headless_conforms_to_protocol(self) -> None:
        assert isinstance(HeadlessAdapter(), RuntimeAdapter)

    def test_headless_has_required_capabilities(self, headless_adapter: HeadlessAdapter) -> None:
        missing = validate_required_capabilities(headless_adapter)
        assert missing == [], f"HeadlessAdapter missing required capabilities: {missing}"

    def test_headless_attach_raises_unsupported(self, headless_adapter: HeadlessAdapter) -> None:
        with pytest.raises(UnsupportedCapability) as exc_info:
            headless_adapter.attach("T1")
        assert exc_info.value.operation == "ATTACH"

    def test_headless_reheal_raises_unsupported(self, headless_adapter: HeadlessAdapter) -> None:
        with pytest.raises(UnsupportedCapability) as exc_info:
            headless_adapter.reheal("T1")
        assert exc_info.value.operation == "REHEAL"

    def test_headless_capabilities_exclude_attach_and_reheal(
        self, headless_adapter: HeadlessAdapter
    ) -> None:
        caps = headless_adapter.capabilities()
        assert CAPABILITY_ATTACH not in caps
        assert CAPABILITY_REHEAL not in caps

    def test_headless_does_not_import_tmux(self) -> None:
        import headless_transport_adapter as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        # Should not have subprocess tmux calls
        assert "\"tmux\"" not in source and "'tmux'" not in source, \
            "HeadlessAdapter references tmux directly"


# ===================================================================
# Section 5: Contract-to-Implementation Alignment
# ===================================================================

class TestContractAlignment:
    """Certify implementation matches docs/RUNTIME_ADAPTER_CONTRACT.md."""

    def test_protocol_has_all_contract_methods(self) -> None:
        """RuntimeAdapter protocol defines all 12 contract methods."""
        contract_methods = {
            "adapter_type", "capabilities", "spawn", "stop", "deliver",
            "attach", "observe", "inspect", "health", "session_health",
            "reheal", "shutdown",
        }
        protocol_methods = {
            name for name in dir(RuntimeAdapter)
            if not name.startswith("_") and callable(getattr(RuntimeAdapter, name, None))
        }
        # Protocol methods include inherited; filter to known contract methods
        missing = contract_methods - protocol_methods
        assert missing == set(), f"Protocol missing contract methods: {missing}"

    def test_required_capabilities_match_contract(self) -> None:
        """Required capabilities match Section 5.2 of contract."""
        expected = {
            CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER,
            CAPABILITY_OBSERVE, CAPABILITY_HEALTH, CAPABILITY_SESSION_HEALTH,
        }
        assert REQUIRED_CAPABILITIES == expected

    def test_tmux_adapter_declares_all_9_capabilities(self) -> None:
        """TmuxAdapter supports all 9 contract capabilities."""
        assert len(TMUX_CAPABILITIES) == 9
        assert REQUIRED_CAPABILITIES <= TMUX_CAPABILITIES

    def test_headless_adapter_declares_7_capabilities(self) -> None:
        """HeadlessAdapter supports 7 capabilities (no ATTACH, REHEAL)."""
        assert len(HEADLESS_CAPABILITIES) == 7
        assert REQUIRED_CAPABILITIES <= HEADLESS_CAPABILITIES
        assert CAPABILITY_ATTACH not in HEADLESS_CAPABILITIES
        assert CAPABILITY_REHEAL not in HEADLESS_CAPABILITIES

    def test_session_health_accepts_terminal_ids(self) -> None:
        """session_health() accepts terminal_ids parameter per OI-561 fix."""
        import inspect
        sig = inspect.signature(TmuxAdapter.session_health)
        params = list(sig.parameters.keys())
        assert "terminal_ids" in params, \
            "session_health() must accept terminal_ids parameter"

    def test_deliver_has_no_lease_validation(self) -> None:
        """deliver() body must not call validate_lease or query lease state."""
        source = inspect.getsource(TmuxAdapter.deliver)
        forbidden = ["validate_lease", "check_lease", "get_lease", "lease_state"]
        violations = [fn for fn in forbidden if fn in source]
        assert violations == [], (
            f"deliver() body references lease functions: {violations}"
        )
        # Also check private delivery helpers
        for helper_name in ("_deliver_primary", "_deliver_legacy", "_handle_pane_not_found"):
            helper = getattr(TmuxAdapter, helper_name, None)
            if helper:
                helper_src = inspect.getsource(helper)
                helper_violations = [fn for fn in forbidden if fn in helper_src]
                assert helper_violations == [], (
                    f"{helper_name} references lease functions: {helper_violations}"
                )


# ===================================================================
# Section 6: Feature Flag Preservation
# ===================================================================

class TestFeatureFlagPreservation:
    """Certify feature flags from contract Section 7.2 are respected."""

    def test_adapter_disabled_returns_empty_capabilities(self, tmp_path: Path) -> None:
        panes = {"session": "test", "T0": {"pane_id": "%0"}}
        (tmp_path / "panes.json").write_text(__import__("json").dumps(panes))
        with patch.dict("os.environ", {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            adapter = TmuxAdapter(str(tmp_path))
            caps = adapter.capabilities()
        assert caps == frozenset()

    def test_adapter_enabled_returns_full_capabilities(self, tmp_path: Path) -> None:
        panes = {"session": "test", "T0": {"pane_id": "%0"}}
        (tmp_path / "panes.json").write_text(__import__("json").dumps(panes))
        with patch.dict("os.environ", {"VNX_TMUX_ADAPTER_ENABLED": "1"}):
            adapter = TmuxAdapter(str(tmp_path))
            caps = adapter.capabilities()
        assert caps == TMUX_CAPABILITIES
