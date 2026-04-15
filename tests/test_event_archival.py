#!/usr/bin/env python3
"""Tests for event archival behavior in subprocess_dispatch.py (F58-PR2).

Verifies that:
  - events are archived on dispatch complete
  - archive path uses dispatch_id as filename
  - live event file is cleared after archive
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from event_store import EventStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_events_dir(tmp_path):
    return tmp_path / "events"


@pytest.fixture()
def event_store(tmp_events_dir):
    return EventStore(events_dir=tmp_events_dir)


def _write_events(store: EventStore, terminal: str, dispatch_id: str, count: int = 3) -> None:
    for i in range(count):
        store.append(terminal, {"type": "text", "data": {"text": f"event {i}"}}, dispatch_id=dispatch_id)


# ---------------------------------------------------------------------------
# test_events_archived_on_dispatch_complete
# ---------------------------------------------------------------------------

class TestEventsArchivedOnDispatchComplete:

    def test_archive_creates_file(self, event_store, tmp_events_dir):
        """archive() creates a file in events/archive/{terminal}/{dispatch_id}.ndjson."""
        _write_events(event_store, "T1", "dispatch-001")
        result = event_store.archive("T1", "dispatch-001")

        assert result is not None
        dest = Path(result)
        assert dest.exists()

    def test_archive_returns_none_for_empty_file(self, event_store):
        """archive() returns None when the live event file is empty."""
        result = event_store.archive("T1", "dispatch-empty")
        assert result is None

    def test_clear_with_dispatch_id_archives_and_truncates(self, event_store, tmp_events_dir):
        """clear(archive_dispatch_id=X) archives AND truncates the live file."""
        _write_events(event_store, "T1", "dispatch-002", count=5)

        live_path = tmp_events_dir / "T1.ndjson"
        assert live_path.stat().st_size > 0

        event_store.clear("T1", archive_dispatch_id="dispatch-002")

        # Live file should be empty (truncated)
        assert live_path.stat().st_size == 0

        # Archive should exist
        archive = tmp_events_dir / "archive" / "T1" / "dispatch-002.ndjson"
        assert archive.exists()


# ---------------------------------------------------------------------------
# test_archive_path_uses_dispatch_id
# ---------------------------------------------------------------------------

class TestArchivePathUsesDispatchId:

    def test_archive_filename_matches_dispatch_id(self, event_store, tmp_events_dir):
        """Archive file is named {dispatch_id}.ndjson."""
        _write_events(event_store, "T2", "f58-pr2-t1-20260414")
        result = event_store.archive("T2", "f58-pr2-t1-20260414")

        assert result is not None
        assert Path(result).name == "f58-pr2-t1-20260414.ndjson"

    def test_archive_directory_includes_terminal(self, event_store, tmp_events_dir):
        """Archive directory structure is events/archive/{terminal}/."""
        _write_events(event_store, "T3", "dispatch-xyz")
        result = event_store.archive("T3", "dispatch-xyz")

        assert result is not None
        path = Path(result)
        assert path.parent.name == "T3"
        assert path.parent.parent.name == "archive"

    def test_different_dispatches_get_separate_archive_files(self, event_store, tmp_events_dir):
        """Each dispatch_id produces a separate archive file."""
        _write_events(event_store, "T1", "dispatch-A")
        event_store.clear("T1", archive_dispatch_id="dispatch-A")

        _write_events(event_store, "T1", "dispatch-B")
        event_store.clear("T1", archive_dispatch_id="dispatch-B")

        archive_a = tmp_events_dir / "archive" / "T1" / "dispatch-A.ndjson"
        archive_b = tmp_events_dir / "archive" / "T1" / "dispatch-B.ndjson"

        assert archive_a.exists()
        assert archive_b.exists()


# ---------------------------------------------------------------------------
# test_live_events_cleared_after_archive
# ---------------------------------------------------------------------------

class TestLiveEventsClearedAfterArchive:

    def test_live_file_empty_after_clear(self, event_store, tmp_events_dir):
        """Live event file is empty after clear()."""
        _write_events(event_store, "T1", "dispatch-003", count=10)

        live_path = tmp_events_dir / "T1.ndjson"
        assert event_store.event_count("T1") == 10

        event_store.clear("T1", archive_dispatch_id="dispatch-003")

        assert event_store.event_count("T1") == 0

    def test_archive_content_matches_live_events(self, event_store, tmp_events_dir):
        """Archived file contains the same events that were in the live file."""
        _write_events(event_store, "T1", "dispatch-004", count=4)

        live_path = tmp_events_dir / "T1.ndjson"
        live_content_before = live_path.read_text()

        event_store.clear("T1", archive_dispatch_id="dispatch-004")

        archive = tmp_events_dir / "archive" / "T1" / "dispatch-004.ndjson"
        archive_content = archive.read_text()

        assert archive_content == live_content_before

    def test_next_dispatch_starts_with_empty_live_file(self, event_store, tmp_events_dir):
        """After clear, new events for the next dispatch accumulate correctly."""
        _write_events(event_store, "T1", "dispatch-005", count=3)
        event_store.clear("T1", archive_dispatch_id="dispatch-005")

        # Start new dispatch
        _write_events(event_store, "T1", "dispatch-006", count=2)

        assert event_store.event_count("T1") == 2

    def test_subprocess_dispatch_uses_clear_not_archive_only(self, tmp_events_dir):
        """subprocess_dispatch.py finally block calls clear() not archive()."""
        # Read the actual source to verify the call site
        dispatch_py = Path(__file__).parent.parent / "scripts" / "lib" / "subprocess_dispatch.py"
        source = dispatch_py.read_text()

        # The fixed implementation uses clear() with archive_dispatch_id
        assert "event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)" in source
        # The old broken form should NOT be present
        assert "event_store.archive(terminal_id, dispatch_id)" not in source
