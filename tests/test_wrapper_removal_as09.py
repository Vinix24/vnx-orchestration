#!/usr/bin/env python3
"""Static checks ensuring wrapper removal for AS-09."""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"


def test_heartbeat_ack_monitor_wrapper_removed():
    """AS-09 removed the wrapper indirection. The dispatch_ack_watcher.sh shim was
    itself later deleted (cb174793), so the durable invariant is simply that the
    wrapper module no longer exists and the monitor it wrapped still does."""
    assert not (SCRIPTS_DIR / "heartbeat_ack_monitor_wrapper.py").exists(), (
        "heartbeat_ack_monitor_wrapper.py should be gone (AS-09 wrapper removal)"
    )
    assert (SCRIPTS_DIR / "heartbeat_ack_monitor.py").exists(), (
        "heartbeat_ack_monitor.py (the direct target) should exist"
    )


def test_pr_queue_completion_attempt_inlines_event_logging():
    content = (SCRIPTS_DIR / "pr_queue_manager.py").read_text(encoding="utf-8")
    assert "def log_completion_attempt" not in content
    assert "completion_attempt" in content
    assert "log_queue_event" in content
