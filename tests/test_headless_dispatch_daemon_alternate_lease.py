#!/usr/bin/env python3
"""Regression tests: alternate-terminal lease management on dispatch reroute.

Codex round-1 finding: when _deliver() rerouted a dispatch to an alternate terminal,
it never acquired/released a lease for that alternate. Audit and release still targeted
the original terminal. Concurrent dispatches could share one worker on the alternate,
corrupting cross-dispatch session/event state.

Fix: _deliver() now acquires the alternate's lease BEFORE releasing the original,
and returns (success, effective_terminal, effective_generation) so _handle() releases
the correct lease and writes an accurate audit record.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import headless_dispatch_daemon as hdd
from headless_dispatch_daemon import DispatchMeta, _deliver
from provider_adapter import Capability


def _minimal_meta(dispatch_id: str = "d-reroute-test", terminal: str = "T1") -> DispatchMeta:
    return DispatchMeta(
        dispatch_id=dispatch_id,
        target_terminal=terminal,
        track="A",
        role="backend-developer",
        gate="f58-pr3",
        raw_instruction=f"[[TARGET:{terminal}]]\ndo work",
        pr_id=None,
    )


class TestAlternateLease:
    """_deliver must acquire alt lease before releasing original on reroute."""

    def test_reroute_acquires_alt_lease_before_releasing_original(self, tmp_path):
        """Lease on alt terminal must be acquired before original is released."""
        meta = _minimal_meta(terminal="T1")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        lease_events: list = []

        mock_result = MagicMock()
        mock_result.status = "done"

        mock_t1 = MagicMock()
        mock_t1.capabilities.return_value = set()  # lacks required caps → reroute
        mock_t1.name.return_value = "T1"

        mock_t2 = MagicMock()
        mock_t2.execute.return_value = mock_result
        mock_t2.capabilities.return_value = {"code"}

        def mock_resolve(terminal_id: str):
            return mock_t1 if terminal_id == "T1" else mock_t2

        mock_adapters = MagicMock()
        mock_adapters.resolve_adapter = mock_resolve

        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch.dict("sys.modules", {"adapters": mock_adapters}),
            patch.object(hdd, "_classify_dispatch", return_value={Capability.CODE}),
            patch.object(hdd, "_find_capable_terminal", return_value="T2"),
            patch.object(
                hdd, "_acquire_lease",
                side_effect=lambda t, d: lease_events.append(("acquire", t)) or 42,
            ),
            patch.object(
                hdd, "_release_lease",
                side_effect=lambda t, g: lease_events.append(("release", t, g)) or True,
            ),
        ):
            success, eff_terminal, eff_generation = _deliver(
                meta, tmp_path / "active.md", state_dir,
                original_terminal="T1",
                original_generation=1,
            )

        assert success is True
        assert eff_terminal == "T2", f"effective terminal must be alt, got {eff_terminal!r}"
        assert eff_generation == 42, f"effective generation must be alt's, got {eff_generation!r}"

        # Acquire T2 must come before release T1
        acquire_t2_idx = next(
            (i for i, e in enumerate(lease_events) if e == ("acquire", "T2")), None
        )
        release_t1_idx = next(
            (i for i, e in enumerate(lease_events) if e[0] == "release" and e[1] == "T1"), None
        )
        assert acquire_t2_idx is not None, f"T2 lease was never acquired: {lease_events}"
        assert release_t1_idx is not None, f"T1 lease was never released: {lease_events}"
        assert acquire_t2_idx < release_t1_idx, (
            f"T2 lease must be acquired BEFORE T1 is released; events: {lease_events}"
        )

    def test_reroute_without_generation_skips_lease_swap(self, tmp_path):
        """When no original_generation is passed (legacy callers), no lease swap occurs."""
        meta = _minimal_meta(terminal="T1")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        lease_events: list = []

        mock_result = MagicMock()
        mock_result.status = "done"

        mock_t1 = MagicMock()
        mock_t1.capabilities.return_value = set()
        mock_t1.name.return_value = "T1"

        mock_t2 = MagicMock()
        mock_t2.execute.return_value = mock_result

        def mock_resolve(terminal_id: str):
            return mock_t1 if terminal_id == "T1" else mock_t2

        mock_adapters = MagicMock()
        mock_adapters.resolve_adapter = mock_resolve

        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch.dict("sys.modules", {"adapters": mock_adapters}),
            patch.object(hdd, "_classify_dispatch", return_value={Capability.CODE}),
            patch.object(hdd, "_find_capable_terminal", return_value="T2"),
            patch.object(
                hdd, "_acquire_lease",
                side_effect=lambda t, d: lease_events.append(("acquire", t)) or 99,
            ),
            patch.object(
                hdd, "_release_lease",
                side_effect=lambda t, g: lease_events.append(("release", t, g)) or True,
            ),
        ):
            # Legacy call without generation — no lease swap should occur
            result = _deliver(meta, tmp_path / "active.md", state_dir)

        success, eff_terminal, eff_generation = result
        assert success is True
        # No lease operations performed inside _deliver when original_generation is None
        assert lease_events == [], f"Expected no lease events but got: {lease_events}"

    def test_alt_lease_failure_aborts_reroute(self, tmp_path):
        """If alt lease acquisition fails, _deliver returns False without doing work."""
        meta = _minimal_meta(terminal="T1")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        release_events: list = []
        work_done = []

        mock_result = MagicMock()
        mock_result.status = "done"

        mock_t1 = MagicMock()
        mock_t1.capabilities.return_value = set()
        mock_t1.name.return_value = "T1"

        mock_t2 = MagicMock()
        mock_t2.execute.side_effect = lambda *a, **kw: work_done.append(True) or mock_result

        def mock_resolve(terminal_id: str):
            return mock_t1 if terminal_id == "T1" else mock_t2

        mock_adapters = MagicMock()
        mock_adapters.resolve_adapter = mock_resolve

        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch.dict("sys.modules", {"adapters": mock_adapters}),
            patch.object(hdd, "_classify_dispatch", return_value={Capability.CODE}),
            patch.object(hdd, "_find_capable_terminal", return_value="T2"),
            patch.object(hdd, "_acquire_lease", return_value=None),  # alt lease unavailable
            patch.object(
                hdd, "_release_lease",
                side_effect=lambda t, g: release_events.append((t, g)) or True,
            ),
        ):
            success, eff_terminal, eff_generation = _deliver(
                meta, tmp_path / "active.md", state_dir,
                original_terminal="T1",
                original_generation=1,
            )

        assert success is False, "Should fail when alt lease cannot be acquired"
        assert work_done == [], "No work must be done on alt when its lease can't be acquired"
        # Original lease must NOT have been released inside _deliver (caller handles it)
        assert release_events == [], f"Original lease released prematurely: {release_events}"
        # Effective terminal stays original (caller still holds that lease)
        assert eff_terminal == "T1"
        assert eff_generation == 1

    def test_two_rerouted_dispatches_serialize_on_alternate(self, tmp_path):
        """Second dispatch rerouting to T2 must fail to acquire lease, not share worker."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        meta1 = _minimal_meta(dispatch_id="d-reroute-1", terminal="T1")
        meta2 = _minimal_meta(dispatch_id="d-reroute-2", terminal="T1")

        work_done: list = []
        # First call to _acquire_lease("T2", ...) returns 42; second returns None (contention)
        acquire_call_count = {"n": 0}

        def mock_acquire(terminal_id: str, dispatch_id: str):
            if terminal_id == "T2":
                acquire_call_count["n"] += 1
                return 42 if acquire_call_count["n"] == 1 else None
            return None

        mock_result = MagicMock()
        mock_result.status = "done"

        mock_t1 = MagicMock()
        mock_t1.capabilities.return_value = set()
        mock_t1.name.return_value = "T1"

        mock_t2 = MagicMock()
        mock_t2.execute.side_effect = lambda *a, **kw: work_done.append(True) or mock_result

        def mock_resolve(terminal_id: str):
            return mock_t1 if terminal_id == "T1" else mock_t2

        mock_adapters = MagicMock()
        mock_adapters.resolve_adapter = mock_resolve

        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch.dict("sys.modules", {"adapters": mock_adapters}),
            patch.object(hdd, "_classify_dispatch", return_value={Capability.CODE}),
            patch.object(hdd, "_find_capable_terminal", return_value="T2"),
            patch.object(hdd, "_acquire_lease", side_effect=mock_acquire),
            patch.object(hdd, "_release_lease", return_value=True),
        ):
            r1 = _deliver(
                meta1, tmp_path / "d1.md", state_dir,
                original_terminal="T1", original_generation=10,
            )
            r2 = _deliver(
                meta2, tmp_path / "d2.md", state_dir,
                original_terminal="T1", original_generation=11,
            )

        success1, eff1, _ = r1
        success2, eff2, _ = r2

        assert success1 is True, "First dispatch must succeed"
        assert eff1 == "T2", "First dispatch must run on T2"
        assert success2 is False, "Second dispatch must fail — T2 lease unavailable (serialized)"
        assert len(work_done) == 1, f"Only one dispatch should execute work, got {len(work_done)}"
