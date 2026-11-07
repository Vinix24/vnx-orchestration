#!/usr/bin/env python3
"""OI-2096: an event archive is extended without repeating lines.

A move that fails between the archive write and the truncate leaves the same
events in the live file and in the archive. The retry must not add them again.
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import event_store as es
from event_store import EventStore

DISPATCH = "A"


@pytest.fixture
def store(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    return EventStore(events_dir=events_dir)


@pytest.fixture
def killed_after_archive_write(monkeypatch):
    """The first archive write succeeds, then the process 'dies' before the truncate."""
    real = es._replace_from_scratch
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        real(*args, **kwargs)
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("killed between the archive write and the truncate")

    monkeypatch.setattr(es, "_replace_from_scratch", wrapper)
    return state


def _fill(store, terminal):
    for kind in ("init", "text", "result"):
        store.append(terminal, {"type": kind, "data": {}}, dispatch_id=DISPATCH)


def _archive_events(store, terminal):
    path = store.archive_dir(terminal) / f"{DISPATCH}.ndjson"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _results(events):
    return [e for e in events if e["type"] == "result"]


@pytest.mark.parametrize("terminal", ["T1", "ephemeral"])
def test_late_archive_retry_after_interrupted_move_adds_nothing(
    store, terminal, killed_after_archive_write
):
    _fill(store, terminal)
    with pytest.raises(OSError):
        store.archive_late_events(terminal, DISPATCH)
    interrupted = _archive_events(store, terminal)
    assert len(interrupted) == 3
    assert len(_results(interrupted)) == 1

    store.archive_late_events(terminal, DISPATCH)

    events = _archive_events(store, terminal)
    assert len(events) == 3
    assert len(_results(events)) == 1


@pytest.mark.parametrize("terminal", ["T1", "ephemeral"])
def test_end_of_run_archive_retry_after_interrupted_move_adds_nothing(
    store, terminal, killed_after_archive_write
):
    _fill(store, terminal)
    assert store.archive_completed_stream(terminal, DISPATCH) is None
    assert len(_archive_events(store, terminal)) == 3

    assert store.archive_completed_stream(terminal, DISPATCH) is not None

    events = _archive_events(store, terminal)
    assert len(events) == 3
    assert len(_results(events)) == 1


@pytest.mark.parametrize("terminal", ["T1", "ephemeral"])
def test_archive_route_retry_after_interrupted_move_adds_nothing(
    store, terminal, killed_after_archive_write
):
    _fill(store, terminal)
    with pytest.raises(OSError):
        store.clear(terminal, archive_dispatch_id=DISPATCH, dispatch_id=DISPATCH)
    store.clear(terminal, archive_dispatch_id=DISPATCH, dispatch_id=DISPATCH)

    events = _archive_events(store, terminal)
    assert len(events) == 3
    assert len(_results(events)) == 1


def test_a_line_repeated_inside_one_content_is_kept_twice(store):
    line = json.dumps({"type": "text", "dispatch_id": DISPATCH, "data": {"text": "same"}})
    live = store._terminal_path("T1")
    live.write_text(f"{line}\n{line}\n")

    store.archive_completed_stream("T1", DISPATCH)

    archive = store.archive_dir("T1") / f"{DISPATCH}.ndjson"
    assert archive.read_text().splitlines() == [line, line]


def test_only_the_new_lines_are_added_to_an_existing_archive(store):
    first = json.dumps({"type": "init", "dispatch_id": DISPATCH})
    second = json.dumps({"type": "result", "dispatch_id": DISPATCH})
    archive = store.archive_dir("T1") / f"{DISPATCH}.ndjson"
    archive.parent.mkdir(parents=True)
    archive.write_text(first)  # no final newline: the tail is not glued to it
    live = store._terminal_path("T1")
    live.write_text(f"{first}\n{second}\n")

    store.archive_completed_stream("T1", DISPATCH)

    assert archive.read_text().splitlines() == [first, second]


def test_nothing_new_leaves_the_archive_untouched(store):
    line = json.dumps({"type": "init", "dispatch_id": DISPATCH})
    archive = store.archive_dir("T1") / f"{DISPATCH}.ndjson"
    archive.parent.mkdir(parents=True)
    archive.write_text(f"{line}\n")
    before = archive.stat()
    store._terminal_path("T1").write_text(f"{line}\n")

    store.archive_completed_stream("T1", DISPATCH)

    after = archive.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert store._terminal_path("T1").read_text() == ""
