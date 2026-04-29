#!/usr/bin/env python3
"""Regression tests: register-stream routing must pass CANONICAL_STATE_DIR.

Codex finding (PR #304): serve_dashboard.py was calling handle_register_stream*()
without forwarding the dashboard's CANONICAL_STATE_DIR. The handlers fell back to
project_root.resolve_state_dir() which ignores VNX_STATE_DIR, so in any non-default
runtime/worktree the register-stream endpoints read a different file than the other
/state/* APIs — producing empty or stale data.

Fix: pass register_file=CANONICAL_STATE_DIR/"dispatch_register.ndjson" explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import serve_dashboard as sd


def _make_get_handler(path: str) -> MagicMock:
    """Create a minimal mock that satisfies DashboardHandler.do_GET."""
    handler = MagicMock()
    handler.path = path
    return handler


class TestRegisterStreamRoutingPassesCanonicalStateDir:
    """The /api/register-stream* routes must forward CANONICAL_STATE_DIR."""

    def test_archive_route_passes_canonical_register_file(self, tmp_path):
        custom_state = tmp_path / "custom_state"
        expected = custom_state / "dispatch_register.ndjson"
        captured: dict = {}

        def fake_archive(h, register_file=None):
            captured["register_file"] = register_file

        handler = _make_get_handler("/api/register-stream/archive")
        with (
            patch.object(sd, "CANONICAL_STATE_DIR", custom_state),
            patch.object(sd, "handle_register_stream_archive", fake_archive),
        ):
            sd.DashboardHandler.do_GET(handler)

        assert captured["register_file"] == expected

    def test_sse_route_passes_canonical_register_file(self, tmp_path):
        custom_state = tmp_path / "custom_state"
        expected = custom_state / "dispatch_register.ndjson"
        captured: dict = {}

        def fake_stream(h, since_ts=None, event_type_filter=None, register_file=None):
            captured["register_file"] = register_file

        handler = _make_get_handler("/api/register-stream")
        with (
            patch.object(sd, "CANONICAL_STATE_DIR", custom_state),
            patch.object(sd, "handle_register_stream", fake_stream),
        ):
            sd.DashboardHandler.do_GET(handler)

        assert captured["register_file"] == expected

    def test_sse_route_forwards_since_ts_and_event_type(self, tmp_path):
        """Query params since_ts and event_type must be forwarded unchanged."""
        custom_state = tmp_path / "state"
        captured: dict = {}

        def fake_stream(h, since_ts=None, event_type_filter=None, register_file=None):
            captured["since_ts"] = since_ts
            captured["event_type_filter"] = event_type_filter
            captured["register_file"] = register_file

        path = "/api/register-stream?since_ts=2026-04-28T10:00:00Z&event_type=gate_passed"
        handler = _make_get_handler(path)
        with (
            patch.object(sd, "CANONICAL_STATE_DIR", custom_state),
            patch.object(sd, "handle_register_stream", fake_stream),
        ):
            sd.DashboardHandler.do_GET(handler)

        assert captured["since_ts"] == "2026-04-28T10:00:00Z"
        assert captured["event_type_filter"] == "gate_passed"
        assert captured["register_file"] == custom_state / "dispatch_register.ndjson"

    def test_archive_and_sse_share_same_register_file_path(self, tmp_path):
        """Both routes must resolve to the same file under CANONICAL_STATE_DIR."""
        custom_state = tmp_path / "shared_state"
        files: list[Path] = []

        def fake_archive(h, register_file=None):
            files.append(register_file)

        def fake_stream(h, since_ts=None, event_type_filter=None, register_file=None):
            files.append(register_file)

        with (
            patch.object(sd, "CANONICAL_STATE_DIR", custom_state),
            patch.object(sd, "handle_register_stream_archive", fake_archive),
            patch.object(sd, "handle_register_stream", fake_stream),
        ):
            sd.DashboardHandler.do_GET(_make_get_handler("/api/register-stream/archive"))
            sd.DashboardHandler.do_GET(_make_get_handler("/api/register-stream"))

        assert len(files) == 2
        assert files[0] == files[1]
        assert files[0] == custom_state / "dispatch_register.ndjson"

    def test_default_register_file_matches_canonical_state_dir(self):
        """Without any override, register_file must equal the module's CANONICAL_STATE_DIR."""
        captured: dict = {}

        def fake_archive(h, register_file=None):
            captured["register_file"] = register_file

        handler = _make_get_handler("/api/register-stream/archive")
        with patch.object(sd, "handle_register_stream_archive", fake_archive):
            sd.DashboardHandler.do_GET(handler)

        assert captured["register_file"] == sd.CANONICAL_STATE_DIR / "dispatch_register.ndjson"
