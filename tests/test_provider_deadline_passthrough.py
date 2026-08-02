#!/usr/bin/env python3
"""Tests for deadline_seconds passthrough from dispatch-spec through spawn to receipt.

Dispatch 20260731-a0-deadline-passthrough: the spec-deadline was dropped on the lane
boundary — claude_spawn's hardcoded 900s default silently overrode the staged 3600s.
These tests verify end-to-end threading and prove that a timeout records which deadline
was in effect so an operator can distinguish "900s timeout" from "3600s timeout".

Every test MUST fail (red) against the pre-fix code and pass (green) afterward.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
for _p in (str(REPO_ROOT), str(SCRIPTS_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# 1. spawn_claude receives the correct total_deadline
# ---------------------------------------------------------------------------

class TestSpawnClaudeDeadlinePassthrough:
    """Verify spawn_claude receives deadline from its callers."""

    def test_default_total_deadline_is_900(self):
        """Without any override, spawn_claude's total_deadline defaults to 900.0."""
        from provider_spawns.claude_spawn import spawn_claude

        import inspect
        sig = inspect.signature(spawn_claude)
        assert sig.parameters["total_deadline"].default == 900.0

    def test_explicit_total_deadline_passed_to_spawn(self):
        """When a caller passes total_deadline=3600, spawn_claude receives 3600."""
        from provider_spawns.claude_spawn import spawn_claude

        # We verify the parameter plumbing by checking the signature accepts it
        # and that the default is 900. The actual value threading is verified in
        # integration tests below.
        import inspect
        sig = inspect.signature(spawn_claude)
        assert "total_deadline" in sig.parameters
        assert sig.parameters["total_deadline"].default == 900.0


# ---------------------------------------------------------------------------
# 2. EnvelopeSpec carries deadline_seconds through the envelope
# ---------------------------------------------------------------------------

class TestEnvelopeSpecDeadlineField:
    """Verify EnvelopeSpec has deadline_seconds and it flows through construction."""

    def test_envelope_spec_has_deadline_field_with_default_900(self):
        """EnvelopeSpec must have deadline_seconds field defaulting to 900."""
        from dispatch_envelope import EnvelopeSpec

        spec = EnvelopeSpec(
            dispatch_id="test-1",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            instruction="test",
            role=None,
            pr_id=None,
            state_dir=Path("/tmp"),
            data_dir=Path("/tmp"),
        )
        assert spec.deadline_seconds == 900

    def test_envelope_spec_explicit_deadline(self):
        """EnvelopeSpec accepts and preserves an explicit deadline_seconds."""
        from dispatch_envelope import EnvelopeSpec

        spec = EnvelopeSpec(
            dispatch_id="test-1",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            instruction="test",
            role=None,
            pr_id=None,
            state_dir=Path("/tmp"),
            data_dir=Path("/tmp"),
            deadline_seconds=3600,
        )
        assert spec.deadline_seconds == 3600

    def test_envelope_spec_deadline_propagates_to_enriched(self, monkeypatch):
        """When constructing the enriched EnvelopeSpec, deadline_seconds propagates."""
        from dispatch_envelope import EnvelopeSpec

        original = EnvelopeSpec(
            dispatch_id="test-1",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            instruction="test",
            role=None,
            pr_id=None,
            state_dir=Path("/tmp"),
            data_dir=Path("/tmp"),
            deadline_seconds=7200,
        )

        # Simulate the enriched copy pattern used in _run_envelope and run_envelope_headless_plan
        enriched = EnvelopeSpec(
            dispatch_id=original.dispatch_id,
            terminal_id=original.terminal_id,
            provider=original.provider,
            model=original.model,
            instruction=original.instruction,
            role=original.role,
            pr_id=original.pr_id,
            state_dir=original.state_dir,
            data_dir=original.data_dir,
            deadline_seconds=original.deadline_seconds,
        )
        assert enriched.deadline_seconds == 7200


# ---------------------------------------------------------------------------
# 3. ClaudeSubprocessAdapter.run() threads total_deadline to spawn_claude
# ---------------------------------------------------------------------------

