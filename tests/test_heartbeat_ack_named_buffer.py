#!/usr/bin/env python3
"""OI-1144 regression: heartbeat_ack_monitor._notify_t0_ack must use a NAMED
tmux buffer (pid + dispatch_id) and delete it afterwards, so two simultaneous
ACKs can't cross on tmux's anonymous "most recent buffer" stack.

This asserts the argv built for ``tmux load-buffer`` / ``paste-buffer`` /
``delete-buffer`` (a unique ``-b <name>`` shared by load+paste and removed by
delete), not a live two-process tmux race.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


def _make_monitor():
    from heartbeat_ack_monitor import HeartbeatACKMonitor

    monitor = object.__new__(HeartbeatACKMonitor)
    monitor._get_t0_pane_id = lambda: "%0"
    return monitor


def _run_notify(monitor, dispatch_id):
    """Run _notify_t0_ack once, returning the recorded tmux argv list."""
    import heartbeat_ack_monitor as ham

    calls = []

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    with patch.object(ham.subprocess, "run", side_effect=fake_run), patch.object(
        ham.time, "sleep"
    ):
        monitor._notify_t0_ack(
            {"terminal": "T1", "dispatch_id": dispatch_id},
            signals=[],
        )
    return calls


def _name(cmd):
    assert "-b" in cmd, f"expected -b named-buffer flag in {cmd}"
    return cmd[cmd.index("-b") + 1]


def test_notify_t0_ack_uses_named_buffer_and_deletes_it():
    monitor = _make_monitor()
    calls = _run_notify(monitor, "dispatch-A")

    loads = [c for c in calls if c[1] == "load-buffer"]
    pastes = [c for c in calls if c[1] == "paste-buffer"]
    deletes = [c for c in calls if c[1] == "delete-buffer"]
    assert len(loads) == 1
    assert len(pastes) == 1
    assert len(deletes) == 1
    # Load, paste and delete must address the SAME named buffer.
    assert _name(loads[0]) == _name(pastes[0]) == _name(deletes[0])
    # The sanitized dispatch_id is folded into the name, so two dispatches
    # cannot collide even within one process.
    assert "dispatch_A" in _name(loads[0])


def test_two_acks_build_distinct_buffer_names():
    monitor = _make_monitor()
    names = []
    for dispatch_id in ("dispatch-A", "dispatch-B"):
        calls = _run_notify(monitor, dispatch_id)
        loads = [c for c in calls if c[1] == "load-buffer"]
        names.append(_name(loads[0]))
    assert names[0] != names[1]
