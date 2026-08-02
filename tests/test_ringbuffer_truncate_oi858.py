#!/usr/bin/env python3
"""Regression tests for OI-858: event ringbuffer truncation on ALL dispatch paths.

Cluster E1: the per-dispatch NDJSON ring buffer (events/T{n}.ndjson) was archived
but never truncated on several teardown paths, causing the live file to grow
unbounded until it blocked the provider lane (read_events_with_timeout chunk-timeout
of 300s). Two dispatches (sfp2, sfp4) confirmed dead from this.

Every test in this file MUST fail on origin/main and pass on the fix branch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch
from event_store import EventStore

# New constants/methods added by the OI-858 fix. These do NOT exist on origin/main.
try:
    from event_store import _OVERSIZE_FLAG_SUFFIX, _SIZE_HARD_LIMIT_BYTES
    _HAS_OI858_CONSTANTS = True
except ImportError:
    _OVERSIZE_FLAG_SUFFIX = ".oversize"
    _SIZE_HARD_LIMIT_BYTES = 50 * 1024 * 1024
    _HAS_OI858_CONSTANTS = False


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_events(tmp_path):
    """Isolated events dir."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    return events_dir


def _build_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        provider="codex",
        terminal_id="T2",
        dispatch_id="oi858-test-dispatch",
        instruction="noop",
        model="sonnet",
        max_retries=3,
        no_auto_commit=False,
        gate="",
        dispatch_paths="",
        pr_id=None,
        role=None,
        deadline_seconds=900,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _seed_live_events(events_dir: Path, terminal_id: str, dispatch_id: str, n: int = 3) -> None:
    """Write n synthetic events to the live NDJSON so clear() has content to process."""
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
    r.frontmatter_fields = MagicMock(return_value={})
    r.token_usage_measured = True
    return r


# ---------------------------------------------------------------------------
# Gap 1: _dispatch_claude_benchmark had no _event_store_safety_net
# ---------------------------------------------------------------------------


class TestBenchmarkSafetyNet:
    """On origin/main, _dispatch_claude_benchmark's finally block only called
    _finish_provider_worktree — NO _event_store_safety_net. If _emit_governance
    raised, the event file was never truncated. The fix adds the safety net so
    the finally block handles all teardown paths."""

    def test_benchmark_safety_net_when_emit_raises(self, tmp_path, tmp_events):
        """When _emit_governance raises during benchmark dispatch, the safety
        net MUST still truncate the event file."""
        terminal_id = "T2"
        dispatch_id = "oi858-benchmark-raise"
        _seed_live_events(tmp_events, terminal_id, dispatch_id, n=5)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider="claude", terminal_id=terminal_id, dispatch_id=dispatch_id)

        # Make emit_dispatch_receipt raise — this causes _emit_governance to
        # propagate the exception. On origin/main, the finally block only has
        # _finish_provider_worktree, so the event file is never truncated.
        # On the fix branch, _event_store_safety_net also runs in the finally.
        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._prepare_provider_workdir", return_value=(None, tmp_path)), \
             patch("provider_spawns.claude_spawn.spawn_claude", return_value=_make_spawn_result()), \
             patch("event_store.EventStore", return_value=store), \
             patch("governance_emit.emit_dispatch_receipt",
                   side_effect=RuntimeError("persistent write failure")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"), \
             patch.dict("os.environ", {
                 "VNX_BENCH_SEED_MATERIALIZE": "1",
                 "VNX_BENCH_CLAUDE_HEADLESS": "1",
             }):
            with pytest.raises(RuntimeError, match="persistent write failure"):
                provider_dispatch._dispatch_claude(args)

        # The key assertion: the finally block must have truncated the file
        # even though _emit_governance raised.
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0, (
            "GAP 1 (benchmark): _dispatch_claude_benchmark had no _event_store_safety_net "
            "in its finally block. When _emit_governance raises, the event file is "
            "never truncated. After fix, live file must be 0 bytes. "
            f"Got {live.stat().st_size if live.exists() else 'missing'} bytes."
        )


# ---------------------------------------------------------------------------
# Gap 2: Worktree failure early-return bypasses safety net
# ---------------------------------------------------------------------------
# On origin/main, every dispatch function that has try/except around
# _prepare_provider_workdir returns 1 WITHOUT calling _event_store_safety_net.
# The safety net lives in a finally block that's nested INSIDE a deeper try,
# so the early return skips it entirely.
#
# _dispatch_deepseek_harness and _dispatch_glm_harness had NO try/except at all
# — a RuntimeError from _prepare_provider_workdir propagated without reaching
# any finally block.


