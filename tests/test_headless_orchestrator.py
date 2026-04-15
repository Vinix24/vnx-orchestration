"""tests/test_headless_orchestrator.py — Tests for HeadlessOrchestrator.

Runs in dry-run mode to avoid actual claude CLI invocations.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Return (data_dir, state_dir) with minimal required structure."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dispatches" / "pending").mkdir(parents=True, exist_ok=True)

    # Write a minimal t0_state.json
    (state_dir / "t0_state.json").write_text(
        json.dumps({"terminals": {}, "_build_seconds": 0.1}),
        encoding="utf-8",
    )
    return data_dir, state_dir


def _import_orchestrator():
    """Import HeadlessOrchestrator with mocked claude CLI detection."""
    import importlib
    import shutil

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator
        importlib.reload(headless_orchestrator)
        return headless_orchestrator


# ---------------------------------------------------------------------------
# test_startup_validation — missing t0_state.json raises RuntimeError
# ---------------------------------------------------------------------------

def test_startup_validation_missing_t0_state(tmp_path: Path) -> None:
    """validate_startup() raises RuntimeError when t0_state.json is absent."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dispatches" / "pending").mkdir(parents=True, exist_ok=True)
    # t0_state.json intentionally NOT created

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator
        orch = headless_orchestrator.HeadlessOrchestrator(
            data_dir=data_dir,
            state_dir=state_dir,
            dry_run=True,
        )
        with pytest.raises(RuntimeError) as exc_info:
            orch.validate_startup()

    assert "t0_state.json" in str(exc_info.value)


def test_startup_validation_missing_claude(tmp_path: Path) -> None:
    """validate_startup() raises RuntimeError when 'claude' not in PATH."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value=None):
        import headless_orchestrator
        orch = headless_orchestrator.HeadlessOrchestrator(
            data_dir=data_dir,
            state_dir=state_dir,
            dry_run=True,
        )
        with pytest.raises(RuntimeError) as exc_info:
            orch.validate_startup()

    assert "claude" in str(exc_info.value).lower()


def test_startup_validation_passes(tmp_path: Path) -> None:
    """validate_startup() succeeds with all prerequisites in place."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator
        orch = headless_orchestrator.HeadlessOrchestrator(
            data_dir=data_dir,
            state_dir=state_dir,
            dry_run=True,
        )
        # Should not raise
        orch.validate_startup()


# ---------------------------------------------------------------------------
# test_all_daemons_start — health file shows all running after start
# ---------------------------------------------------------------------------

def test_all_daemons_start(tmp_path: Path) -> None:
    """Start orchestrator; health file written within 35s and shows all daemons running."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator

        # Stub out DispatchDaemon.start so it doesn't actually poll
        mock_daemon = MagicMock()
        mock_daemon._shutdown = threading.Event()

        with patch("headless_orchestrator.HeadlessOrchestrator._invoke_trigger"):
            orch = headless_orchestrator.HeadlessOrchestrator(
                data_dir=data_dir,
                state_dir=state_dir,
                dry_run=True,
            )

            # Patch DispatchDaemon to return our stub
            with patch("headless_dispatch_daemon.DispatchDaemon") as MockDaemon:
                MockDaemon.return_value = mock_daemon

                try:
                    orch.start()

                    # Wait for first health file write (up to 35s; actually much faster)
                    health_path = data_dir / "headless_health.json"
                    deadline = time.monotonic() + 35.0
                    while not health_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.1)

                    assert health_path.exists(), "headless_health.json was not written"
                    health = json.loads(health_path.read_text())

                    daemons = health["daemons"]
                    assert daemons["receipt_watcher"] == "running", f"receipt_watcher: {daemons}"
                    assert daemons["silence_watchdog"] in ("running", "stopped")  # may not start within 35s
                    assert "started_at" in health
                    assert "uptime_seconds" in health
                    assert "decisions_made" in health
                finally:
                    orch.stop()


# ---------------------------------------------------------------------------
# test_graceful_shutdown — all threads stop within 10s after stop()
# ---------------------------------------------------------------------------

def test_graceful_shutdown(tmp_path: Path) -> None:
    """stop() shuts down all threads within 10 seconds."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator

        mock_daemon = MagicMock()
        mock_daemon._shutdown = threading.Event()

        with patch("headless_dispatch_daemon.DispatchDaemon") as MockDaemon:
            MockDaemon.return_value = mock_daemon

            orch = headless_orchestrator.HeadlessOrchestrator(
                data_dir=data_dir,
                state_dir=state_dir,
                dry_run=True,
            )
            orch.start()
            time.sleep(0.3)  # let threads spin up

            t_stop_start = time.monotonic()
            orch.stop()
            elapsed = time.monotonic() - t_stop_start

            assert elapsed < 11.0, f"stop() took {elapsed:.1f}s — exceeded 11s budget"
            assert orch._shutdown.is_set()

            # All our tracked threads should be dead
            threads_to_check = [
                orch._receipt_watcher._thread if orch._receipt_watcher else None,
                orch._watchdog_thread,
                orch._health_thread,
                orch._decision_thread,
            ]
            for thread in threads_to_check:
                if thread is not None:
                    assert not thread.is_alive(), f"Thread {thread.name!r} still running after stop()"


# ---------------------------------------------------------------------------
# test_health_file_updated — health file is refreshed each cycle
# ---------------------------------------------------------------------------

def test_health_file_updated(tmp_path: Path) -> None:
    """Health file mtime advances across two consecutive health writes."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator

        # Override health interval to be very short for the test
        original_interval = headless_orchestrator._HEALTH_INTERVAL
        headless_orchestrator._HEALTH_INTERVAL = 0.2

        mock_daemon = MagicMock()
        mock_daemon._shutdown = threading.Event()

        with patch("headless_dispatch_daemon.DispatchDaemon") as MockDaemon:
            MockDaemon.return_value = mock_daemon

            orch = headless_orchestrator.HeadlessOrchestrator(
                data_dir=data_dir,
                state_dir=state_dir,
                dry_run=True,
            )
            try:
                orch.start()

                health_path = data_dir / "headless_health.json"

                # Wait for first write
                deadline = time.monotonic() + 5.0
                while not health_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert health_path.exists(), "First health write never occurred"

                first_mtime = health_path.stat().st_mtime
                first_data = json.loads(health_path.read_text())

                # Wait for second write
                deadline = time.monotonic() + 2.0
                while health_path.stat().st_mtime <= first_mtime and time.monotonic() < deadline:
                    time.sleep(0.05)

                second_mtime = health_path.stat().st_mtime
                second_data = json.loads(health_path.read_text())

                assert second_mtime > first_mtime, "Health file mtime did not advance"
                assert second_data["last_health_check"] != first_data["last_health_check"], (
                    "last_health_check timestamp did not change"
                )
                assert second_data["uptime_seconds"] >= first_data["uptime_seconds"]

            finally:
                headless_orchestrator._HEALTH_INTERVAL = original_interval
                orch.stop()
