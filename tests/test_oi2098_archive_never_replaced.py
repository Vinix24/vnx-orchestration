#!/usr/bin/env python3
"""OI-2098: copying a live stream into its archive extends it, never replaces it."""

import json
import sys
import threading
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


def _archive_lines(store):
    path = store.archive_dir("T1") / f"{DISPATCH}.ndjson"
    return [line for line in path.read_text().splitlines() if line.strip()]


def test_teardown_event_after_final_archive_is_added_not_replacing(store):
    for i in range(50):
        store.append("T1", {"type": "text", "data": {"n": i}}, dispatch_id=DISPATCH)
    assert store.archive_completed_stream("T1", DISPATCH) is not None
    assert len(_archive_lines(store)) == 50

    store.append("T1", {"type": "provider_teardown_worktree", "data": {}}, dispatch_id=DISPATCH)
    store.clear("T1", archive_dispatch_id=DISPATCH)

    lines = _archive_lines(store)
    assert len(lines) == 51
    assert json.loads(lines[-1])["type"] == "provider_teardown_worktree"
    assert store._terminal_path("T1").read_text() == ""


def test_locked_mover_finishing_before_the_copy_keeps_the_archive(store, monkeypatch):
    for i in range(21):
        store.append("T1", {"type": "text", "data": {"n": i}}, dispatch_id=DISPATCH)
    live = store._terminal_path("T1")
    dest = store.archive_dir("T1") / f"{DISPATCH}.ndjson"

    mover_done = threading.Event()
    copy_started = threading.Event()
    real_replace = es._replace_from_scratch
    real_copy2 = es.shutil.copy2

    def mover_replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        # Let a copier that is not held off by the lock run its size check first.
        copy_started.wait(timeout=0.5)

    def copier_copy2(src, dst, *args, **kwargs):
        # Only a copier that does not take the lock gets here: it has done its
        # size check and now waits until the mover has truncated the live file.
        copy_started.set()
        mover_done.wait(timeout=5)
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(es, "_replace_from_scratch", mover_replace)
    monkeypatch.setattr(es.shutil, "copy2", copier_copy2)

    def mover():
        try:
            EventStore._move_to_archive(
                live, dest, DISPATCH, owned_only=False, remove_live=False
            )
        finally:
            mover_done.set()

    thread = threading.Thread(target=mover)
    thread.start()
    store.clear("T1", archive_dispatch_id=DISPATCH)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert dest.stat().st_size > 0
    assert len(_archive_lines(store)) == 21


def test_archive_then_clear_on_the_same_live_content_repeats_no_line(store):
    for i in range(5):
        store.append("T1", {"type": "text", "data": {"n": i}}, dispatch_id=DISPATCH)

    store.archive("T1", DISPATCH)
    store.clear("T1", archive_dispatch_id=DISPATCH)

    lines = _archive_lines(store)
    assert len(lines) == 5
    assert len(set(lines)) == 5
