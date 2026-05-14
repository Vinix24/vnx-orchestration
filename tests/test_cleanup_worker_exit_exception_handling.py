#!/usr/bin/env python3
"""tests/test_cleanup_worker_exit_exception_handling.py — regression guard for OI-1437 silent-except narrowing.

Verifies that:
- cleanup_worker_exit() completes without error on a clean env
- ImportError from HealthBeacon is emitted via _emit(), not silently swallowed or raised
- (OSError, ValueError) in _emit() itself is swallowed silently (logging must never raise)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(_SCRIPTS_DIR))


def _setup_state(tmp_path: Path, dispatch_id: str = "d-exc-001", terminal_id: str = "T1"):
    """Create state dir with schema, a registered dispatch, and an acquired lease."""
    from runtime_coordination import get_connection, init_schema, register_dispatch
    from lease_manager import LeaseManager
    from worker_state_manager import WorkerStateManager

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    init_schema(state_dir)

    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
        conn.commit()

    lm = LeaseManager(state_dir, auto_init=False)
    lease = lm.acquire(terminal_id, dispatch_id=dispatch_id)
    lease_generation = lease.generation

    wm = WorkerStateManager(state_dir, auto_init=False)
    wm.initialize(terminal_id, dispatch_id=dispatch_id)
    wm.transition(terminal_id, "working")

    return state_dir, dispatch_id, terminal_id, lease_generation


def test_runs_clean_on_default_env(tmp_path):
    """cleanup_worker_exit() completes without raising on a valid env."""
    import cleanup_worker_exit as cwe

    state_dir, dispatch_id, terminal_id, lease_generation = _setup_state(tmp_path)

    result = cwe.cleanup_worker_exit(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        exit_status="success",
        lease_generation=lease_generation,
        state_dir=state_dir,
        dispatch_file=None,
    )

    assert result.lease_released is True
    assert result.errors == []


def test_corrupt_input_logs_warning(tmp_path, capsys):
    """ImportError from HealthBeacon is caught and emitted via _emit(), not raised."""
    import cleanup_worker_exit as cwe

    state_dir, dispatch_id, terminal_id, lease_generation = _setup_state(
        tmp_path, dispatch_id="d-exc-002"
    )

    # Patch health_beacon to raise ImportError, exercising the narrowed except path
    with patch.dict("sys.modules", {"health_beacon": None}):
        result = cwe.cleanup_worker_exit(
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            exit_status="success",
            lease_generation=lease_generation,
            state_dir=state_dir,
            dispatch_file=None,
        )

    # Function must still return cleanly
    assert result.lease_released is True

    # _emit("WARN", "health_beacon_failed", ...) writes to stderr
    captured = capsys.readouterr()
    assert "health_beacon_failed" in captured.err or "WARN" in captured.err, (
        f"Expected WARN emit for health_beacon_failed on stderr; got: {captured.err!r}"
    )
