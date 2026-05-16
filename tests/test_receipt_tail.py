"""Tests for scripts/control_centre/receipt_tail.py (Wave 5 PR-5.6)."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _collect_n(tail: ReceiptTail, n: int, timeout: float = 5.0) -> list[MergedEvent]:
    events: list[MergedEvent] = []
    deadline = time.monotonic() + timeout

    for event in tail.stream():
        events.extend([event] if event else [])
        if len(events) >= n:
            break
        if time.monotonic() > deadline:
            break

    tail.stop()
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_streams_single_project_events_in_order(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    path = cfg.ndjson_path

    for i in range(3):
        _write_event(path, {
            "dispatch_id": f"d-{i}",
            "event_type": "task_complete",
            "timestamp": f"2026-05-16T12:0{i}:00.000+00:00",
            "status": "success",
        })

    tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
    events = _collect_n(tail, 3)

    assert len(events) == 3
    dispatch_ids = [e.dispatch_id for e in events]
    assert dispatch_ids == ["d-0", "d-1", "d-2"]
    assert all(e.project_id == "proj-a" for e in events)


def test_merges_multi_project_with_timestamp_ordering(tmp_path: Path) -> None:
    cfg_a = _make_config(tmp_path, "proj-a")
    cfg_b = _make_config(tmp_path, "proj-b")

    _write_event(cfg_a.ndjson_path, {
        "dispatch_id": "a-1",
        "event_type": "task_complete",
        "timestamp": "2026-05-16T12:00:00.000+00:00",
    })
    _write_event(cfg_b.ndjson_path, {
        "dispatch_id": "b-1",
        "event_type": "task_complete",
        "timestamp": "2026-05-16T12:00:01.000+00:00",
    })
    _write_event(cfg_a.ndjson_path, {
        "dispatch_id": "a-2",
        "event_type": "task_complete",
        "timestamp": "2026-05-16T12:00:02.000+00:00",
    })

    tail = ReceiptTail(projects=[cfg_a, cfg_b], poll_interval=0.05)
    events = _collect_n(tail, 3)

    assert len(events) == 3
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps), "events must be timestamp-ordered"
    assert events[0].dispatch_id == "a-1"
    assert events[1].dispatch_id == "b-1"
    assert events[2].dispatch_id == "a-2"


def test_resumes_from_offset_after_restart(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    path = cfg.ndjson_path

    _write_event(path, {"dispatch_id": "d-0", "event_type": "task_complete", "timestamp": "T0"})

    tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
    first_batch = _collect_n(tail, 1)
    assert len(first_batch) == 1
    assert first_batch[0].dispatch_id == "d-0"

    _write_event(path, {"dispatch_id": "d-1", "event_type": "task_complete", "timestamp": "T1"})
    _write_event(path, {"dispatch_id": "d-2", "event_type": "task_complete", "timestamp": "T2"})

    tail2 = ReceiptTail(projects=[cfg], poll_interval=0.05)
    second_batch = _collect_n(tail2, 3)

    assert len(second_batch) == 3, "fresh tail reads all events from start"


def test_handles_truncated_ring_buffer(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    path = cfg.ndjson_path

    for i in range(3):
        _write_event(path, {"dispatch_id": f"old-{i}", "event_type": "task_complete", "timestamp": f"T{i}"})

    tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
    old_events = _collect_n(tail, 3)
    assert len(old_events) == 3

    path.write_text("", encoding="utf-8")
    _write_event(path, {"dispatch_id": "new-0", "event_type": "task_complete", "timestamp": "T9"})

    tail2 = ReceiptTail(projects=[cfg], poll_interval=0.05)
    new_events = _collect_n(tail2, 1)

    assert any(e.dispatch_id == "new-0" for e in new_events), (
        "after truncation, new events must be emitted"
    )


def test_emits_partial_event_with_warning_on_malformed_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _make_config(tmp_path, "proj-a")
    path = cfg.ndjson_path

    path.write_text(
        json.dumps({"dispatch_id": "good-1", "event_type": "task_complete", "timestamp": "T0"})
        + "\n"
        + "{malformed json\n"
        + json.dumps({"dispatch_id": "good-2", "event_type": "task_complete", "timestamp": "T2"})
        + "\n",
        encoding="utf-8",
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="scripts.control_centre.receipt_tail"):
        tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
        events = _collect_n(tail, 2)

    dispatch_ids = [e.dispatch_id for e in events]
    assert "good-1" in dispatch_ids
    assert "good-2" in dispatch_ids
    assert any("malformed" in r.message.lower() for r in caplog.records), (
        "malformed line must emit a WARNING"
    )


def test_no_event_drops_at_high_throughput(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, "proj-stress")
    path = cfg.ndjson_path

    n = 100
    for i in range(n):
        _write_event(path, {
            "dispatch_id": f"stress-{i}",
            "event_type": "task_complete",
            "timestamp": f"2026-05-16T12:00:{i:02d}.000+00:00",
        })

    tail = ReceiptTail(projects=[cfg], poll_interval=0.01)
    events = _collect_n(tail, n, timeout=10.0)

    assert len(events) == n, f"Expected {n} events, got {len(events)}"
    seen_ids = {e.dispatch_id for e in events}
    expected_ids = {f"stress-{i}" for i in range(n)}
    assert seen_ids == expected_ids, "No events must be dropped at synthetic load"


def test_partial_write_does_not_lose_data(tmp_path: Path) -> None:
    """Finding 2 regression: partial write (no trailing newline) must not drop the line."""
    cfg = _make_config(tmp_path, "proj-partial")
    path = cfg.ndjson_path

    tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
    ps = tail._projects[0]

    # Write first complete event
    _write_event(path, {"dispatch_id": "first", "event_type": "task_complete", "timestamp": "T0"})
    events_first = tail._poll_project(ps)
    assert len(events_first) == 1
    assert events_first[0].dispatch_id == "first"

    offset_after_first = ps.offset

    # Write partial second event (no newline — simulates writer mid-flush)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"dispatch_id": "second", "event_type": "task_complete", "timestamp": "T1"}')

    # Poll while partial — offset must NOT advance, event must NOT be yielded
    events_partial = tail._poll_project(ps)
    assert events_partial == [], "Partial write must yield no events"
    assert ps.offset == offset_after_first, "Offset must not advance on partial write"

    # Complete the line (add newline)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n")

    # Now poll — must yield the second event
    events_complete = tail._poll_project(ps)
    assert len(events_complete) == 1
    assert events_complete[0].dispatch_id == "second", "Second event must be received after newline"

    tail.stop()


def test_truly_malformed_line_skipped_with_warning_and_advance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 2: genuine malformed JSON (full line) is skipped with warning; offset advances."""
    cfg = _make_config(tmp_path, "proj-malformed")
    path = cfg.ndjson_path

    path.write_text(
        "{bad json here}\n"
        + json.dumps({"dispatch_id": "after-bad", "event_type": "task_complete", "timestamp": "T1"})
        + "\n",
        encoding="utf-8",
    )

    tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
    ps = tail._projects[0]

    import logging
    with caplog.at_level(logging.WARNING, logger="scripts.control_centre.receipt_tail"):
        events = tail._poll_project(ps)

    assert len(events) == 1
    assert events[0].dispatch_id == "after-bad"
    assert any("malformed" in r.message.lower() for r in caplog.records), (
        "malformed line must emit a WARNING"
    )
    assert ps.offset == path.stat().st_size, "Offset must advance past malformed line"

    tail.stop()


def test_offset_only_advances_after_successful_parse(tmp_path: Path) -> None:
    """Finding 2: offset stays at line_start for partial write, advances on parse success."""
    cfg = _make_config(tmp_path, "proj-offset")
    path = cfg.ndjson_path

    tail = ReceiptTail(projects=[cfg], poll_interval=0.05)
    ps = tail._projects[0]

    # Write partial line (no closing brace or newline)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"dispatch_id": "d1"')

    # Poll — offset must stay at 0 (partial write)
    tail._poll_project(ps)
    assert ps.offset == 0, f"Offset must stay at 0 for partial write, got {ps.offset}"

    # Complete the line
    with open(path, "a", encoding="utf-8") as f:
        f.write(', "event_type": "task_complete", "timestamp": "T0"}\n')

    # Poll — offset must now advance
    events = tail._poll_project(ps)
    assert len(events) == 1
    assert events[0].dispatch_id == "d1"
    assert ps.offset == path.stat().st_size, "Offset must equal file size after successful parse"

    tail.stop()
