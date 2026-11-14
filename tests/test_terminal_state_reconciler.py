#!/usr/bin/env python3
"""Tests for terminal_state fallback reconciler."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from terminal_state_reconciler import ReconcilerConfig, reconcile_terminal_state  # noqa: E402


def _write_receipts(path: Path, lines: list[dict]) -> None:
    payload = "\n".join(json.dumps(item) for item in lines) + "\n"
    path.write_text(payload, encoding="utf-8")


def _tmux_stub(_terminals, _state_dir: Path, _allow_tmux_probe: bool):
    return {
        "T1": {
            "pane_id": "%1",
            "pane_alive": True,
            "current_command": "claude",
            "recent_log_activity": "2026-02-10T12:00:05+00:00",
            "seconds_since_log_activity": 10,
        },
        "T2": {
            "pane_id": "%2",
            "pane_alive": True,
            "current_command": "claude",
            "recent_log_activity": "2026-02-10T12:00:04+00:00",
            "seconds_since_log_activity": 12,
        },
        "T3": {
            "pane_id": "%3",
            "pane_alive": True,
            "current_command": "claude",
            "recent_log_activity": "2026-02-10T12:00:03+00:00",
            "seconds_since_log_activity": 15,
        },
    }


def _process_stub():
    return {
        "processes": {
            "dispatcher": True,
            "receipt_processor": True,
            "heartbeat_monitor": True,
        },
        "core_pipeline_healthy": True,
    }


def _tmux_stub_node_recent(_terminals, _state_dir: Path, _allow_tmux_probe: bool):
    return {
        "T1": {
            "pane_id": "%1",
            "pane_alive": True,
            "current_command": "node",
            "recent_log_activity": "2026-02-10T12:00:05+00:00",
            "seconds_since_log_activity": 8,
        },
        "T2": {
            "pane_id": "%2",
            "pane_alive": True,
            "current_command": "node",
            "recent_log_activity": "2026-02-10T12:00:04+00:00",
            "seconds_since_log_activity": 9,
        },
        "T3": {
            "pane_id": "%3",
            "pane_alive": True,
            "current_command": "node",
            "recent_log_activity": "2026-02-10T12:00:03+00:00",
            "seconds_since_log_activity": 10,
        },
    }


def _tmux_stub_spinner_titles(_terminals, _state_dir: Path, _allow_tmux_probe: bool):
    return {
        "T1": {
            "pane_id": "%1",
            "pane_alive": True,
            "current_command": "node",
            "pane_title": "⠂ Backend Task",
            "pane_active": False,
            "recent_log_activity": "2026-02-10T11:40:00+00:00",
            "seconds_since_log_activity": 1500,
        },
        "T2": {
            "pane_id": "%2",
            "pane_alive": True,
            "current_command": "node",
            "pane_title": "⠐ Crawler Task",
            "pane_active": False,
            "recent_log_activity": "2026-02-10T11:40:00+00:00",
            "seconds_since_log_activity": 1500,
        },
        "T3": {
            "pane_id": "%3",
            "pane_alive": True,
            "current_command": "node",
            "pane_title": "⠠ Investigation Task",
            "pane_active": False,
            "recent_log_activity": "2026-02-10T11:40:00+00:00",
            "seconds_since_log_activity": 1500,
        },
    }


def _tmux_stub_node_stale(_terminals, _state_dir: Path, _allow_tmux_probe: bool):
    return {
        "T1": {
            "pane_id": "%1",
            "pane_alive": True,
            "current_command": "node",
            "recent_log_activity": "2026-02-10T11:40:00+00:00",
            "seconds_since_log_activity": 1500,
        },
        "T2": {
            "pane_id": "%2",
            "pane_alive": True,
            "current_command": "node",
            "recent_log_activity": "2026-02-10T11:40:00+00:00",
            "seconds_since_log_activity": 1500,
        },
        "T3": {
            "pane_id": "%3",
            "pane_alive": True,
            "current_command": "node",
            "recent_log_activity": "2026-02-10T11:40:00+00:00",
            "seconds_since_log_activity": 1500,
        },
    }


def test_reconciler_missing_primary_uses_fallback(tmp_path: Path):
    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_complete",
                "terminal": "T1",
                "timestamp": "2026-02-10T12:00:10+00:00",
                "dispatch_id": "dispatch-A",
            },
            {
                "event_type": "task_timeout",
                "terminal": "T2",
                "timestamp": "2026-02-10T12:00:11+00:00",
                "dispatch_id": "dispatch-B",
                "status": "no_confirmation",
            },
            {
                "event_type": "task_started",
                "terminal": "T3",
                "timestamp": "2026-02-10T12:00:12+00:00",
                "dispatch_id": "dispatch-C",
            },
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        config=ReconcilerConfig(stale_after_seconds=60),
        tmux_probe=_tmux_stub,
        process_probe=_process_stub,
    )

    assert result["degraded"] is True
    assert "primary_missing" in result["degraded_reasons"]
    assert result["terminals"]["T1"]["status"] == "active"
    assert result["terminals"]["T2"]["status"] == "blocked"
    assert result["terminals"]["T3"]["status"] == "working"
    assert result["terminals"]["T3"]["claimed_by"] == "dispatch-C"


def test_reconciler_failure_receipt_keeps_terminal_available(tmp_path: Path):
    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_failed",
                "terminal": "T2",
                "timestamp": "2026-02-10T12:00:11+00:00",
                "dispatch_id": "dispatch-B",
                "status": "error",
            },
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        config=ReconcilerConfig(stale_after_seconds=60),
        tmux_probe=_tmux_stub_node_stale,
        process_probe=_process_stub,
    )

    assert result["terminals"]["T2"]["status"] == "idle"
    assert result["terminals"]["T2"]["claimed_by"] is None


def test_reconciler_corrupted_primary_marks_degraded(tmp_path: Path):
    (tmp_path / "terminal_state.json").write_text("{bad-json", encoding="utf-8")
    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_started",
                "terminal": "T1",
                "timestamp": "2026-02-10T12:00:20+00:00",
                "dispatch_id": "dispatch-Z",
            }
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        tmux_probe=_tmux_stub,
        process_probe=_process_stub,
    )

    reasons = result["degraded_reasons"]
    assert any(reason.startswith("primary_corrupt:") for reason in reasons)
    assert result["terminals"]["T1"]["status"] == "working"
    assert result["terminals"]["T1"]["claimed_by"] == "dispatch-Z"


def test_reconciler_detects_stale_primary_and_fallback(tmp_path: Path):
    primary = {
        "schema_version": 1,
        "terminals": {
            "T1": {
                "terminal_id": "T1",
                "status": "claimed",
                "claimed_by": "old-claim",
                "claimed_at": "2026-02-10T10:00:00+00:00",
                "lease_expires_at": "2026-02-10T10:02:00+00:00",
                "last_activity": "2026-02-10T10:00:05+00:00",
                "version": 3,
            }
        },
    }
    primary_path = tmp_path / "terminal_state.json"
    primary_path.write_text(json.dumps(primary), encoding="utf-8")

    stale_epoch = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    os.utime(primary_path, (stale_epoch, stale_epoch))

    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_complete",
                "terminal": "T1",
                "timestamp": "2026-02-10T12:00:30+00:00",
                "dispatch_id": "old-claim",
            }
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        config=ReconcilerConfig(stale_after_seconds=5),
        tmux_probe=_tmux_stub,
        process_probe=_process_stub,
    )

    assert "primary_stale" in result["degraded_reasons"]
    assert result["primary_health"]["stale"] is True
    assert result["terminals"]["T1"]["status"] == "active"
    assert result["terminals"]["T1"]["claimed_by"] is None


def test_reconciler_split_brain_conflicting_claims(tmp_path: Path):
    primary = {
        "schema_version": 1,
        "terminals": {
            "T1": {
                "terminal_id": "T1",
                "status": "claimed",
                "claimed_by": "dispatch-conflict",
                "claimed_at": "2026-02-10T12:00:00+00:00",
                "lease_expires_at": "2026-02-10T12:01:00+00:00",
                "last_activity": "2026-02-10T12:00:01+00:00",
                "version": 1,
            },
            "T2": {
                "terminal_id": "T2",
                "status": "claimed",
                "claimed_by": "dispatch-conflict",
                "claimed_at": "2026-02-10T12:00:00+00:00",
                "lease_expires_at": "2026-02-10T12:01:00+00:00",
                "last_activity": "2026-02-10T12:00:02+00:00",
                "version": 1,
            },
        },
    }
    (tmp_path / "terminal_state.json").write_text(json.dumps(primary), encoding="utf-8")

    # The claims only count while their lease is live, so pin the clock inside it.
    result = reconcile_terminal_state(
        tmp_path,
        now=datetime(2026, 2, 10, 12, 0, 30, tzinfo=timezone.utc),
        tmux_probe=_tmux_stub,
        process_probe=_process_stub,
    )

    assert result["degraded"] is True
    assert any(
        reason.startswith("split_brain_conflicting_claim:dispatch-conflict:T1,T2")
        for reason in result["degraded_reasons"]
    )


def test_reconciler_repair_mode_writes_primary(tmp_path: Path):
    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_started",
                "terminal": "T1",
                "timestamp": "2026-02-10T12:01:00+00:00",
                "dispatch_id": "dispatch-repair",
            }
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        repair=True,
        tmux_probe=_tmux_stub,
        process_probe=_process_stub,
    )

    assert result["mode"] == "repair"
    target = tmp_path / "terminal_state.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["terminals"]["T1"]["claimed_by"] == "dispatch-repair"


def test_reconciler_treats_node_runtime_with_recent_activity_as_active(tmp_path: Path):
    result = reconcile_terminal_state(
        tmp_path,
        tmux_probe=_tmux_stub_node_recent,
        process_probe=_process_stub,
    )

    assert result["terminals"]["T1"]["status"] == "active"
    assert result["terminals"]["T2"]["status"] == "active"
    assert result["terminals"]["T3"]["status"] == "active"


def test_reconciler_treats_spinner_titles_as_active_without_recent_logs(tmp_path: Path):
    result = reconcile_terminal_state(
        tmp_path,
        tmux_probe=_tmux_stub_spinner_titles,
        process_probe=_process_stub,
    )

    assert result["terminals"]["T1"]["status"] == "active"
    assert result["terminals"]["T2"]["status"] == "active"
    assert result["terminals"]["T3"]["status"] == "active"


def test_reconciler_overrides_idle_receipt_with_live_spinner_signal(tmp_path: Path):
    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_complete",
                "terminal": "T3",
                "timestamp": "2026-02-10T12:00:10+00:00",
                "dispatch_id": "dispatch-C",
            }
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        tmux_probe=_tmux_stub_spinner_titles,
        process_probe=_process_stub,
    )

    assert result["terminals"]["T3"]["status"] == "active"


def test_reconciler_keeps_claimed_terminal_working_with_stale_activity(tmp_path: Path):
    now = datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc)
    primary = {
        "schema_version": 1,
        "terminals": {
            "T1": {
                "terminal_id": "T1",
                "status": "claimed",
                "claimed_by": "dispatch-waiting-user",
                "claimed_at": "2026-02-10T11:50:00+00:00",
                "lease_expires_at": "2026-02-10T12:10:00+00:00",
                "last_activity": "2026-02-10T11:40:00+00:00",
                "version": 4,
            }
        },
    }
    (tmp_path / "terminal_state.json").write_text(json.dumps(primary), encoding="utf-8")

    result = reconcile_terminal_state(
        tmp_path,
        now=now,
        config=ReconcilerConfig(active_window_seconds=120),
        tmux_probe=_tmux_stub_node_stale,
        process_probe=_process_stub,
    )

    assert result["terminals"]["T1"]["status"] == "working"
    assert result["terminals"]["T1"]["claimed_by"] == "dispatch-waiting-user"


def test_reconciler_expired_lease_allows_idle_with_stale_activity(tmp_path: Path):
    now = datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc)
    primary = {
        "schema_version": 1,
        "terminals": {
            "T1": {
                "terminal_id": "T1",
                "status": "claimed",
                "claimed_by": "dispatch-expired",
                "claimed_at": "2026-02-10T11:20:00+00:00",
                "lease_expires_at": "2026-02-10T11:30:00+00:00",
                "last_activity": "2026-02-10T11:25:00+00:00",
                "version": 2,
            }
        },
    }
    (tmp_path / "terminal_state.json").write_text(json.dumps(primary), encoding="utf-8")

    result = reconcile_terminal_state(
        tmp_path,
        now=now,
        config=ReconcilerConfig(active_window_seconds=120),
        tmux_probe=_tmux_stub_node_stale,
        process_probe=_process_stub,
    )

    assert result["terminals"]["T1"]["status"] == "idle"


def test_reconciler_prefers_timestamped_complete_over_later_timeout_without_timestamp(tmp_path: Path):
    _write_receipts(
        tmp_path / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_complete",
                "terminal": "T1",
                "timestamp": "2026-02-10T12:00:30+00:00",
                "dispatch_id": "dispatch-A",
                "status": "success",
            },
            {
                "event_type": "task_timeout",
                "terminal": "T1",
                "dispatch_id": "dispatch-A",
                "status": "no_confirmation",
            },
        ],
    )

    result = reconcile_terminal_state(
        tmp_path,
        tmux_probe=_tmux_stub_node_stale,
        process_probe=_process_stub,
    )

    # Since 0f341325d a receipt with a timestamp outranks one without, even
    # when the timestamp-less one comes later in the file.
    assert result["terminals"]["T1"]["status"] == "idle"
