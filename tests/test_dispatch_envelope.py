"""test_dispatch_envelope.py — Tests for dispatch_envelope.py (PR-1 codex, PR-2 claude-subprocess).

Verifies:
1. Success path: spawn_codex/spawn_claude -> 0 -> BOTH report AND receipt emitted, returncode 0.
2. Failure path: spawn error -> BOTH report AND receipt emitted, status "failure".
3. Timeout path: spawn timed_out -> BOTH report AND receipt emitted, status "timeout".
4. Stopped_early path (claude-only): spawn stopped_early -> BOTH report AND receipt, status "success".
5. Fail-closed: receipt emit raises -> EnvelopeGovernError (non-zero, no silent loss).
6. Fail-closed: receipt_path returns None -> EnvelopeGovernError.
7. Idempotent dedup: pre-existing receipt line -> GOVERN skips write, no double-emit.
8. Flag-off: VNX_UNIFIED_ENVELOPE unset -> legacy dispatch called, envelope NOT invoked.
9. Flag-on: VNX_UNIFIED_ENVELOPE=1 + lanes contains lane -> envelope invoked.
10. Lane "claude" alias -> routes to envelope (same as "claude-subprocess").
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
import provider_dispatch
from dispatch_envelope import (
    EnvelopeGovernError,
    EnvelopeSpec,
    LaneRouter,
    run_envelope,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeCodexResult:
    """Minimal CodexSpawnResult-compatible stub for spawn_codex mocking."""

    returncode: int = 0
    completion_text: str = "task done"
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0

    def __post_init__(self):
        if self.token_usage is None:
            self.token_usage = {"input_tokens": 100, "output_tokens": 50}


@dataclass
class _FakeClaudeResult:
    """Minimal ClaudeSpawnResult-compatible stub for spawn_claude mocking."""

    returncode: int = 0
    completion: Dict[str, Any] = field(default_factory=dict)
    events_written: int = 10
    session_id: Optional[str] = "session-001"
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.token_usage is None:
            self.token_usage = {
                "input_tokens": 200,
                "output_tokens": 100,
                "cache_read_input_tokens": 50,
            }


# ---------------------------------------------------------------------------
# Fixture: minimal EnvelopeSpec pointing at tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec(tmp_path: Path) -> EnvelopeSpec:
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir(parents=True)
    (data_dir / "unified_reports").mkdir(parents=True)
    return EnvelopeSpec(
        dispatch_id="env-pr1-test-001",
        terminal_id="T1",
        provider="codex",
        model="gpt-5.2-codex",
        instruction="implement the feature",
        role="backend-developer",
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


@pytest.fixture()
def spec_claude(tmp_path: Path) -> EnvelopeSpec:
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir(parents=True)
    (data_dir / "unified_reports").mkdir(parents=True)
    return EnvelopeSpec(
        dispatch_id="env-pr2-test-001",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction="implement the feature",
        role="backend-developer",
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


def _touch_and_return(path: Path) -> Path:
    """Create an empty file at path and return the path (for mock side_effect)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


_UNSET = object()  # sentinel distinguishing "not provided" from explicit None


def _stub_governance(
    spec: EnvelopeSpec,
    *,
    receipt_side_effect=None,
    receipt_return=_UNSET,
):
    """Return (report_path, receipt_path, mock_report, mock_receipt) wired to spec."""
    report_path = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
    receipt_path = spec.state_dir / "t0_receipts.ndjson"
    receipt_path.write_text("")  # ensure .exists() check passes for normal cases

    mock_report = MagicMock(return_value=report_path)
    if receipt_side_effect is not None:
        mock_receipt = MagicMock(side_effect=receipt_side_effect)
    elif receipt_return is not _UNSET:
        mock_receipt = MagicMock(return_value=receipt_return)
    else:
        mock_receipt = MagicMock(return_value=receipt_path)

    return report_path, receipt_path, mock_report, mock_receipt


