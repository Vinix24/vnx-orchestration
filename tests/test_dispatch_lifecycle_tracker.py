"""Tests for scripts/control_centre/dispatch_lifecycle_tracker.py (Wave 5 PR-5.6)."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.control_centre.dispatch_lifecycle_tracker import (
    DispatchLifecycleTracker,
    DispatchOutcome,
    DispatchStatus,
)
from scripts.control_centre.receipt_tail import MergedEvent, ProjectConfig, ReceiptTail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, project_id: str) -> ProjectConfig:
    receipt_path = tmp_path / project_id / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    return ProjectConfig(
        project_id=project_id,
        root=tmp_path / project_id,
        ndjson_path=receipt_path,
    )


def _write_event(path: Path, event: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _make_tail_and_tracker(configs: List[ProjectConfig]) -> tuple[ReceiptTail, DispatchLifecycleTracker]:
    tail = ReceiptTail(projects=configs, poll_interval=0.05)
    tracker = DispatchLifecycleTracker(receipt_tail=tail)
    return tail, tracker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_track_returns_completed_on_success_receipt(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    tail, tracker = _make_tail_and_tracker([cfg])

    dispatch_id = "disp-001"
    tracker.register(dispatch_id, "proj-a")

    def _emit_after_delay() -> None:
        time.sleep(0.1)
        _write_event(cfg.ndjson_path, {
            "dispatch_id": dispatch_id,
            "event_type": "task_complete",
            "status": "success",
            "timestamp": "2026-05-16T12:00:00.000+00:00",
        })

    threading.Thread(target=_emit_after_delay, daemon=True).start()

    outcome = tracker.track(dispatch_id, "proj-a", timeout_seconds=5.0)
    tail.stop()

    assert outcome.status == DispatchStatus.COMPLETED
    assert outcome.success is True
    assert outcome.dispatch_id == dispatch_id


def test_track_returns_failed_on_failure_receipt(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    tail, tracker = _make_tail_and_tracker([cfg])

    dispatch_id = "disp-fail"
    tracker.register(dispatch_id, "proj-a")

    def _emit() -> None:
        time.sleep(0.1)
        _write_event(cfg.ndjson_path, {
            "dispatch_id": dispatch_id,
            "event_type": "task_failed",
            "status": "failure",
            "timestamp": "2026-05-16T12:00:00.000+00:00",
        })

    threading.Thread(target=_emit, daemon=True).start()

    outcome = tracker.track(dispatch_id, "proj-a", timeout_seconds=5.0)
    tail.stop()

    assert outcome.status == DispatchStatus.FAILED
    assert outcome.success is False


def test_track_returns_timeout_after_window(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    tail, tracker = _make_tail_and_tracker([cfg])

    dispatch_id = "disp-timeout"

    outcome = tracker.track(dispatch_id, "proj-a", timeout_seconds=0.15)
    tail.stop()

    assert outcome.status == DispatchStatus.TIMEOUT


def test_status_non_blocking_returns_current_state(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    tail, tracker = _make_tail_and_tracker([cfg])

    dispatch_id = "disp-status"
    tracker.register(dispatch_id, "proj-a")

    assert tracker.status(dispatch_id, "proj-a") == DispatchStatus.PENDING

    _write_event(cfg.ndjson_path, {
        "dispatch_id": dispatch_id,
        "event_type": "task_complete",
        "status": "success",
        "timestamp": "2026-05-16T12:00:00.000+00:00",
    })

    deadline = time.monotonic() + 3.0
    while tracker.status(dispatch_id, "proj-a") == DispatchStatus.PENDING:
        time.sleep(0.05)
        if time.monotonic() > deadline:
            break

    tail.stop()
    assert tracker.status(dispatch_id, "proj-a") == DispatchStatus.COMPLETED


def test_parallel_dispatches_no_crosstalk(tmp_path: Path) -> None:
    cfg_a = _make_config(tmp_path, "proj-a")
    cfg_b = _make_config(tmp_path, "proj-b")
    tail, tracker = _make_tail_and_tracker([cfg_a, cfg_b])

    id_a = "disp-alpha"
    id_b = "disp-beta"

    tracker.register(id_a, "proj-a")
    tracker.register(id_b, "proj-b")

    outcomes: dict[str, DispatchOutcome] = {}

    def _track_a() -> None:
        outcomes["a"] = tracker.track(id_a, "proj-a", timeout_seconds=5.0)

    def _track_b() -> None:
        outcomes["b"] = tracker.track(id_b, "proj-b", timeout_seconds=5.0)

    threads = [
        threading.Thread(target=_track_a, daemon=True),
        threading.Thread(target=_track_b, daemon=True),
    ]
    for t in threads:
        t.start()

    time.sleep(0.1)

    _write_event(cfg_a.ndjson_path, {
        "dispatch_id": id_a,
        "event_type": "task_complete",
        "status": "success",
        "timestamp": "2026-05-16T12:00:00.000+00:00",
    })
    _write_event(cfg_b.ndjson_path, {
        "dispatch_id": id_b,
        "event_type": "task_failed",
        "status": "failure",
        "timestamp": "2026-05-16T12:00:01.000+00:00",
    })

    for t in threads:
        t.join(timeout=6.0)

    tail.stop()

    assert "a" in outcomes, "proj-a tracker did not complete"
    assert "b" in outcomes, "proj-b tracker did not complete"
    assert outcomes["a"].status == DispatchStatus.COMPLETED, "proj-a must complete"
    assert outcomes["b"].status == DispatchStatus.FAILED, "proj-b must fail"
    assert outcomes["a"].dispatch_id == id_a
    assert outcomes["b"].dispatch_id == id_b


def test_cross_project_events_do_not_affect_other_dispatch(tmp_path: Path) -> None:
    cfg_a = _make_config(tmp_path, "proj-a")
    cfg_b = _make_config(tmp_path, "proj-b")
    tail, tracker = _make_tail_and_tracker([cfg_a, cfg_b])

    dispatch_id = "shared-name"
    tracker.register(dispatch_id, "proj-a")

    _write_event(cfg_b.ndjson_path, {
        "dispatch_id": dispatch_id,
        "event_type": "task_complete",
        "status": "success",
        "timestamp": "2026-05-16T12:00:00.000+00:00",
    })

    time.sleep(0.3)
    status = tracker.status(dispatch_id, "proj-a")
    tail.stop()

    assert status == DispatchStatus.PENDING, (
        "Event from proj-b must NOT update dispatch registered under proj-a"
    )


def test_same_dispatch_id_across_projects_tracked_separately(tmp_path: Path) -> None:
    """Finding 1 regression: same dispatch_id in two projects must be independent entries."""
    cfg_a = _make_config(tmp_path, "proj-a")
    cfg_b = _make_config(tmp_path, "proj-b")
    tail, tracker = _make_tail_and_tracker([cfg_a, cfg_b])

    shared_id = "collision-dispatch"
    tracker.register(shared_id, "proj-a")
    tracker.register(shared_id, "proj-b")

    # Both should start as PENDING — separate entries, not one overwriting the other
    assert tracker.status(shared_id, "proj-a") == DispatchStatus.PENDING
    assert tracker.status(shared_id, "proj-b") == DispatchStatus.PENDING

    # Complete only proj-a's version
    _write_event(cfg_a.ndjson_path, {
        "dispatch_id": shared_id,
        "event_type": "task_complete",
        "status": "success",
        "timestamp": "2026-05-16T12:00:00.000+00:00",
    })

    deadline = time.monotonic() + 3.0
    while tracker.status(shared_id, "proj-a") == DispatchStatus.PENDING:
        time.sleep(0.05)
        if time.monotonic() > deadline:
            break

    tail.stop()

    assert tracker.status(shared_id, "proj-a") == DispatchStatus.COMPLETED, (
        "proj-a dispatch must complete from its own receipt"
    )
    assert tracker.status(shared_id, "proj-b") == DispatchStatus.PENDING, (
        "proj-b dispatch must stay PENDING — it shares dispatch_id but is isolated"
    )


def test_parallel_dispatches_isolated_by_project(tmp_path: Path) -> None:
    """Finding 1 regression: parallel track() calls with same dispatch_id must not crosstalk."""
    cfg_a = _make_config(tmp_path, "proj-a")
    cfg_b = _make_config(tmp_path, "proj-b")
    tail, tracker = _make_tail_and_tracker([cfg_a, cfg_b])

    shared_id = "shared-parallel"
    outcomes: dict[str, DispatchOutcome] = {}

    def _track_a() -> None:
        outcomes["a"] = tracker.track(shared_id, "proj-a", timeout_seconds=5.0)

    def _track_b() -> None:
        outcomes["b"] = tracker.track(shared_id, "proj-b", timeout_seconds=5.0)

    threads = [
        threading.Thread(target=_track_a, daemon=True),
        threading.Thread(target=_track_b, daemon=True),
    ]
    for t in threads:
        t.start()

    time.sleep(0.1)

    _write_event(cfg_a.ndjson_path, {
        "dispatch_id": shared_id,
        "event_type": "task_complete",
        "status": "success",
        "timestamp": "2026-05-16T12:00:00.000+00:00",
    })
    _write_event(cfg_b.ndjson_path, {
        "dispatch_id": shared_id,
        "event_type": "task_failed",
        "status": "failure",
        "timestamp": "2026-05-16T12:00:01.000+00:00",
    })

    for t in threads:
        t.join(timeout=6.0)

    tail.stop()

    assert "a" in outcomes, "proj-a tracker did not complete"
    assert "b" in outcomes, "proj-b tracker did not complete"
    assert outcomes["a"].status == DispatchStatus.COMPLETED, "proj-a must complete"
    assert outcomes["b"].status == DispatchStatus.FAILED, "proj-b must fail"
    assert outcomes["a"].project_id == "proj-a"
    assert outcomes["b"].project_id == "proj-b"