class TestWorktreeFailureSafetyNet:
    """When _prepare_provider_workdir raises RuntimeError, the dispatch function
    MUST call _event_store_safety_net before returning. On origin/main, none of
    the provider dispatch functions do this."""

    @pytest.mark.parametrize("provider,dispatch_fn,extra_patches", [
        ("codex", "_dispatch_codex", {}),
        ("gemini", "_dispatch_gemini", {}),
        ("kimi", "_dispatch_kimi", {}),
        ("litellm:deepseek", "_dispatch_litellm", {"DEEPSEEK_API_KEY": "sk-test-key"}),
        ("deepseek-harness", "_dispatch_deepseek_harness", {"DEEPSEEK_API_KEY": "sk-test-key"}),
        ("glm-harness", "_dispatch_glm_harness", {}),
    ])
    def test_worktree_failure_truncates(self, tmp_path, tmp_events, provider, dispatch_fn, extra_patches):
        terminal_id = "T2"
        safe_provider = provider.replace(":", "-")
        dispatch_id = f"oi858-wt-{safe_provider}"

        # Pre-populate the event file — this simulates leftover events from a
        # previous dispatch that was never truncated.
        _seed_live_events(tmp_events, terminal_id, dispatch_id, n=20)

        store = EventStore(events_dir=tmp_events)
        args = _build_args(provider=provider, terminal_id=terminal_id, dispatch_id=dispatch_id)

        env_patches = {k: v for k, v in extra_patches.items() if v == "sk-test-key"}
        env_dict = {}
        if "DEEPSEEK_API_KEY" in env_patches:
            env_dict["DEEPSEEK_API_KEY"] = "sk-test-key"

        with patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._prepare_provider_workdir",
                   side_effect=RuntimeError("worktree create failed")), \
             patch("event_store.EventStore", return_value=store), \
             patch.dict("os.environ", env_dict if env_dict else {}):
            fn = getattr(provider_dispatch, dispatch_fn)
            rc = fn(args)

        assert rc == 1, f"{dispatch_fn}: worktree failure should return 1"
        live = tmp_events / f"{terminal_id}.ndjson"
        assert not live.exists() or live.stat().st_size == 0, (
            f"GAP 2 ({dispatch_fn}): _event_store_safety_net was not called on "
            f"worktree failure. Live file has {live.stat().st_size if live.exists() else 'missing'} "
            f"bytes instead of 0. On origin/main, the safety net in the finally "
            f"block is unreachable from this early-return path."
        )


# ---------------------------------------------------------------------------
# Gap 3: Oversize warning had no consumer (36 warnings, zero reads)
# ---------------------------------------------------------------------------


class TestOversizeConsumer:
    """On origin/main, the oversize warning at event_store.py:150 only logs a
    warning. It fired 36 times without any consumer. The fix adds a persistent
    flag file that the dispatcher can surface without tailing the log."""

    def test_oversize_writes_flag_file(self, tmp_path, tmp_events):
        if not _HAS_OI858_CONSTANTS:
            pytest.fail(
                "GAP 3: _OVERSIZE_FLAG_SUFFIX does not exist on origin/main. "
                "The oversize flag file mechanism was never implemented."
            )

        store = EventStore(events_dir=tmp_events)
        terminal = "T1"

        payload = "x" * 100000  # 100KB per event
        for i in range(200):  # 200 * ~100KB = ~20MB > 10MB warning threshold
            store.append(terminal, {
                "type": "text",
                "data": {"msg": f"event-{i}", "padding": payload},
                "dispatch_id": "oi858-oversize-test",
            }, dispatch_id="oi858-oversize-test")

        flag_path = tmp_events / f"{terminal}{_OVERSIZE_FLAG_SUFFIX}"
        assert flag_path.exists(), (
            "GAP 3: oversize flag file was not created. "
            f"Expected {flag_path} to exist. On origin/main, the warning "
            "was log-only and fired 36 times without any consumer."
        )

    def test_oversize_flag_cleared_on_teardown(self, tmp_path, tmp_events):
        if not _HAS_OI858_CONSTANTS:
            pytest.fail("GAP 3: oversize flag constants not available on origin/main.")

        store = EventStore(events_dir=tmp_events)
        terminal = "T1"

        payload = "x" * 100000
        for i in range(200):
            store.append(terminal, {
                "type": "text",
                "data": {"msg": f"event-{i}", "padding": payload},
                "dispatch_id": "oi858-clear-flag",
            }, dispatch_id="oi858-clear-flag")

        flag_path = tmp_events / f"{terminal}{_OVERSIZE_FLAG_SUFFIX}"
        assert flag_path.exists(), "Flag must be created by oversize condition"

        store.clear(terminal, archive_dispatch_id="oi858-clear-flag")
        assert not flag_path.exists(), (
            "Flag file must be removed on clear() — stale flags would cause false alarms"
        )

    def test_oversize_flags_method(self, tmp_path, tmp_events):
        if not _HAS_OI858_CONSTANTS:
            pytest.fail("GAP 3: oversize_flags() method does not exist on origin/main.")

        store = EventStore(events_dir=tmp_events)
        terminal = "T1"

        assert store.oversize_flags() == [], "No flags initially"

        payload = "x" * 100000
        for i in range(200):
            store.append(terminal, {
                "type": "text",
                "data": {"msg": f"event-{i}", "padding": payload},
                "dispatch_id": "oi858-flags-method",
            }, dispatch_id="oi858-flags-method")

        flags = store.oversize_flags()
        assert len(flags) > 0, "oversize_flags() must return flag files"

        store.clear(terminal, archive_dispatch_id="oi858-flags-method")
        assert store.oversize_flags() == [], "oversize_flags() must be empty after clear"