# ---------------------------------------------------------------------------
# 1-3: success / failure / timeout all emit BOTH report AND receipt
# ---------------------------------------------------------------------------


class TestEnvelopeEmitsBothReportAndReceipt:
    """PREPARE->ROUTE->EXECUTE->GOVERN emits report AND receipt for every outcome."""

    def _run(self, spec, codex_result):
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec)

        with patch("provider_spawns.codex_spawn.spawn_codex", return_value=codex_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            result = run_envelope(spec, lane="codex")

        return result, mock_report, mock_receipt

    def test_success_emits_report_and_receipt(self, spec):
        codex_result = _FakeCodexResult(returncode=0)
        result, mock_report, mock_receipt = self._run(spec, codex_result)

        assert result.status == "success"
        assert result.returncode == 0
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()

    def test_failure_emits_report_and_receipt(self, spec):
        codex_result = _FakeCodexResult(returncode=1, error="codex process exited 1")
        result, mock_report, mock_receipt = self._run(spec, codex_result)

        assert result.status == "failure"
        assert result.returncode == 1
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["status"] == "failure"

    def test_timeout_emits_report_and_receipt(self, spec):
        codex_result = _FakeCodexResult(returncode=1, timed_out=True)
        result, mock_report, mock_receipt = self._run(spec, codex_result)

        assert result.status == "timeout"
        assert result.returncode == 1
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["status"] == "timeout"


# ---------------------------------------------------------------------------
# 4-5: fail-closed on missing / failed receipt
# ---------------------------------------------------------------------------


class TestEnvelopeFailClosed:
    """GOVERN must raise EnvelopeGovernError when receipt is missing — never silent."""

    def test_receipt_emit_raises_fail_closed(self, spec):
        _, _, mock_report, mock_receipt = _stub_governance(
            spec, receipt_side_effect=RuntimeError("disk full")
        )
        codex_result = _FakeCodexResult(returncode=0)

        with patch("provider_spawns.codex_spawn.spawn_codex", return_value=codex_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            with pytest.raises(EnvelopeGovernError, match="receipt emit raised"):
                run_envelope(spec, lane="codex")

    def test_none_receipt_path_fail_closed(self, spec):
        _, _, mock_report, mock_receipt = _stub_governance(
            spec, receipt_return=None
        )
        codex_result = _FakeCodexResult(returncode=0)

        with patch("provider_spawns.codex_spawn.spawn_codex", return_value=codex_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            with pytest.raises(EnvelopeGovernError, match="receipt_path is None"):
                run_envelope(spec, lane="codex")


# ---------------------------------------------------------------------------
# 6-7: flag-off = legacy path; flag-on = envelope invoked
# ---------------------------------------------------------------------------


class TestFlagGate:
    """VNX_UNIFIED_ENVELOPE flag controls whether envelope or legacy path is used."""

    _CODEX_ARGV = [
        "--provider", "codex",
        "--terminal-id", "T1",
        "--dispatch-id", "test-flag-gate-codex",
        "--instruction", "noop",
    ]

    def test_flag_off_calls_legacy_dispatch_codex(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE unset -> _dispatch_codex, envelope NOT invoked."""
        monkeypatch.delenv("VNX_UNIFIED_ENVELOPE", raising=False)
        monkeypatch.delenv("VNX_UNIFIED_ENVELOPE_LANES", raising=False)

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_codex", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_codex_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CODEX_ARGV)

        mock_legacy.assert_called_once()
        mock_via_envelope.assert_not_called()
        assert result == 0

    def test_flag_on_calls_envelope(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE=1 + codex in lanes -> _dispatch_codex_via_envelope."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "codex")

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_codex", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_codex_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CODEX_ARGV)

        mock_via_envelope.assert_called_once()
        mock_legacy.assert_not_called()
        assert result == 0

    def test_flag_on_wrong_lane_calls_legacy(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE=1 but codex NOT in lanes -> legacy _dispatch_codex."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "gemini,kimi")

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_codex", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_codex_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CODEX_ARGV)

        mock_legacy.assert_called_once()
        mock_via_envelope.assert_not_called()
        assert result == 0


# ---------------------------------------------------------------------------
# LaneRouter unit test
# ---------------------------------------------------------------------------


class TestLaneRouter:

    def test_codex_returns_codex_adapter(self):
        from dispatch_envelope import CodexAdapter
        adapter = LaneRouter().get("codex")
        assert isinstance(adapter, CodexAdapter)

    def test_claude_subprocess_returns_claude_adapter(self):
        from dispatch_envelope import ClaudeSubprocessAdapter
        adapter = LaneRouter().get("claude-subprocess")
        assert isinstance(adapter, ClaudeSubprocessAdapter)

    def test_unknown_lane_raises(self):
        with pytest.raises(ValueError, match="no adapter registered"):
            LaneRouter().get("unknown-lane")


# ---------------------------------------------------------------------------
# Claude-subprocess: success / failure / timeout / stopped_early emit BOTH
# ---------------------------------------------------------------------------


class TestClaudeEnvelopeEmitsBothReportAndReceipt:
    """PREPARE->ROUTE->EXECUTE->GOVERN emits report AND receipt for every outcome (claude-subprocess lane)."""

    def _run(self, spec, claude_result):
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec)

        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            result = run_envelope(spec, lane="claude-subprocess")

        return result, mock_report, mock_receipt

    def test_success_emits_report_and_receipt(self, spec_claude):
        claude_result = _FakeClaudeResult(returncode=0)
        result, mock_report, mock_receipt = self._run(spec_claude, claude_result)

        assert result.status == "success"
        assert result.returncode == 0
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()

    def test_failure_emits_report_and_receipt(self, spec_claude):
        claude_result = _FakeClaudeResult(returncode=1, error="claude process exited 1")
        result, mock_report, mock_receipt = self._run(spec_claude, claude_result)

        assert result.status == "failure"
        assert result.returncode == 1
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["status"] == "failure"

    def test_timeout_emits_report_and_receipt(self, spec_claude):
        claude_result = _FakeClaudeResult(returncode=1, timed_out=True)
        result, mock_report, mock_receipt = self._run(spec_claude, claude_result)

        assert result.status == "timeout"
        assert result.returncode == 1
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["status"] == "timeout"

    def test_stopped_early_emits_report_and_receipt(self, spec_claude):
        claude_result = _FakeClaudeResult(returncode=0, stopped_early=True)
        result, mock_report, mock_receipt = self._run(spec_claude, claude_result)

        assert result.status == "success"
        assert result.returncode == 0
        assert result.report_path is not None
        assert result.receipt_path is not None
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()


# ---------------------------------------------------------------------------
# Claude: fail-closed on missing / failed receipt
# ---------------------------------------------------------------------------


class TestClaudeEnvelopeFailClosed:
    """GOVERN must raise EnvelopeGovernError when receipt is missing — never silent (claude lane)."""

    def test_receipt_emit_raises_fail_closed(self, spec_claude):
        _, _, mock_report, mock_receipt = _stub_governance(
            spec_claude, receipt_side_effect=RuntimeError("disk full")
        )
        claude_result = _FakeClaudeResult(returncode=0)

        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            with pytest.raises(EnvelopeGovernError, match="receipt emit raised"):
                run_envelope(spec_claude, lane="claude-subprocess")

    def test_none_receipt_path_fail_closed(self, spec_claude):
        _, _, mock_report, mock_receipt = _stub_governance(
            spec_claude, receipt_return=None
        )
        claude_result = _FakeClaudeResult(returncode=0)

        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            with pytest.raises(EnvelopeGovernError, match="receipt_path is None"):
                run_envelope(spec_claude, lane="claude-subprocess")


# ---------------------------------------------------------------------------
# Idempotent dedup: pre-existing receipt → GOVERN skips write
# ---------------------------------------------------------------------------


class TestEnvelopeIdempotentDedup:
    """When a receipt line already exists for this dispatch_id, GOVERN skips the write."""

    def test_pre_existing_receipt_skips_emit(self, spec_claude):
        """GOVERN should not call emit_dispatch_receipt when receipt already present."""
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec_claude)

        # Pre-populate the NDJSON with a line for this dispatch_id
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            '{"dispatch_id":"env-pr2-test-001","status":"success"}\n',
            encoding="utf-8",
        )

        claude_result = _FakeClaudeResult(returncode=0)

        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            result = run_envelope(spec_claude, lane="claude-subprocess")

        assert result.status == "success"
        assert result.returncode == 0
        assert result.receipt_path == receipt_path
        # Report is still emitted (idempotent via emit_unified_report's early-return)
        mock_report.assert_called_once()
        # Receipt is NOT emitted (already exists — idempotent dedup)
        mock_receipt.assert_not_called()

    def test_no_pre_existing_receipt_emits_normally(self, spec_claude):
        """When no receipt exists, emit_dispatch_receipt is called as normal."""
        # Build receipt_path manually without pre-creating it (unlike _stub_governance)
        report_path = spec_claude.data_dir / "unified_reports" / f"{spec_claude.dispatch_id}.md"
        receipt_path = spec_claude.state_dir / "t0_receipts.ndjson"
        mock_report = MagicMock(return_value=report_path)
        # Side-effect creates the file on disk so _govern's .exists() check passes
        mock_receipt = MagicMock()
        mock_receipt.side_effect = lambda **kwargs: _touch_and_return(receipt_path)

        # Ensure NDJSON does NOT exist yet (no pre-populated receipt)
        assert not receipt_path.exists()

        claude_result = _FakeClaudeResult(returncode=0)

        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            result = run_envelope(spec_claude, lane="claude-subprocess")

        assert result.status == "success"
        mock_report.assert_called_once()
        mock_receipt.assert_called_once()


# ---------------------------------------------------------------------------
# Flag gate: claude - flag-off = legacy path; flag-on = envelope invoked
# ---------------------------------------------------------------------------


class TestFlagGateClaude:
    """VNX_UNIFIED_ENVELOPE flag controls whether envelope or legacy path is used for claude."""

    _CLAUDE_ARGV = [
        "--provider", "claude",
        "--terminal-id", "T1",
        "--dispatch-id", "test-flag-gate-claude",
        "--instruction", "noop",
    ]

    def test_flag_off_calls_legacy_dispatch_claude(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE unset -> _dispatch_claude, envelope NOT invoked."""
        monkeypatch.delenv("VNX_UNIFIED_ENVELOPE", raising=False)
        monkeypatch.delenv("VNX_UNIFIED_ENVELOPE_LANES", raising=False)

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_claude", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_claude_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CLAUDE_ARGV)

        mock_legacy.assert_called_once()
        mock_via_envelope.assert_not_called()
        assert result == 0

    def test_flag_on_calls_envelope(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE=1 + claude-subprocess in lanes -> _dispatch_claude_via_envelope."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "claude-subprocess")

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_claude", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_claude_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CLAUDE_ARGV)

        mock_via_envelope.assert_called_once()
        mock_legacy.assert_not_called()
        assert result == 0

    def test_flag_on_claude_alias_calls_envelope(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE=1 + "claude" (alias) in lanes -> _dispatch_claude_via_envelope."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "claude")

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_claude", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_claude_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CLAUDE_ARGV)

        mock_via_envelope.assert_called_once()
        mock_legacy.assert_not_called()
        assert result == 0

    def test_flag_on_wrong_lane_calls_legacy(self, monkeypatch):
        """VNX_UNIFIED_ENVELOPE=1 but claude NOT in lanes -> legacy _dispatch_claude."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "codex,gemini")

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_claude", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_claude_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CLAUDE_ARGV)

        mock_legacy.assert_called_once()
        mock_via_envelope.assert_not_called()
        assert result == 0
