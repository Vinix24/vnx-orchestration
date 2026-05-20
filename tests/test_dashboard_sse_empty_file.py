#!/usr/bin/env python3
"""Test: SSE handler does not 404 when the live NDJSON file is empty (0 bytes).

Dispatch-ID: 20260520-1450-dashboard-phase-a
"""

import sys
import time
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler."""

    def __init__(self, break_on_flush: bool = True):
        self.wfile = MagicMock()
        # Raise BrokenPipeError on flush so the polling loop exits immediately
        if break_on_flush:
            self.wfile.flush.side_effect = BrokenPipeError
        self.response_code: int | None = None
        self.headers_sent: list[tuple[str, str]] = []
        self._headers_ended = False

    def send_response(self, code) -> None:
        self.response_code = int(code)

    def send_header(self, key: str, value: str) -> None:
        self.headers_sent.append((key, value))

    def end_headers(self) -> None:
        self._headers_ended = True


def _make_mock_store(event_count: int = 0):
    store = MagicMock()
    store.event_count.return_value = event_count
    store.tail.return_value = iter([])
    return store


class TestSSEHandlerEmptyFile(unittest.TestCase):
    """SSE endpoint stays open (200 OK) even when event store is empty."""

    def _fresh_module(self, mock_store):
        """Import api_agent_stream with a patched _store."""
        if "api_agent_stream" in sys.modules:
            del sys.modules["api_agent_stream"]
        with patch("event_store.EventStore", return_value=mock_store):
            import api_agent_stream  # noqa: PLC0415
            # Replace module-level _store so the handler uses our mock
            api_agent_stream._store = mock_store
        return api_agent_stream

    def test_no_404_on_empty_store(self):
        """When event_count==0, handle_agent_stream must NOT return 404."""
        mock_store = _make_mock_store(event_count=0)
        mod = self._fresh_module(mock_store)
        handler = FakeHandler(break_on_flush=True)

        mod.handle_agent_stream(handler, "T1", None)

        self.assertNotEqual(
            handler.response_code,
            int(HTTPStatus.NOT_FOUND),
            "SSE handler must not return 404 for empty event store",
        )

    def test_sse_headers_sent_on_empty_store(self):
        """With empty store, SSE response headers are still emitted before poll loop."""
        mock_store = _make_mock_store(event_count=0)
        mod = self._fresh_module(mock_store)
        handler = FakeHandler(break_on_flush=True)

        mod.handle_agent_stream(handler, "T1", None)

        self.assertEqual(
            handler.response_code,
            int(HTTPStatus.OK),
            "SSE handler must send 200 OK before entering poll loop",
        )
        content_types = [v for k, v in handler.headers_sent if k == "Content-Type"]
        self.assertIn(
            "text/event-stream",
            content_types,
            "SSE Content-Type header must be sent even for empty event store",
        )

    def test_invalid_terminal_still_bad_request(self):
        """Unknown terminal names still get 400 (bad request)."""
        mock_store = _make_mock_store(event_count=0)
        mod = self._fresh_module(mock_store)
        handler = FakeHandler(break_on_flush=False)

        mod.handle_agent_stream(handler, "TX_INVALID", None)

        self.assertEqual(
            handler.response_code,
            int(HTTPStatus.BAD_REQUEST),
            "Unknown terminal must return 400 BAD_REQUEST",
        )


if __name__ == "__main__":
    unittest.main()