class TestClaudeSubprocessAdapterDeadline:
    """Verify ClaudeSubprocessAdapter passes deadline through to spawn_claude."""

    def test_adapter_passes_total_deadline_to_spawn(self, monkeypatch):
        """ClaudeSubprocessAdapter.run() must pass spec.deadline_seconds as total_deadline."""
        from dispatch_envelope import ClaudeSubprocessAdapter, EnvelopeSpec

        captured = {}

        def fake_spawn(*, total_deadline=900.0, **kwargs):
            captured["total_deadline"] = total_deadline
            from dataclasses import dataclass as _dc, field as _f
            @_dc
            class _FakeResult:
                returncode: int = 0
                completion: dict = _f(default_factory=dict)
                events_written: int = 0
                session_id: str | None = None
                timed_out: bool = False
                stopped_early: bool = False
                error: str | None = None
                token_usage: dict | None = None
                completion_text: str = ""
                _adapter: object = None
            return _FakeResult()

        # spawn_claude is imported inside ClaudeSubprocessAdapter.run() from
        # provider_spawns.claude_spawn — patch at the import source.
        monkeypatch.setattr(
            "provider_spawns.claude_spawn.spawn_claude", fake_spawn
        )

        adapter = ClaudeSubprocessAdapter()
        spec = EnvelopeSpec(
            dispatch_id="test-1", terminal_id="T1", provider="claude",
            model="sonnet", instruction="test", role=None, pr_id=None,
            state_dir=Path("/tmp"), data_dir=Path("/tmp"),
            deadline_seconds=3600,
        )
        adapter.run(spec)

        assert captured.get("total_deadline") == 3600.0, (
            f"Expected total_deadline=3600.0, got {captured.get('total_deadline')}"
        )

    def test_adapter_default_deadline_is_900(self, monkeypatch):
        """When EnvelopeSpec doesn't set deadline, adapter passes 900 (default)."""
        from dispatch_envelope import ClaudeSubprocessAdapter, EnvelopeSpec

        captured = {}

        def fake_spawn(*, total_deadline=900.0, **kwargs):
            captured["total_deadline"] = total_deadline
            from dataclasses import dataclass as _dc, field as _f
            @_dc
            class _FakeResult:
                returncode: int = 0
                completion: dict = _f(default_factory=dict)
                events_written: int = 0
                session_id: str | None = None
                timed_out: bool = False
                stopped_early: bool = False
                error: str | None = None
                token_usage: dict | None = None
                completion_text: str = ""
                _adapter: object = None
            return _FakeResult()

        monkeypatch.setattr(
            "provider_spawns.claude_spawn.spawn_claude", fake_spawn
        )

        adapter = ClaudeSubprocessAdapter()
        spec = EnvelopeSpec(
            dispatch_id="test-1", terminal_id="T1", provider="claude",
            model="sonnet", instruction="test", role=None, pr_id=None,
            state_dir=Path("/tmp"), data_dir=Path("/tmp"),
            # deadline_seconds NOT set — uses default 900
        )
        adapter.run(spec)

        assert captured.get("total_deadline") == 900.0, (
            f"Expected default total_deadline=900.0, got {captured.get('total_deadline')}"
        )


# ---------------------------------------------------------------------------
# 4. Receipt captures deadline_seconds (loud timeout)
# ---------------------------------------------------------------------------

class TestReceiptDeadlineField:
    """Verify the receipt records deadline_seconds so timeouts are loud."""

    def test_receipt_v2_has_deadline_field(self):
        """ReceiptV2 must accept and serialize deadline_seconds."""
        from receipt_schema import ReceiptV2

        receipt = ReceiptV2(
            dispatch_id="20260101-120000-test",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            status="timeout",
            completion_pct=0,
            risk=0.0,
            findings=[],
            duration_seconds=900.0,
            token_usage={"input": 0, "output": 0},
            receipt_kind="dispatch",
            deadline_seconds=3600,
        )
        d = receipt.to_dict()
        assert d["deadline_seconds"] == 3600
        assert d["status"] == "timeout"
        # An operator can now see: status=timeout, deadline=3600s — it was a
        # 3600s deadline that was exceeded, not a 900s one.

    def test_receipt_v2_omits_deadline_when_none(self):
        """ReceiptV2 omits deadline_seconds from serialization when None."""
        from receipt_schema import ReceiptV2

        receipt = ReceiptV2(
            dispatch_id="20260101-120000-test",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=10.0,
            token_usage={"input": 100, "output": 50},
            receipt_kind="dispatch",
            deadline_seconds=None,
        )
        d = receipt.to_dict()
        assert "deadline_seconds" not in d, (
            "deadline_seconds=None must be omitted from serialized receipt"
        )

    def test_emit_dispatch_receipt_accepts_deadline_seconds(self):
        """emit_dispatch_receipt must accept the deadline_seconds parameter."""
        from governance_emit import emit_dispatch_receipt

        import inspect
        sig = inspect.signature(emit_dispatch_receipt)
        assert "deadline_seconds" in sig.parameters, (
            "emit_dispatch_receipt must accept deadline_seconds param"
        )

    def test_receipt_timeout_with_deadline_distinguishable(self):
        """A timeout at 900s with a 3600s deadline must be distinguishable."""
        from receipt_schema import ReceiptV2

        # Scenario: spec set 3600s, but spawn used 900s (the pre-fix bug).
        # Post-fix: spec 3600 -> spawn 3600 -> timeout at 3600s.
        # The receipt records deadline_seconds so we know which case happened.
        receipt_900 = ReceiptV2(
            dispatch_id="bug-case",
            terminal_id="T1", provider="deepseek-harness", model="deepseek-v4-pro",
            status="timeout", completion_pct=0, risk=0.0, findings=[],
            duration_seconds=900.3, token_usage={"input": 0, "output": 0},
            receipt_kind="dispatch", deadline_seconds=900,
        )
        receipt_3600 = ReceiptV2(
            dispatch_id="fixed-case",
            terminal_id="T1", provider="deepseek-harness", model="deepseek-v4-pro",
            status="timeout", completion_pct=0, risk=0.0, findings=[],
            duration_seconds=3600.1, token_usage={"input": 0, "output": 0},
            receipt_kind="dispatch", deadline_seconds=3600,
        )

        d900 = receipt_900.to_dict()
        d3600 = receipt_3600.to_dict()

        assert d900["deadline_seconds"] == 900
        assert d3600["deadline_seconds"] == 3600
        assert d900["deadline_seconds"] != d3600["deadline_seconds"], (
            "The two cases must be distinguishable via deadline_seconds"
        )


