#!/usr/bin/env python3
"""Regression tests for OI-1104: heartbeat_ack_monitor._monitor_dispatch()
returns immediately for subprocess-adapter terminals without removing the
dispatch_id from active_dispatches / polling_threads, leaking dispatch state.

Fix: pop both structures before the early return.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

_UNSET = object()


@contextlib.contextmanager
def _stubbed_heartbeat_ack_monitor_deps():
    """Make append_receipt/python_singleton importable with the attributes
    heartbeat_ack_monitor's module-level `from X import Y` needs, then
    restore sys.modules to exactly its prior state on exit.

    OI-1403: the previous version used `sys.modules.setdefault(...)` and
    then unconditionally overwrote `AppendReceiptError`/`append_receipt_payload`
    on whatever ended up in sys.modules. In a full suite, append_receipt is
    almost always already the REAL, shared module by the time this test file
    runs — setdefault() is a no-op, and the two lines after it stomped the
    real module's attributes with mocks, process-wide, with no teardown.
    Every later test that goes through append_receipt.append_receipt_payload
    (e.g. report_to_receipt_converter.py, which re-imports it via
    sys.modules) then silently got a mock returning None instead of the
    real function. Restoring here — unconditionally, on every exit path —
    is only needed for the FIRST import of heartbeat_ack_monitor in a
    session: once `from append_receipt import ...` has executed once, the
    names are bound into heartbeat_ack_monitor's own namespace and further
    mutation of sys.modules["append_receipt"] has no effect on it.
    """
    injected = []
    saved_attrs = []

    for name in ("append_receipt", "python_singleton"):
        if name not in sys.modules:
            sys.modules[name] = MagicMock()
            injected.append(name)

    mod_ar = sys.modules["append_receipt"]
    mod_ps = sys.modules["python_singleton"]

    for mod, attr, value in (
        (mod_ar, "AppendReceiptError", Exception),
        (mod_ar, "append_receipt_payload", MagicMock(return_value=None)),
        (mod_ps, "enforce_python_singleton", MagicMock(return_value=None)),
    ):
        saved_attrs.append((mod, attr, getattr(mod, attr, _UNSET)))
        setattr(mod, attr, value)

    try:
        yield
    finally:
        for mod, attr, original in saved_attrs:
            if original is _UNSET:
                delattr(mod, attr)
            else:
                setattr(mod, attr, original)
        for name in injected:
            sys.modules.pop(name, None)


def _build_monitor():
    """Return a HeartbeatACKMonitor with all I/O stubbed out.

    Uses __new__ to skip __init__ entirely, then manually sets the minimal
    attributes exercised by _monitor_dispatch and _is_subprocess_terminal.
    """
    # Import the class — its module-level `from append_receipt import ...`
    # runs at import time, so we stub it in sys.modules before importing.
    with _stubbed_heartbeat_ack_monitor_deps():
        from heartbeat_ack_monitor import HeartbeatACKMonitor

    monitor = HeartbeatACKMonitor.__new__(HeartbeatACKMonitor)
    monitor.active_dispatches = {}
    monitor.terminal_heartbeats = {}
    monitor.log_checksums = {}
    monitor.polling_threads = {}
    monitor.heartbeat_poll_interval = 2
    monitor.confirmation_threshold = 3
    monitor.timeout_seconds = 60
    monitor.dispatch_lease_seconds = 600
    monitor._shadow_terminal_state_enabled = False
    monitor._terminal_state_update = None
    monitor._terminal_update_type = None
    monitor._default_lease_expires = None
    monitor.receipts_file = "/dev/null"
    monitor.is_shadow_mode = False
    monitor.dashboard_file = "/dev/null"
    monitor.terminal_status_file = "/dev/null"
    monitor.terminal_logs = {}
    monitor.state_dir = "/tmp"
    return monitor


def _dispatch_info(dispatch_id: str, terminal: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "dispatch_id": dispatch_id,
        "task_id": "t-001",
        "pr_id": "",
        "terminal": terminal,
        "sent_time": now,
        "timeout_time": now + timedelta(seconds=60),
        "confirmed": False,
        "confirmation_time": None,
        "confirmation_method": None,
        "signals_detected": [],
    }


class TestMonitorDispatchSubprocessCleanup(unittest.TestCase):
    """OI-1104 regression: subprocess-adapter early return must remove
    dispatch_id from active_dispatches and polling_threads."""

    def test_subprocess_terminal_clears_active_dispatches(self):
        """After early return, active_dispatches must not contain the dispatch_id."""
        monitor = _build_monitor()
        dispatch_id = "d-sub-001"
        terminal = "T1"

        monitor.active_dispatches[dispatch_id] = _dispatch_info(dispatch_id, terminal)
        monitor.polling_threads[dispatch_id] = threading.current_thread()

        with patch.dict(os.environ, {"VNX_ADAPTER_T1": "subprocess"}):
            monitor._monitor_dispatch(dispatch_id)

        self.assertNotIn(dispatch_id, monitor.active_dispatches,
                         "dispatch_id must be removed from active_dispatches on subprocess early return")

    def test_subprocess_terminal_clears_polling_threads(self):
        """After early return, polling_threads must not contain the dispatch_id."""
        monitor = _build_monitor()
        dispatch_id = "d-sub-002"
        terminal = "T2"

        monitor.active_dispatches[dispatch_id] = _dispatch_info(dispatch_id, terminal)
        monitor.polling_threads[dispatch_id] = threading.current_thread()

        with patch.dict(os.environ, {"VNX_ADAPTER_T2": "subprocess"}):
            monitor._monitor_dispatch(dispatch_id)

        self.assertNotIn(dispatch_id, monitor.polling_threads,
                         "dispatch_id must be removed from polling_threads on subprocess early return")

    def test_tmux_terminal_is_not_subprocess(self):
        """Non-subprocess terminals must not trigger the early-return path."""
        monitor = _build_monitor()
        env_without_t1 = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T1"}
        with patch.dict(os.environ, env_without_t1, clear=True):
            self.assertFalse(monitor._is_subprocess_terminal("T1"))

    def test_missing_dispatch_id_returns_immediately(self):
        """_monitor_dispatch on an unknown dispatch_id must return without error."""
        monitor = _build_monitor()
        monitor._monitor_dispatch("d-nonexistent-999")  # must not raise

    def test_subprocess_cleanup_idempotent_when_polling_threads_absent(self):
        """pop() on already-absent polling_threads key must not raise (idempotent)."""
        monitor = _build_monitor()
        dispatch_id = "d-sub-003"
        terminal = "T3"

        # Only populate active_dispatches, NOT polling_threads
        monitor.active_dispatches[dispatch_id] = _dispatch_info(dispatch_id, terminal)
        # polling_threads[dispatch_id] intentionally absent

        with patch.dict(os.environ, {"VNX_ADAPTER_T3": "subprocess"}):
            monitor._monitor_dispatch(dispatch_id)  # must not raise KeyError

        self.assertNotIn(dispatch_id, monitor.active_dispatches)
        self.assertNotIn(dispatch_id, monitor.polling_threads)


if __name__ == "__main__":
    unittest.main()