# ---------------------------------------------------------------------------
# Gap 4: No hard upper bound — file grows unbounded without teardown
# ---------------------------------------------------------------------------


class TestHardLimit:
    """On origin/main, there is no hard upper bound — if teardown never runs,
    the file grows until it blocks the lane. The fix adds a hard limit that
    triggers auto-truncation during append(), with emergency archiving."""

    def test_hard_limit_auto_truncates(self, tmp_path, tmp_events):
        if not _HAS_OI858_CONSTANTS:
            pytest.fail(
                "GAP 4: _SIZE_HARD_LIMIT_BYTES does not exist on origin/main. "
                "There is no hard upper bound — the file grows unbounded."
            )

        import event_store as es_module

        store = EventStore(events_dir=tmp_events)
        terminal = "T1"
        dispatch_id = "oi858-hard-limit"

        original_limit = es_module._SIZE_HARD_LIMIT_BYTES
        try:
            es_module._SIZE_HARD_LIMIT_BYTES = 500_000  # 500KB for test speed

            payload = "x" * 50000  # 50KB per event
            for i in range(30):  # 30 * ~50KB = ~1.5MB > 500KB limit
                store.append(terminal, {
                    "type": "text",
                    "data": {"msg": f"event-{i}", "padding": payload},
                    "dispatch_id": dispatch_id,
                }, dispatch_id=dispatch_id)

            live_path = tmp_events / f"{terminal}.ndjson"
            live_size = live_path.stat().st_size if live_path.exists() else 0
            assert live_size < es_module._SIZE_HARD_LIMIT_BYTES * 2, (
                f"GAP 4: live file grew to {live_size} bytes despite hard limit "
                f"of {es_module._SIZE_HARD_LIMIT_BYTES}. The hard upper bound must "
                "prevent unbounded growth even without teardown."
            )

            # Emergency archive must exist
            archive_dir = tmp_events / "archive" / terminal
            archives = list(archive_dir.glob("emergency-*.ndjson"))
            assert len(archives) > 0, (
                "Emergency archive must be created when hard limit triggers."
            )
        finally:
            es_module._SIZE_HARD_LIMIT_BYTES = original_limit

    def test_file_stays_under_limit_across_many_dispatches(self, tmp_path, tmp_events):
        """20 dispatches without clear() must not exceed the hard limit."""
        if not _HAS_OI858_CONSTANTS:
            pytest.fail("GAP 4: no hard limit on origin/main — file grows unbounded.")

        import event_store as es_module

        original_limit = es_module._SIZE_HARD_LIMIT_BYTES
        try:
            es_module._SIZE_HARD_LIMIT_BYTES = 100_000  # 100KB for test speed

            store = EventStore(events_dir=tmp_events)
            terminal = "T3"

            for d in range(20):
                for i in range(50):
                    store.append(terminal, {
                        "type": "text",
                        "data": {"msg": f"dispatch-{d}-event-{i}", "padding": "y" * 200},
                        "dispatch_id": f"oi858-many-{d}",
                    }, dispatch_id=f"oi858-many-{d}")

                live_path = tmp_events / f"{terminal}.ndjson"
                live_size = live_path.stat().st_size if live_path.exists() else 0
                assert live_size < es_module._SIZE_HARD_LIMIT_BYTES * 3, (
                    f"GAP 4: after {d+1} dispatches without clear(), live file "
                    f"is {live_size} bytes (limit: {es_module._SIZE_HARD_LIMIT_BYTES}). "
                    "Without the hard limit, this file would grow unbounded forever."
                )
        finally:
            es_module._SIZE_HARD_LIMIT_BYTES = original_limit


# ---------------------------------------------------------------------------
# Gap 5: _emit_governance clear() failure silently swallowed at DEBUG level
# ---------------------------------------------------------------------------
# This isn't a gap that causes the file to not be truncated (the safety net
# catches it), but the DEBUG-level log means the operator never knows clear()
# failed. The fix upgrades it to WARNING.
#
# This is an observability fix — the test verifies the log level change.
# It does NOT fail on origin/main (the safety net still handles the clear),
# so we skip it here. The behavioral proof is in Gap 1-4 above.