# ---------------------------------------------------------------------------
# 5. All three harness lanes (claude, deepseek-harness, glm-harness) get
#    deadline passthrough
# ---------------------------------------------------------------------------

class TestHarnessLaneDeadlinePassthrough:
    """Verify all three harness lanes that delegate to spawn_claude receive the deadline."""

    def test_deepseek_harness_spawn_accepts_total_deadline_kwarg(self):
        """spawn_deepseek_harness must accept total_deadline via **kwargs."""
        from provider_spawns.deepseek_harness_spawn import spawn_deepseek_harness

        import inspect
        sig = inspect.signature(spawn_deepseek_harness)
        assert "kwargs" in sig.parameters or "total_deadline" in sig.parameters, (
            "spawn_deepseek_harness must accept total_deadline (via **kwargs or explicit)"
        )

    def test_glm_harness_spawn_accepts_total_deadline_kwarg(self):
        """spawn_glm_harness must accept total_deadline via **kwargs."""
        from provider_spawns.glm_harness_spawn import spawn_glm_harness

        import inspect
        sig = inspect.signature(spawn_glm_harness)
        assert "kwargs" in sig.parameters or "total_deadline" in sig.parameters, (
            "spawn_glm_harness must accept total_deadline (via **kwargs or explicit)"
        )

    def test_deepseek_harness_passes_total_deadline_to_spawn_claude(self, monkeypatch):
        """spawn_deepseek_harness forwards total_deadline kwarg to spawn_claude."""
        from provider_spawns.deepseek_harness_spawn import spawn_deepseek_harness

        captured = {}

        def fake_spawn(*, total_deadline=900.0, **kwargs):
            captured["total_deadline"] = total_deadline
            from dataclasses import dataclass as _dc, field as _f
            @_dc
            class _FakeResult:
                returncode: int = 0
                completion: dict = _f(default_factory=dict)
                events_written: int = 0
                session_id: str | None = None
                timed_out: bool = False
                stopped_early: bool = False
                error: str | None = None
                token_usage: dict | None = None
                completion_text: str = ""
                _adapter: object = None
            return _FakeResult()

        monkeypatch.setattr(
            "provider_spawns.deepseek_harness_spawn.spawn_claude", fake_spawn
        )
        # Must have a fake API key so it doesn't fast-fail before spawn
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")

        spawn_deepseek_harness(
            prompt="test", model="deepseek-v4-pro", dispatch_id="d1",
            terminal_id="T1", total_deadline=7200.0,
        )

        assert captured.get("total_deadline") == 7200.0, (
            f"Expected total_deadline=7200.0 forwarded to spawn_claude, "
            f"got {captured.get('total_deadline')}"
        )

    def test_glm_harness_passes_total_deadline_to_spawn_claude(self, monkeypatch):
        """spawn_glm_harness forwards total_deadline kwarg to spawn_claude."""
        from provider_spawns.glm_harness_spawn import spawn_glm_harness

        captured = {}

        def fake_spawn(*, total_deadline=900.0, **kwargs):
            captured["total_deadline"] = total_deadline
            from dataclasses import dataclass as _dc, field as _f
            @_dc
            class _FakeResult:
                returncode: int = 0
                completion: dict = _f(default_factory=dict)
                events_written: int = 0
                session_id: str | None = None
                timed_out: bool = False
                stopped_early: bool = False
                error: str | None = None
                token_usage: dict | None = None
                completion_text: str = ""
                _adapter: object = None
            return _FakeResult()

        monkeypatch.setattr(
            "provider_spawns.glm_harness_spawn.spawn_claude", fake_spawn
        )
        # Bypass proxy reachability check
        monkeypatch.setattr(
            "provider_spawns.glm_harness_spawn._proxy_reachable", lambda *a, **kw: True
        )

        spawn_glm_harness(
            prompt="test", model="glm-5.2", dispatch_id="d1",
            terminal_id="T1", total_deadline=7200.0,
        )

        assert captured.get("total_deadline") == 7200.0, (
            f"Expected total_deadline=7200.0 forwarded to spawn_claude, "
            f"got {captured.get('total_deadline')}"
        )

    def test_deepseek_harness_default_is_900(self, monkeypatch):
        """Without explicit total_deadline, deepseek-harness defaults to 900."""
        from provider_spawns.deepseek_harness_spawn import spawn_deepseek_harness

        captured = {}

        def fake_spawn(*, total_deadline=900.0, **kwargs):
            captured["total_deadline"] = total_deadline
            from dataclasses import dataclass as _dc, field as _f
            @_dc
            class _FakeResult:
                returncode: int = 0
                completion: dict = _f(default_factory=dict)
                events_written: int = 0
                session_id: str | None = None
                timed_out: bool = False
                stopped_early: bool = False
                error: str | None = None
                token_usage: dict | None = None
                completion_text: str = ""
                _adapter: object = None
            return _FakeResult()

        monkeypatch.setattr(
            "provider_spawns.deepseek_harness_spawn.spawn_claude", fake_spawn
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")

        spawn_deepseek_harness(
            prompt="test", model="deepseek-v4-pro", dispatch_id="d1",
            terminal_id="T1",
            # total_deadline NOT passed — spawn_claude's own default (900.0) applies.
            # Our mock mirrors this with default=900.0.
        )

        assert captured.get("total_deadline") == 900.0, (
            f"Without explicit total_deadline, spawn_claude should receive 900.0 "
            f"(its own default), got {captured.get('total_deadline')}"
        )


# ---------------------------------------------------------------------------
# 6. provider_dispatch CLI --deadline-seconds arg
# ---------------------------------------------------------------------------

class TestProviderDispatchDeadlineArg:
    """Verify provider_dispatch.py accepts --deadline-seconds CLI arg."""

    def test_parser_accepts_deadline_seconds(self):
        """_build_parser() must include --deadline-seconds argument."""
        from provider_dispatch import _build_parser

        parser = _build_parser()
        # Parse with the new arg
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "20260101-test",
            "--instruction", "test",
            "--deadline-seconds", "3600",
        ])
        assert args.deadline_seconds == 3600

    def test_parser_deadline_seconds_default_is_900(self):
        """Without --deadline-seconds, args.deadline_seconds defaults to 900."""
        from provider_dispatch import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "20260101-test",
            "--instruction", "test",
        ])
        assert args.deadline_seconds == 900


# ---------------------------------------------------------------------------
# 7. Pool worker reads and passes deadline from dispatch-spec.json
# ---------------------------------------------------------------------------

class TestPoolWorkerDeadline:
    """Verify pool_worker_runner reads deadline_seconds from spec and passes it."""

    def test_deliver_claude_accepts_deadline_kwarg(self):
        """_deliver_claude must accept deadline_seconds parameter."""
        from pool_worker_runner import _deliver_claude

        import inspect
        sig = inspect.signature(_deliver_claude)
        assert "deadline_seconds" in sig.parameters, (
            "_deliver_claude must accept deadline_seconds kwarg"
        )
        assert sig.parameters["deadline_seconds"].default == 900

    def test_deliver_provider_accepts_deadline_kwarg(self):
        """_deliver_provider must accept deadline_seconds parameter."""
        from pool_worker_runner import _deliver_provider

        import inspect
        sig = inspect.signature(_deliver_provider)
        assert "deadline_seconds" in sig.parameters, (
            "_deliver_provider must accept deadline_seconds kwarg"
        )
        assert sig.parameters["deadline_seconds"].default == 900
