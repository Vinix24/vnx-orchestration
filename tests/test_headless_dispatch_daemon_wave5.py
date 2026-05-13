#!/usr/bin/env python3
"""CFX-W5-2: headless_dispatch_daemon pr_id propagation tests.

Verifies that:
  1. parse_dispatch_metadata extracts pr_id from explicit 'PR-ID:' header.
  2. parse_dispatch_metadata falls back to 'PR-<digits>' pattern.
  3. _deliver adapter path includes pr_id in context dict.
  4. _deliver fallback path forwards pr_id to deliver_with_recovery.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from headless_dispatch_daemon import (
    DispatchMeta,
    _deliver,
    _extract_pr_id,
    parse_dispatch_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dispatch_file(tmp_path: Path, body: str, stem: str = "test-dispatch") -> Path:
    p = tmp_path / f"{stem}.md"
    p.write_text(body, encoding="utf-8")
    return p


def _minimal_meta(pr_id: str | None = None) -> DispatchMeta:
    return DispatchMeta(
        dispatch_id="d-daemon-test",
        target_terminal="T1",
        track="A",
        role="backend-developer",
        gate="f58-pr3",
        raw_instruction="[[TARGET:T1]]\ndo work",
        pr_id=pr_id,
    )


# ---------------------------------------------------------------------------
# _extract_pr_id
# ---------------------------------------------------------------------------

class TestExtractPrId:
    def test_explicit_pr_id_header(self):
        text = "## Meta\nPR-ID: CFX-W5-2\nsome body"
        assert _extract_pr_id(text) == "CFX-W5-2"

    def test_explicit_pr_id_strips_trailing_words(self):
        text = "PR-ID: CFX-W5-2 (Wave 5 cleanup)\nsome body"
        # regex captures up to the first whitespace
        assert _extract_pr_id(text) == "CFX-W5-2"

    def test_fallback_pr_dash_digits(self):
        text = "see PR-461 for context"
        assert _extract_pr_id(text) == "461"

    def test_fallback_pr_hash_digits(self):
        text = "merged via PR #459"
        assert _extract_pr_id(text) == "459"

    def test_explicit_wins_over_fallback(self):
        text = "PR-ID: CFX-W5-2\nSee also PR-461"
        assert _extract_pr_id(text) == "CFX-W5-2"

    def test_no_pr_id_returns_none(self):
        text = "No PR reference here at all"
        assert _extract_pr_id(text) is None


# ---------------------------------------------------------------------------
# parse_dispatch_metadata
# ---------------------------------------------------------------------------

class TestParseDispatchMetadataPrId:
    def test_parses_explicit_pr_id(self, tmp_path):
        body = "[[TARGET:T1]]\nPR-ID: CFX-W5-2\nRole: backend-developer\n"
        path = _make_dispatch_file(tmp_path, body)
        meta = parse_dispatch_metadata(path)
        assert meta is not None
        assert meta.pr_id == "CFX-W5-2"

    def test_parses_fallback_pr_number(self, tmp_path):
        body = "[[TARGET:T2]]\nRole: backend-developer\nSee PR-461 for context.\n"
        path = _make_dispatch_file(tmp_path, body)
        meta = parse_dispatch_metadata(path)
        assert meta is not None
        assert meta.pr_id == "461"

    def test_pr_id_none_when_absent(self, tmp_path):
        body = "[[TARGET:T1]]\nRole: backend-developer\nNo PR reference.\n"
        path = _make_dispatch_file(tmp_path, body)
        meta = parse_dispatch_metadata(path)
        assert meta is not None
        assert meta.pr_id is None


# ---------------------------------------------------------------------------
# _deliver — adapter context path
# ---------------------------------------------------------------------------

class TestDeliverAdapterPathPropagatesPrId:
    def test_pr_id_in_adapter_context(self, tmp_path):
        """Adapter execute() must receive pr_id in the context dict."""
        meta = _minimal_meta(pr_id="CFX-W5-2")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        captured_context: dict = {}

        mock_result = MagicMock()
        mock_result.status = "done"

        mock_adapter = MagicMock()
        mock_adapter.capabilities.return_value = set()
        mock_adapter.execute.side_effect = lambda instr, ctx: (
            captured_context.update(ctx) or mock_result
        )

        import headless_dispatch_daemon as hdd
        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch("sys.path"),
            patch.dict("sys.modules", {"adapters": MagicMock(resolve_adapter=lambda t: mock_adapter)}),
            patch.object(hdd, "_classify_dispatch", return_value=set()),
        ):
            _deliver(meta, tmp_path / "active.md", state_dir)

        assert "pr_id" in captured_context, (
            f"pr_id missing from adapter context: {captured_context}"
        )
        assert captured_context["pr_id"] == "CFX-W5-2"

    def test_pr_id_none_when_not_set(self, tmp_path):
        """When meta.pr_id is None, context still contains the key (value None)."""
        meta = _minimal_meta(pr_id=None)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        captured_context: dict = {}

        mock_result = MagicMock()
        mock_result.status = "done"

        mock_adapter = MagicMock()
        mock_adapter.capabilities.return_value = set()
        mock_adapter.execute.side_effect = lambda instr, ctx: (
            captured_context.update(ctx) or mock_result
        )

        import headless_dispatch_daemon as hdd
        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch("sys.path"),
            patch.dict("sys.modules", {"adapters": MagicMock(resolve_adapter=lambda t: mock_adapter)}),
            patch.object(hdd, "_classify_dispatch", return_value=set()),
        ):
            _deliver(meta, tmp_path / "active.md", state_dir)

        assert "pr_id" in captured_context
        assert captured_context["pr_id"] is None


# ---------------------------------------------------------------------------
# _deliver — fallback deliver_with_recovery path
# ---------------------------------------------------------------------------

class TestDeliverFallbackPathPropagatesPrId:
    def test_deliver_with_recovery_receives_pr_id(self, tmp_path):
        """When adapter import fails, deliver_with_recovery must receive pr_id."""
        meta = _minimal_meta(pr_id="461")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mock_dwr = MagicMock(return_value=True)
        mock_sd = MagicMock()
        mock_sd.deliver_with_recovery = mock_dwr

        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            # None in sys.modules causes ModuleNotFoundError (subclass of ImportError)
            patch.dict("sys.modules", {"adapters": None, "subprocess_dispatch": mock_sd}),
        ):
            _deliver(meta, tmp_path / "active.md", state_dir)

        assert mock_dwr.called, "deliver_with_recovery was not called on fallback path"
        _, kwargs = mock_dwr.call_args
        assert kwargs.get("pr_id") == "461", (
            f"Expected pr_id='461', got {kwargs.get('pr_id')!r}"
        )

    def test_deliver_with_recovery_pr_id_none_propagated(self, tmp_path):
        """When pr_id is None and adapter unavailable, None is forwarded."""
        meta = _minimal_meta(pr_id=None)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mock_dwr = MagicMock(return_value=True)
        mock_sd = MagicMock()
        mock_sd.deliver_with_recovery = mock_dwr

        with (
            patch("headless_dispatch_daemon._repo_root", return_value=tmp_path),
            patch.dict("sys.modules", {"adapters": None, "subprocess_dispatch": mock_sd}),
        ):
            _deliver(meta, tmp_path / "active.md", state_dir)

        assert mock_dwr.called
        _, kwargs = mock_dwr.call_args
        assert kwargs.get("pr_id") is None
