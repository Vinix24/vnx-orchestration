"""Tests for the OI-1263 router-lane cooldown write, at the real observation
point: the provider-lane dispatch itself (``provider_dispatch._emit_governance``
via ``_maybe_record_provider_lane_cooldown``).

Context: two independent reviewers BLOCKed dispatch/20260817-g1p4-cooldown
because the cooldown write was wired onto ``WorkflowSupervisor.handle_incident``,
gated on a PROVIDER_* incident class that no production caller ever raises (the
only real callers, in ``subprocess_health_monitor.py``, only ever raise
PROCESS_CRASH/TERMINAL_UNRESPONSIVE with ``component="subprocess_adapter"`` —
not a lane name ``_LANE_CHECKS`` even knows). The prior test suite
(``tests/test_workflow_supervisor.py::TestRouterLaneCooldown``, now replaced)
fed ``handle_incident`` a fabricated ``component="deepseek-harness"`` plus a
hand-picked provider incident class directly — proving the write-then-read
wiring works in isolation, never that production could reach it. That gap is
exactly what let nine kimi dispatches die on the same 403 between
2026-08-16T11:03:24Z and 2026-08-16T11:11:32Z with the lane never marked out:
the next dispatch re-entered the same dead lane every time.

These tests fail a provider call the way it actually fails in production — via
the event-payload shape ``failure_classification.classify_failure_safe`` (and,
upstream of it, the per-lane spawn parser) already produces, never a
synthesized 403 string invented here — and then prove a cooldown lands on the
LANE THE DISPATCH ACTUALLY USED, and that the next routing decision skips it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import pytest


def _build_args(dispatch_id: str) -> "argparse.Namespace":
    import argparse

    return argparse.Namespace(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        instruction="do the thing",
        pr_id=None,
        role=None,
        mandate_id=None,
        deadline_seconds=None,
        session_id=None,
        project_id=None,
    )


def _emit_failure(provider: str, model: str, result, dispatch_id: str) -> None:
    """Drive the real ``_emit_governance`` call site with a failing result,
    mocking only the receipt/report writers (same pattern as the existing
    provider-dispatch test suite) — everything else, including the new
    OI-1263 cooldown hook, runs for real."""
    import provider_dispatch as pd

    args = _build_args(dispatch_id)
    start_time = datetime.now(timezone.utc)
    end_time = datetime.now(timezone.utc)
    with patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
         patch("governance_emit.emit_unified_report") as mock_report:
        mock_receipt.return_value = Path("/tmp/receipts.ndjson")
        mock_report.return_value = Path("/tmp/report.md")
        pd._emit_governance(args, provider, model, result, start_time, end_time, "failure")


class TestKimi403WritesRealLaneCooldown:
    """The concrete measured incident: kimi returns an HTTP 403 (quota/auth),
    which ``kimi_spawn.normalize_kimi_event`` turns into an error event whose
    text carries ``http_status=403`` — never a clean top-level 403, and never
    a ``KimiSpawnResult.error`` string invented by this test file; this is the
    literal format ``_finalize_kimi_result`` produces when ``errors_captured``
    is non-empty (see kimi_spawn.py ``_consume_kimi_stream``/``normalize_kimi_event``)."""

    def _kimi_403_result(self):
        from provider_spawns.kimi_spawn import KimiSpawnResult

        return KimiSpawnResult(
            returncode=1,
            completion_text="",
            events_written=2,
            session_id=None,
            timed_out=False,
            error=(
                "[quota_or_auth] provider=kimi reason=quota_or_auth "
                "msg='forbidden' raw='{\"status\": 403, \"message\": \"forbidden\"}'"
            ),
        )

    def test_kimi_403_marks_kimi_lane_unavailable(self):
        import provider_dispatch as pd
        from providers.smart_router.availability import lane_available

        _emit_failure("kimi", "kimi-k3", self._kimi_403_result(), "d-kimi-403")

        state_dir = pd._resolve_state_dir()
        ok, reason = lane_available("kimi", env={}, state_dir=state_dir)
        assert ok is False
        assert "cooldown" in reason

    def test_next_routing_decision_skips_kimi_for_codex(self, monkeypatch):
        """After the cooldown write, the next routing decision for a
        kimi-primary tier falls through to its codex fallback."""
        import shutil as _shutil

        import provider_dispatch as pd
        from providers.smart_router.tier_routing import resolve_tier_route

        monkeypatch.setattr(_shutil, "which", lambda name: "/usr/local/bin/kimi")

        _emit_failure("kimi", "kimi-k3", self._kimi_403_result(), "d-kimi-403-route")

        state_dir = pd._resolve_state_dir()
        route = resolve_tier_route("kimi-k3", env={}, state_dir=state_dir)
        assert route.provider == "codex"
        assert "kimi unavailable" in route.reason


class TestDeepSeekCreditExhaustionWritesRealLaneCooldown:
    """The measured 2026-08-15 incident: OpenRouter/DeepSeek returns the
    credit-exhaustion reason as ordinary completion TEXT (``"API Error: 402
    Insufficient Balance"``), not as ``result.error`` and not as a clean 402 —
    see lege-provider-credits-lezen-als-server-error. ``result.error`` stays
    None here on purpose: that is exactly the shape the harness produces, and
    is why the fix must read ``completion_text``, not ``stderr``."""

    def _deepseek_credit_exhausted_result(self):
        from provider_spawns.deepseek_harness_spawn import DeepSeekHarnessSpawnResult

        return DeepSeekHarnessSpawnResult(
            returncode=1,
            completion={"text": "API Error: 402 Insufficient Balance"},
            events_written=1,
            session_id=None,
            timed_out=False,
            error=None,
        )

    def test_deepseek_402_as_completion_text_marks_lane_unavailable(self):
        import provider_dispatch as pd
        from providers.smart_router.availability import lane_available

        _emit_failure(
            "deepseek-harness", "deepseek-v4-pro",
            self._deepseek_credit_exhausted_result(), "d-deepseek-402",
        )

        state_dir = pd._resolve_state_dir()
        ok, reason = lane_available(
            "deepseek-harness", env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir,
        )
        assert ok is False
        assert "cooldown" in reason


class TestNonProviderFailureWritesNoCooldown:
    """A failure that is NOT a provider quota/auth/credit signal (a plain
    non-zero exit with no recognizable reason — ``failure_classification``
    buckets this as ``"unknown"``) must never cool a lane down; false
    positives here would take a healthy lane out of rotation."""

    def test_generic_kimi_exit_failure_writes_no_cooldown(self):
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult
        from providers.smart_router.availability import lane_available

        result = KimiSpawnResult(
            returncode=1,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            error="kimi exited with code 1",
        )
        _emit_failure("kimi", "kimi-k3", result, "d-kimi-generic-failure")

        state_dir = pd._resolve_state_dir()
        ok, _ = lane_available("kimi", env={}, state_dir=state_dir)
        assert ok is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
