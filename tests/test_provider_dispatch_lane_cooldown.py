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
    a ``KimiSpawnResult.error`` string invented by this test file. ``_kimi_403_result``
    below does not hand-type that string (an earlier version of this fixture did,
    and swapped the ``msg``/``raw`` fields relative to what production actually
    emits — a claim that was never checked against the real code). It instead
    drives ``provider_spawns.kimi_spawn.spawn_kimi`` for real, the same way
    ``tests/test_kimi_spawn.py::TestNonJsonAnd403Handling._run_with_raw_bytes``
    does: only ``_start_kimi_subprocess`` is mocked (to hand the drainer a raw
    403 JSON body on a pipe), so ``normalize_kimi_event``, ``_consume_kimi_stream``,
    and ``_finalize_kimi_result`` all run for real and produce whatever string
    production produces today — this fixture cannot drift from production
    because it does not restate production's logic, it calls it."""

    def _kimi_403_result(self):
        import io
        import os
        import threading

        from unittest.mock import MagicMock

        from provider_spawns.kimi_spawn import spawn_kimi

        raw = b'{"status": 403, "message": "forbidden"}\n'
        read_fd, write_fd = os.pipe()

        def _writer():
            try:
                os.write(write_fd, raw)
            finally:
                os.close(write_fd)

        writer_thread = threading.Thread(target=_writer, daemon=True)
        writer_thread.start()

        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.poll.return_value = 1
        fake_proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
        fake_proc.stderr = io.BytesIO(b"")
        fake_proc.wait = MagicMock(return_value=1)
        fake_proc.kill = MagicMock()

        try:
            with patch("provider_spawns.kimi_spawn._start_kimi_subprocess") as mock_start:
                mock_start.return_value = (fake_proc, None)
                result = spawn_kimi("prompt", dispatch_id="d-kimi-403-fixture", terminal_id="T1")
        finally:
            writer_thread.join(timeout=5)
        return result

    def test_kimi_403_marks_kimi_lane_unavailable(self):
        """Assert on the cooldown write itself (file + contents), never on
        ``lane_available``'s compound verdict — that function also declines a
        lane for a missing CLI/env var, so on a CI runner without the kimi
        CLI on PATH the assertion would pass for the wrong reason (or fail
        outright, as it did before this fix: the CLI-absence reason string
        doesn't contain "cooldown")."""
        import json
        import math

        import provider_dispatch as pd
        from incident_taxonomy import IncidentClass
        from providers.smart_router import availability

        _emit_failure("kimi", "kimi-k3", self._kimi_403_result(), "d-kimi-403")

        state_dir = pd._resolve_state_dir()
        cooldown_path = availability._cooldown_file(state_dir, "kimi")
        assert cooldown_path.is_file(), (
            f"expected a router-lane cooldown file at {cooldown_path}"
        )
        data = json.loads(cooldown_path.read_text(encoding="utf-8"))
        assert data["lane"] == "kimi"
        assert data["failure_class"] == IncidentClass.PROVIDER_QUOTA_EXHAUSTED.value
        # kimi reports this 403 as `quota_or_auth` and cannot itself tell quota
        # and auth apart; OI-940 identifies this specific case as quota
        # exhaustion, not a broken key. A recoverable state must never
        # permanently disable the lane, so the cooldown has to be both
        # strictly positive (the lane is out for a while) and finite (it will
        # come back on its own) — asserting only "> 0" would let inf through.
        remaining = availability.lane_cooldown_remaining("kimi", state_dir=state_dir)
        assert remaining > 0
        assert math.isfinite(remaining)

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
        """Same principle as the kimi 403 test above: assert on the cooldown
        file itself, not on ``lane_available``'s compound verdict."""
        import json

        import provider_dispatch as pd
        from incident_taxonomy import IncidentClass
        from providers.smart_router import availability

        _emit_failure(
            "deepseek-harness", "deepseek-v4-pro",
            self._deepseek_credit_exhausted_result(), "d-deepseek-402",
        )

        state_dir = pd._resolve_state_dir()
        cooldown_path = availability._cooldown_file(state_dir, "deepseek-harness")
        assert cooldown_path.is_file(), (
            f"expected a router-lane cooldown file at {cooldown_path}"
        )
        data = json.loads(cooldown_path.read_text(encoding="utf-8"))
        assert data["lane"] == "deepseek-harness"
        assert data["failure_class"] == IncidentClass.PROVIDER_QUOTA_EXHAUSTED.value
        assert availability.lane_cooldown_remaining("deepseek-harness", state_dir=state_dir) > 0


class TestNonProviderFailureWritesNoCooldown:
    """A failure that is NOT a provider quota/auth/credit signal (a plain
    non-zero exit with no recognizable reason — ``failure_classification``
    buckets this as ``"unknown"``) must never cool a lane down; false
    positives here would take a healthy lane out of rotation."""

    def test_generic_kimi_exit_failure_writes_no_cooldown(self):
        """Assert the ABSENCE of a cooldown file, not that ``lane_available``
        returns True — on a runner without the kimi CLI on PATH,
        ``lane_available`` returns False regardless of cooldown state (missing
        CLI is checked before cooldown), so that assertion measured whether
        kimi was installed, never whether this code wrote a cooldown."""
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult
        from providers.smart_router import availability

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
        cooldown_path = availability._cooldown_file(state_dir, "kimi")
        assert not cooldown_path.exists(), (
            f"a non-provider failure must never write a cooldown file, found {cooldown_path}"
        )
        assert availability.lane_cooldown_remaining("kimi", state_dir=state_dir) == 0.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
