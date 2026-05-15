#!/usr/bin/env python3
"""Wave 4.6 PR-4.6.4 — byte-identity tests for gemini_spawn.

Verifies that spawn_gemini (streaming path) produces the same normalized events
as calling GeminiAdapter._normalize directly on the same raw fixture events.
Uses a recorded NDJSON fixture replayed via mocked drain_stream.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_LIB / "adapters"))

from canonical_event import CanonicalEvent
from provider_spawns.gemini_spawn import (
    normalize_gemini_event,
    GeminiSpawnResult,
    spawn_gemini,
)
from adapters.gemini_adapter import GeminiAdapter


# ---------------------------------------------------------------------------
# Recorded fixture — deterministic NDJSON stream (record once, replay always)
# ---------------------------------------------------------------------------

# Each dict is a raw Gemini stream-json event as the CLI would emit.
FIXTURE_STREAM_EVENTS = [
    {"type": "session_start"},
    {"type": "text", "text": "## Security Review\n\nFound 0 critical issues."},
    {"type": "text", "text": " No SQL injection vectors detected."},
    {
        "type": "done",
        "text": "Review complete.",
        "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 80},
    },
]

EXPECTED_EVENT_TYPES = ["init", "text", "text", "complete"]

EXPECTED_PAYLOADS = [
    {"raw_type": "session_start"},
    {"text": "## Security Review\n\nFound 0 critical issues."},
    {"text": " No SQL injection vectors detected."},
    {
        "text": "Review complete.",
        "token_count": {
            "input_tokens": 200,
            "output_tokens": 80,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_nondeterministic(ev_dict: dict) -> dict:
    """Remove timestamp and event_id (generated fresh per call)."""
    return {k: v for k, v in ev_dict.items() if k not in ("timestamp", "event_id")}


def _fixture_canonical_events(terminal_id: str, dispatch_id: str) -> list:
    """Convert raw fixture dicts to CanonicalEvents via normalize_gemini_event."""
    return [normalize_gemini_event(raw, terminal_id, dispatch_id) for raw in FIXTURE_STREAM_EVENTS]


# ---------------------------------------------------------------------------
# Test: normalize_gemini_event produces expected canonical shapes
# ---------------------------------------------------------------------------

class TestNormalizerFixtureShapes:
    """The standalone normalizer maps the recorded fixture to expected shapes."""

    def test_event_types_match_expected(self):
        events = _fixture_canonical_events("T3", "d1")
        types = [e.event_type for e in events]
        assert types == EXPECTED_EVENT_TYPES

    def test_init_event_data(self):
        events = _fixture_canonical_events("T3", "d1")
        assert events[0].data == {"raw_type": "session_start"}

    def test_text_events_preserve_content(self):
        events = _fixture_canonical_events("T3", "d1")
        assert events[1].data["text"] == "## Security Review\n\nFound 0 critical issues."
        assert events[2].data["text"] == " No SQL injection vectors detected."

    def test_complete_event_includes_token_count(self):
        events = _fixture_canonical_events("T3", "d1")
        complete = events[3]
        assert complete.event_type == "complete"
        assert complete.data.get("text") == "Review complete."
        tc = complete.data.get("token_count", {})
        assert tc.get("input_tokens") == 200
        assert tc.get("output_tokens") == 80
        assert tc.get("cache_creation_tokens") == 0

    def test_all_events_have_gemini_provider(self):
        events = _fixture_canonical_events("T3", "d1")
        assert all(e.provider == "gemini" for e in events)

    def test_all_events_tier_1(self):
        events = _fixture_canonical_events("T3", "d1")
        assert all(e.observability_tier == 1 for e in events)


# ---------------------------------------------------------------------------
# Test: GeminiAdapter._normalize delegates to normalize_gemini_event (byte identity)
# ---------------------------------------------------------------------------

class TestAdapterNormalizerDelegatesIdentity:
    """GeminiAdapter._normalize and normalize_gemini_event produce identical output."""

    def test_byte_identity_per_event(self):
        """GeminiAdapter._normalize output == normalize_gemini_event output (excl. non-det fields)."""
        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "d1"

        for raw in FIXTURE_STREAM_EVENTS:
            via_adapter = adapter._normalize(raw).to_dict()
            via_standalone = normalize_gemini_event(raw, "T3", "d1").to_dict()

            assert _strip_nondeterministic(via_adapter) == _strip_nondeterministic(via_standalone), (
                f"Mismatch for raw={raw!r}:\n"
                f"  adapter:    {via_adapter}\n"
                f"  standalone: {via_standalone}"
            )


# ---------------------------------------------------------------------------
# Test: spawn_gemini streaming path fires event_writer with correct shapes
# ---------------------------------------------------------------------------

class TestSpawnGeminiStreamingEventWriter:
    """spawn_gemini fires event_writer with to_dict() of normalized canonical events."""

    def _run_spawn_with_fixture(self) -> list[dict]:
        """Run spawn_gemini with fixture events injected via mocked drain_stream."""
        import io

        class _FakeProc:
            stdin = io.BytesIO()
            stdout = io.BytesIO(b"")
            stderr = io.BytesIO(b"")
            returncode = 0
            pid = 12345

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

            def poll(self):
                return 0

        fixture_events = _fixture_canonical_events("T3", "d-fixture-01")
        collected: list[dict] = []

        def _writer(tid: str, ev_dict: dict, dispatch_id: str = "") -> None:
            collected.append(ev_dict)

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}), \
             patch(
                 "provider_spawns.gemini_spawn._GeminiNormalizerHost.drain_stream",
                 return_value=iter(fixture_events),
             ):
            spawn_gemini(
                prompt="test prompt",
                model="gemini-2.5-pro",
                dispatch_id="d-fixture-01",
                terminal_id="T3",
                event_writer=_writer,
            )

        return collected

    def test_event_count_matches_fixture(self):
        collected = self._run_spawn_with_fixture()
        assert len(collected) == len(FIXTURE_STREAM_EVENTS)

    def test_event_types_match_expected(self):
        collected = self._run_spawn_with_fixture()
        types = [ev.get("event_type") for ev in collected]
        assert types == EXPECTED_EVENT_TYPES

    def test_events_match_standalone_normalizer(self):
        """Events collected via event_writer == normalize_gemini_event().to_dict() per event."""
        collected = self._run_spawn_with_fixture()
        for i, (collected_ev, raw) in enumerate(zip(collected, FIXTURE_STREAM_EVENTS)):
            expected = normalize_gemini_event(raw, "T3", "d-fixture-01").to_dict()
            assert _strip_nondeterministic(collected_ev) == _strip_nondeterministic(expected), (
                f"Event {i} mismatch: {collected_ev!r} != {expected!r}"
            )

    def test_completion_text_extracted(self):
        """spawn_gemini extracts completion_text from the complete event."""
        import io

        class _FakeProc:
            stdin = io.BytesIO()
            stdout = io.BytesIO(b"")
            stderr = io.BytesIO(b"")
            returncode = 0
            pid = 12345

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

            def poll(self):
                return 0

        fixture_events = _fixture_canonical_events("T3", "d1")

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}), \
             patch(
                 "provider_spawns.gemini_spawn._GeminiNormalizerHost.drain_stream",
                 return_value=iter(fixture_events),
             ):
            result = spawn_gemini(
                prompt="test", model="gemini-2.5-pro",
                dispatch_id="d1", terminal_id="T3",
            )

        assert result.completion_text == "Review complete."
        assert result.returncode == 0
        assert result.timed_out is False


# ---------------------------------------------------------------------------
# Test: legacy path produces synthetic result event
# ---------------------------------------------------------------------------

class TestLegacyPathSyntheticEvent:

    def test_legacy_fires_single_result_event(self):
        """Legacy path emits a single synthetic {'type': 'result', 'data': text, ...} event."""
        import io

        class _FakeProc:
            stdin = io.BytesIO()
            stdout = io.BytesIO(b'{"response": "Legacy review findings."}')
            stderr = io.BytesIO(b"")
            returncode = 0
            pid = 12345

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

            def poll(self):
                return 0

        collected: list[dict] = []

        def _writer(tid, ev_dict, dispatch_id=""):
            collected.append(ev_dict)

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "0"}), \
             patch(
                 "provider_spawns.gemini_spawn._drain_buffered",
                 return_value=('{"response": "Legacy review findings."}', "", "ok"),
             ):
            result = spawn_gemini(
                prompt="test", model="gemini-2.5-pro",
                dispatch_id="d1", terminal_id="T3",
                event_writer=_writer,
            )

        assert len(collected) == 1
        assert collected[0]["type"] == "result"
        assert collected[0]["data"] == "Legacy review findings."
        assert result.completion_text == "Legacy review findings."
        assert result.events_written == 1
