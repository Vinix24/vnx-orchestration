#!/usr/bin/env python3
"""Regression tests: per-terminal NDJSON ring-buffer rotation for non-Claude providers.

H2 sweep update: archive+clear moved from dispatch finally-block into _emit_governance
so the receipt carries an events_path pointer to the archive before the live file is
cleared.  These tests verify the rotation contract still holds end-to-end by mocking
the governance sinks (receipt + report writes) while letting _emit_governance run for
real — which exercises the archive→receipt→clear sequence.

Verifies:
- Live T{n}.ndjson is 0 bytes (or absent) after dispatch completes
- Archive .vnx-data/events/archive/T{n}/<dispatch_id>.ndjson has content
- All 4 providers: codex, gemini, kimi, litellm
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_events(tmp_path):
    """Isolated events dir + archive sub-dir."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    return events_dir


def _build_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        provider="codex",
        terminal_id="T2",
        dispatch_id="central-1-event-rotate-test",
        instruction="noop",
        model="sonnet",
        max_retries=3,
        no_auto_commit=False,
        gate="",
        dispatch_paths="",
        pr_id=None,
        role=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _seed_live_events(events_dir: Path, terminal_id: str, dispatch_id: str, n: int = 3) -> None:
    """Write n synthetic events to the live NDJSON so clear() has content to archive."""
    live = events_dir / f"{terminal_id}.ndjson"
    live.parent.mkdir(parents=True, exist_ok=True)
    with open(live, "w") as f:
        for i in range(n):
            f.write(json.dumps({"type": "text", "sequence": i + 1, "dispatch_id": dispatch_id}) + "\n")


def _make_spawn_result(*, error=None, timed_out=False, returncode=0, event_writer_failures=0):
    r = MagicMock()
    r.error = error
    r.timed_out = timed_out
    r.returncode = returncode
    r.event_writer_failures = event_writer_failures
    r.completion_text = "ok"
    r.token_usage = None
    return r


# Patch targets for governance sinks so tests don't need real DB / receipts.
# We mock at the governance_emit module level (where the functions are defined),
# not at the provider_dispatch import level, so _emit_governance can run for real.
_GOVERNANCE_PATCHES = [
    patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake_receipt.ndjson")),
    patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake_report.md")),
    patch("provider_costs.emit_provider_cost"),
    patch("provider_dispatch._record_provider_metadata"),
]


def _apply_governance_patches(fn):
    """Decorator to apply all governance patches to a test."""
    import functools
    for p in reversed(_GOVERNANCE_PATCHES):
        fn = p(fn)
    return fn


# ---------------------------------------------------------------------------
# _dispatch_codex
# ---------------------------------------------------------------------------


class TestCodexEventRotation:
    def test_live_file_truncated_after_success(self, tmp_path, tmp_events):
        from event_store import EventStore

        terminal_id = "T2"
        dispatch_id = "codex-rotate-ok"
        _seed_live_events(tmp_events, terminal_id, dispatch_id)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="codex", terminal_id=terminal_id, dispatch_id=dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_spawns.codex_spawn.spawn_codex", return_value=_make_spawn_result()), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_codex(args)

        assert rc == 0
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0, "live file must be empty after dispatch"
        archive = tmp_events / "archive" / terminal_id / f"{dispatch_id}.ndjson"
        assert archive.exists() and archive.stat().st_size > 0, "archive must have content"

    def test_live_file_truncated_after_failure(self, tmp_path, tmp_events):
        from event_store import EventStore

        terminal_id = "T2"
        dispatch_id = "codex-rotate-fail"
        _seed_live_events(tmp_events, terminal_id, dispatch_id)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="codex", terminal_id=terminal_id, dispatch_id=dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_spawns.codex_spawn.spawn_codex", return_value=_make_spawn_result(returncode=1)), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_codex(args)

        assert rc == 1
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0


# ---------------------------------------------------------------------------
# _dispatch_gemini
# ---------------------------------------------------------------------------


class TestGeminiEventRotation:
    def test_live_file_truncated_after_success(self, tmp_path, tmp_events):
        from event_store import EventStore

        terminal_id = "T2"
        dispatch_id = "gemini-rotate-ok"
        _seed_live_events(tmp_events, terminal_id, dispatch_id)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="gemini", terminal_id=terminal_id, dispatch_id=dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=_make_spawn_result()), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_gemini(args)

        assert rc == 0
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0
        archive = tmp_events / "archive" / terminal_id / f"{dispatch_id}.ndjson"
        assert archive.exists() and archive.stat().st_size > 0

    def test_live_file_truncated_after_error(self, tmp_path, tmp_events):
        from event_store import EventStore

        terminal_id = "T2"
        dispatch_id = "gemini-rotate-err"
        _seed_live_events(tmp_events, terminal_id, dispatch_id)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="gemini", terminal_id=terminal_id, dispatch_id=dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=_make_spawn_result(error="timeout")), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_gemini(args)

        assert rc == 1
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0


# ---------------------------------------------------------------------------
# _dispatch_kimi
# ---------------------------------------------------------------------------


class TestKimiEventRotation:
    def test_live_file_truncated_after_success(self, tmp_path, tmp_events):
        from event_store import EventStore

        terminal_id = "T2"
        dispatch_id = "kimi-rotate-ok"
        _seed_live_events(tmp_events, terminal_id, dispatch_id)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="kimi", terminal_id=terminal_id, dispatch_id=dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=_make_spawn_result()), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_kimi(args)

        assert rc == 0
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0
        archive = tmp_events / "archive" / terminal_id / f"{dispatch_id}.ndjson"
        assert archive.exists() and archive.stat().st_size > 0

    def test_live_file_truncated_after_failure(self, tmp_path, tmp_events):
        from event_store import EventStore

        terminal_id = "T2"
        dispatch_id = "kimi-rotate-fail"
        _seed_live_events(tmp_events, terminal_id, dispatch_id)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="kimi", terminal_id=terminal_id, dispatch_id=dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=_make_spawn_result(returncode=1)), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_kimi(args)

        assert rc == 1
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0


# ---------------------------------------------------------------------------
# _dispatch_litellm
# ---------------------------------------------------------------------------


class TestLitellmEventRotation:
    def _store_and_args(self, tmp_events, terminal_id, dispatch_id):
        from event_store import EventStore

        _seed_live_events(tmp_events, terminal_id, dispatch_id)
        store = EventStore(events_dir=tmp_events)
        args = _build_args(
            provider="litellm:deepseek",
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
        )
        return store, args

    def test_live_file_truncated_after_success(self, tmp_path, tmp_events):
        terminal_id = "T2"
        dispatch_id = "litellm-rotate-ok"
        store, args = self._store_and_args(tmp_events, terminal_id, dispatch_id)

        mock_result = _make_spawn_result()

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_deepseek_model", return_value="deepseek/deepseek-v4-pro"), \
             patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=mock_result), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"), \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            rc = provider_dispatch._dispatch_litellm(args)

        assert rc == 0
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0
        archive = tmp_events / "archive" / terminal_id / f"{dispatch_id}.ndjson"
        assert archive.exists() and archive.stat().st_size > 0

    def test_live_file_truncated_after_failure(self, tmp_path, tmp_events):
        terminal_id = "T2"
        dispatch_id = "litellm-rotate-fail"
        store, args = self._store_and_args(tmp_events, terminal_id, dispatch_id)

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_deepseek_model", return_value="deepseek/deepseek-v4-pro"), \
             patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=_make_spawn_result(error="api error")), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/fake.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"), \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            rc = provider_dispatch._dispatch_litellm(args)

        assert rc == 1
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0
