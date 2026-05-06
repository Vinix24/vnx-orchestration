#!/usr/bin/env python3
"""Integration test: GeminiAdapter streaming gated by VNX_GEMINI_STREAM=1.

Verifies:
- VNX_GEMINI_STREAM=0 (default): behavior identical to pre-migration (single Tier-3 event).
- VNX_GEMINI_STREAM=1: live events accumulate in EventStore during execution; Tier-1 on all events.
- Real gemini subprocess integration requires `gemini` on PATH (skipped otherwise).
- Fake subprocess path covers gated drain logic without needing real gemini binary.

BILLING SAFETY: No Anthropic SDK. subprocess-only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.gemini_adapter import GeminiAdapter
from event_store import EventStore
from provider_adapter import AdapterResult

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fake subprocess scripts that mimic gemini --output-format stream-json
# ---------------------------------------------------------------------------

_FAKE_GEMINI_STREAM_SCRIPT = """\
import json, sys

print(json.dumps({"type": "session_start"}), flush=True)
print(json.dumps({"type": "message", "text": "Reviewing code..."}), flush=True)
print(json.dumps({"type": "tool_use", "name": "read_file", "args": {"path": "README.md"}}), flush=True)
print(json.dumps({"type": "tool_result", "output": "# README contents"}), flush=True)
print(json.dumps({"type": "result", "text": "No critical issues found.",
                  "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 200}}), flush=True)
sys.exit(0)
"""

_FAKE_GEMINI_CLEAN_SCRIPT = """\
import json, sys

print(json.dumps({"type": "session_start"}), flush=True)
print(json.dumps({"type": "message", "text": "Done."}), flush=True)
print(json.dumps({"type": "result", "text": "Complete."}), flush=True)
sys.exit(0)
"""


def _spawn_fake_gemini(script: str) -> subprocess.Popen:
    """Spawn a Python subprocess that mimics gemini stream-json output."""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


@pytest.fixture()
def event_store(tmp_path: Path) -> EventStore:
    return EventStore(events_dir=tmp_path / "events")


# ---------------------------------------------------------------------------
# Default-off tests (VNX_GEMINI_STREAM unset or 0)
# ---------------------------------------------------------------------------

class TestDefaultOffBehavior:
    """Verify VNX_GEMINI_STREAM=0 preserves legacy single-event Tier-3 path."""

    def test_stream_events_yields_single_tier3_result(self, monkeypatch):
        """stream_events() without stream flag yields one Tier-3 result event."""
        monkeypatch.delenv("VNX_GEMINI_STREAM", raising=False)

        adapter = GeminiAdapter("T3")

        fake_result = AdapterResult(
            status="done",
            output="legacy findings",
            events=[{"type": "result", "data": "legacy findings"}],
            event_count=1,
            duration_seconds=0.5,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="gemini",
            model="gemini-2.5-flash",
        )
        adapter._execute_legacy = lambda **_kw: fake_result

        events = list(adapter.stream_events("test prompt", {}))
        assert len(events) == 1
        assert events[0]["type"] == "result"
        assert events[0]["observability_tier"] == 3

    def test_execute_returns_done_on_legacy(self, monkeypatch):
        """execute() without stream flag routes to legacy path and returns AdapterResult."""
        monkeypatch.delenv("VNX_GEMINI_STREAM", raising=False)

        adapter = GeminiAdapter("T3")
        fake_result = AdapterResult(
            status="done",
            output="legacy output",
            events=[],
            event_count=1,
            duration_seconds=0.5,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="gemini",
            model="gemini-2.5-flash",
        )
        adapter._execute_legacy = lambda **_kw: fake_result

        result = adapter.execute("test prompt", {})
        assert result.status == "done"
        assert result.provider == "gemini"


# ---------------------------------------------------------------------------
# Streaming path tests using fake subprocess (no gemini binary needed)
# ---------------------------------------------------------------------------

class TestStreamingWithFakeSubprocess:
    """Drain-stream logic tests using a fake subprocess — no real gemini needed."""

    def _patch_popen(self, monkeypatch, script: str, adapter: GeminiAdapter) -> None:
        """Patch subprocess.Popen in gemini_adapter to spawn fake script."""
        import adapters.gemini_adapter as ga_mod
        original_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            if cmd and "gemini" in str(cmd[0]):
                return original_popen(
                    [sys.executable, "-c", script],
                    stdin=kwargs.get("stdin", subprocess.PIPE),
                    stdout=kwargs.get("stdout", subprocess.PIPE),
                    stderr=kwargs.get("stderr", subprocess.PIPE),
                    start_new_session=kwargs.get("start_new_session", True),
                )
            return original_popen(cmd, **kwargs)

        monkeypatch.setattr(ga_mod.subprocess, "Popen", fake_popen)

    def test_streaming_emits_multiple_events(self, monkeypatch, event_store: EventStore):
        """VNX_GEMINI_STREAM=1 + fake subprocess emits multiple CanonicalEvents."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "stream-test-001"
        self._patch_popen(monkeypatch, _FAKE_GEMINI_STREAM_SCRIPT, adapter)

        ctx = {
            "terminal_id": "T3",
            "dispatch_id": "stream-test-001",
            "event_store": event_store,
            "chunk_timeout": 10.0,
            "total_deadline": 30.0,
        }
        events = list(adapter.stream_events("test prompt", ctx))
        assert len(events) >= 3, f"Expected ≥3 events, got {len(events)}: {events}"

    def test_all_streaming_events_have_tier_1(self, monkeypatch, event_store: EventStore):
        """All streamed events must carry observability_tier=1."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "tier-test-001"
        self._patch_popen(monkeypatch, _FAKE_GEMINI_STREAM_SCRIPT, adapter)

        ctx = {
            "terminal_id": "T3",
            "dispatch_id": "tier-test-001",
            "event_store": event_store,
            "chunk_timeout": 10.0,
            "total_deadline": 30.0,
        }
        events = list(adapter.stream_events("test prompt", ctx))
        assert events, "No events produced"
        for ev in events:
            assert ev.get("observability_tier") == 1, (
                f"Event has tier != 1: {ev}"
            )

    def test_streaming_event_types_include_init_text_complete(
        self, monkeypatch, event_store: EventStore
    ):
        """Streaming events must include init, text, and complete types."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "types-test-001"
        self._patch_popen(monkeypatch, _FAKE_GEMINI_STREAM_SCRIPT, adapter)

        ctx = {
            "terminal_id": "T3",
            "dispatch_id": "types-test-001",
            "event_store": event_store,
            "chunk_timeout": 10.0,
            "total_deadline": 30.0,
        }
        events = list(adapter.stream_events("test prompt", ctx))
        types_seen = {ev.get("event_type") for ev in events}
        assert "init" in types_seen, f"Expected 'init' in {types_seen}"
        assert "text" in types_seen, f"Expected 'text' in {types_seen}"
        assert "complete" in types_seen, f"Expected 'complete' in {types_seen}"

    def test_execute_streaming_returns_done(self, monkeypatch, event_store: EventStore):
        """execute() with VNX_GEMINI_STREAM=1 returns status='done' for rc=0."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "execute-stream-001"
        self._patch_popen(monkeypatch, _FAKE_GEMINI_STREAM_SCRIPT, adapter)

        ctx = {
            "terminal_id": "T3",
            "dispatch_id": "execute-stream-001",
            "event_store": event_store,
            "chunk_timeout": 10.0,
            "total_deadline": 30.0,
        }
        result = adapter.execute("test prompt", ctx)
        assert result.status == "done"
        assert result.provider == "gemini"
        assert result.event_count > 0

    def test_event_store_accumulates_during_stream(self, monkeypatch, event_store: EventStore):
        """EventStore must contain events after a streaming run."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "store-test-001"
        self._patch_popen(monkeypatch, _FAKE_GEMINI_STREAM_SCRIPT, adapter)

        terminal_id = "T3"
        dispatch_id = "store-test-001"
        ctx = {
            "terminal_id": terminal_id,
            "dispatch_id": dispatch_id,
            "event_store": event_store,
            "chunk_timeout": 10.0,
            "total_deadline": 30.0,
        }
        list(adapter.stream_events("test prompt", ctx))
        count = event_store.event_count(terminal_id)
        assert count > 0, (
            f"EventStore must contain events after streaming run, got count={count}"
        )


# ---------------------------------------------------------------------------
# Real gemini binary integration (skip if not installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("gemini") is None, reason="gemini binary not installed")
class TestGeminiLiveStreaming:
    """Boot real gemini subprocess with VNX_GEMINI_STREAM=1."""

    FIXTURE_PROMPT = (
        "In exactly one sentence, state what color the sky is on a clear day."
    )

    def test_stream_events_yields_events(self, monkeypatch, event_store: EventStore):
        """Real gemini subprocess emits at least one event when streaming."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T-real")
        ctx = {
            "terminal_id": "T-real",
            "dispatch_id": "real-stream-001",
            "event_store": event_store,
            "chunk_timeout": 60.0,
            "total_deadline": 120.0,
        }
        events = list(adapter.stream_events(self.FIXTURE_PROMPT, ctx))
        assert events, "No events yielded by real gemini stream"

    def test_all_real_events_have_tier_1(self, monkeypatch, event_store: EventStore):
        """All real gemini events must have observability_tier=1."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        adapter = GeminiAdapter("T-real")
        ctx = {
            "terminal_id": "T-real",
            "dispatch_id": "real-tier-001",
            "event_store": event_store,
            "chunk_timeout": 60.0,
            "total_deadline": 120.0,
        }
        events = list(adapter.stream_events(self.FIXTURE_PROMPT, ctx))
        assert events
        for ev in events:
            assert ev.get("observability_tier") == 1, (
                f"Real event has tier != 1: {ev}"
            )
