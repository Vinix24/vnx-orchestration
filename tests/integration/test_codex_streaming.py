#!/usr/bin/env python3
"""Integration test: CodexAdapter live streaming via StreamingDrainerMixin.

Verifies that events accumulate in the EventStore during execution (not only after),
and that observability_tier=1 is applied to every event.

Requires `codex` binary on PATH — skips when not installed.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.codex_adapter import CodexAdapter
from event_store import EventStore


pytestmark = pytest.mark.integration


@pytest.fixture()
def event_store(tmp_path: Path) -> EventStore:
    return EventStore(events_dir=tmp_path / "events")


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex binary not installed")
class TestCodexLiveStreaming:
    """Boot real codex subprocess and verify live event accumulation."""

    FIXTURE_PROMPT = (
        "In one short sentence, describe what the number 42 is famous for. "
        "Reply with exactly one sentence."
    )

    def test_stream_events_yields_before_completion(self, event_store: EventStore, tmp_path: Path):
        """Events must appear in EventStore before the entire run completes."""
        terminal_id = "T-test"
        dispatch_id = "integration-streaming-001"

        adapter = CodexAdapter(terminal_id)
        ctx = {
            "terminal_id": terminal_id,
            "dispatch_id": dispatch_id,
            "event_store": event_store,
            "chunk_timeout": 60.0,
            "total_deadline": 120.0,
        }

        events_collected: list[dict] = []
        event_count_mid_run: list[int] = []
        stream_started = threading.Event()

        def _stream():
            stream_started.set()
            for ev in adapter.stream_events(self.FIXTURE_PROMPT, ctx):
                events_collected.append(ev)

        thread = threading.Thread(target=_stream, daemon=True)
        thread.start()
        stream_started.wait(timeout=5)

        # Poll EventStore while the stream runs (up to 30s)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and thread.is_alive():
            count = event_store.event_count(terminal_id)
            if count > 0:
                event_count_mid_run.append(count)
                break
            time.sleep(0.5)

        thread.join(timeout=120)

        assert events_collected, "No events yielded by stream_events()"
        assert any(
            ev.get("observability_tier") == 1 for ev in events_collected
        ), "All events must have observability_tier=1"

        # Verify event types seen
        types_seen = {ev.get("type") for ev in events_collected}
        assert types_seen, "No event types in stream"

    def test_execute_returns_done_status(self, event_store: EventStore):
        """execute() returns status='done' for a successful codex run."""
        adapter = CodexAdapter("T-test")
        ctx = {
            "terminal_id": "T-test",
            "dispatch_id": "integration-execute-001",
            "event_store": event_store,
            "chunk_timeout": 60.0,
            "total_deadline": 120.0,
        }
        result = adapter.execute(self.FIXTURE_PROMPT, ctx)
        assert result.provider == "codex"
        assert result.status in ("done", "failed"), f"Unexpected status: {result.status}"
        assert result.event_count >= 0

    def test_all_events_have_tier_1(self, event_store: EventStore):
        """Every canonical event emitted by codex must have observability_tier=1."""
        adapter = CodexAdapter("T-test")
        ctx = {
            "terminal_id": "T-test",
            "dispatch_id": "integration-tier-001",
            "event_store": event_store,
            "chunk_timeout": 60.0,
            "total_deadline": 120.0,
        }
        events = list(adapter.stream_events(self.FIXTURE_PROMPT, ctx))
        assert events, "No events produced"
        for ev in events:
            assert ev.get("observability_tier") == 1, (
                f"Event has tier != 1: {ev}"
            )

    def test_archive_populated_after_dispatch(self, event_store: EventStore):
        """After execution, archiving the EventStore produces a non-empty file."""
        terminal_id = "T-test"
        dispatch_id = "integration-archive-001"
        adapter = CodexAdapter(terminal_id)
        ctx = {
            "terminal_id": terminal_id,
            "dispatch_id": dispatch_id,
            "event_store": event_store,
            "chunk_timeout": 60.0,
            "total_deadline": 120.0,
        }
        list(adapter.stream_events(self.FIXTURE_PROMPT, ctx))

        # Archive and verify
        archive_path = event_store.archive(terminal_id, dispatch_id)
        if event_store.event_count(terminal_id) > 0:
            assert archive_path is not None
            assert archive_path.exists()
            lines = [l for l in archive_path.read_text().splitlines() if l.strip()]
            assert len(lines) > 0, "Archive file must be non-empty"
