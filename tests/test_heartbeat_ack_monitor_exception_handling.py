#!/usr/bin/env python3
"""Exception-handling regression tests for heartbeat_ack_monitor.py (OI-1437).

Covers two narrowed sites:
- line 153: ValueError from datetime.fromisoformat on corrupt timestamp
- line 417: OSError from os.path.getmtime on inaccessible log file
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


def _make_monitor():
    """Create HeartbeatACKMonitor bypassing env-dependent __init__."""
    from heartbeat_ack_monitor import HeartbeatACKMonitor
    monitor = object.__new__(HeartbeatACKMonitor)
    monitor.terminal_heartbeats = {}
    monitor.terminal_logs = {}
    monitor.dashboard_file = "/nonexistent/dashboard.json"
    return monitor


def test_runs_clean_on_default_env():
    """_initialize_heartbeats with no dashboard file completes without raising."""
    monitor = _make_monitor()
    monitor._initialize_heartbeats()
    assert monitor.terminal_heartbeats == {}


def test_corrupt_timestamp_logs_debug_not_error(caplog):
    """Corrupt ISO timestamp triggers ValueError → logged at DEBUG, not ERROR."""
    monitor = _make_monitor()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(
            {"terminals": {"T1": {"last_update": "not-a-valid-timestamp"}}},
            f,
        )
        tmp_path = f.name

    try:
        monitor.dashboard_file = tmp_path
        with caplog.at_level(logging.DEBUG):
            monitor._initialize_heartbeats()
        # No ERROR records — ValueError was caught at DEBUG level
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records
        # DEBUG record references the bad value
        debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
        assert "not-a-valid-timestamp" in debug_msgs
    finally:
        os.unlink(tmp_path)


def test_generate_ack_receipt_persists_without_confidence_field(tmp_path, monkeypatch):
    """ADR-035 §9 PR-5 fix-r1 regression: `_generate_ack_receipt` used to set
    `confidence` on the persisted receipt with no `schema_version`, so the
    payload's own `schema_version` setdefault (v2) collided with the new
    legacy-field guard (T27) and `_append_receipt`'s broad except swallowed
    the resulting `AppendReceiptError` -- the ACK receipt silently failed to
    persist. `confidence` is a §3.3 trimmed field with no live receipt
    reader; drop it from the receipt (keep the local var for the log line)."""
    from heartbeat_ack_monitor import HeartbeatACKMonitor

    monitor = object.__new__(HeartbeatACKMonitor)
    monitor.state_dir = str(tmp_path)
    monitor.receipts_file = str(tmp_path / "t0_receipts.ndjson")
    monitor.active_dispatches = {}
    monitor._shadow_terminal_state_enabled = False
    monkeypatch.setenv("VNX_ACK_DIRECT_NOTIFY", "0")

    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=timezone.utc)
    dispatch_info = {
        "dispatch_id": "ack-confidence-regression",
        "task_id": "task-1",
        "terminal": "T1",
        "confirmation_time": now,
        "sent_time": now,
        "confirmation_method": "heartbeat",
    }
    signals = [{"type": "heartbeat", "delay_seconds": 1.2, "timestamp": now}]

    monitor._generate_ack_receipt(dispatch_info, signals)

    lines = Path(monitor.receipts_file).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "ACK receipt must persist, not be silently dropped"
    stored = json.loads(lines[0])
    assert stored["dispatch_id"] == "ack-confidence-regression"
    assert "confidence" not in stored


def test_check_log_change_oserror_swallowed(caplog, tmp_path):
    """OSError from getmtime on inaccessible log is caught and logged at DEBUG."""
    monitor = _make_monitor()
    fake_log = str(tmp_path / "terminal.log")
    monitor.terminal_logs = {"T1": fake_log}

    after_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with patch("os.path.exists", return_value=True), \
         patch("os.path.getmtime", side_effect=OSError("permission denied")), \
         caplog.at_level(logging.DEBUG):
        result = monitor._check_log_change("T1", None, after_time)

    assert result is None
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "permission denied" in debug_msgs
