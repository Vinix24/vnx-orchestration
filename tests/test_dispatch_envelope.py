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
    completion_text: str = ""

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


class TestEnvelopeEventArchiveClear:
    """OI-918: the end-of-dispatch clear must only fire when archiving succeeded.

    _govern's finally unconditionally truncated the live event stream even when
    the end-of-dispatch archive had failed — destroying exactly the events it
    was meant to preserve. _archive_dispatch_events now returns
    (events_path, clear_ok), and the clear is gated on clear_ok.
    """

    def _run(self, spec, codex_result, archive_return):
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec)
        mock_archive = MagicMock(return_value=archive_return)
        mock_clear = MagicMock()

        with patch("provider_spawns.codex_spawn.spawn_codex", return_value=codex_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt), \
             patch("dispatch_envelope._archive_dispatch_events", mock_archive), \
             patch("dispatch_envelope._clear_dispatch_events", mock_clear):
            result = run_envelope(spec, lane="codex")

        return result, mock_archive, mock_clear

    def test_archive_failure_skips_clear(self, spec):
        """archive raised (clear_ok=False) -> live stream must NOT be truncated."""
        codex_result = _FakeCodexResult(returncode=0)
        result, mock_archive, mock_clear = self._run(spec, codex_result, (None, False))

        assert result.status == "success"
        mock_archive.assert_called_once_with(spec.terminal_id, spec.dispatch_id)
        mock_clear.assert_not_called()

    def test_archive_success_still_clears(self, spec):
        """archive succeeded (clear_ok=True) -> clear still fires as before."""
        codex_result = _FakeCodexResult(returncode=0)
        result, mock_archive, mock_clear = self._run(spec, codex_result, ("/arc/path.ndjson", True))

        assert result.status == "success"
        mock_clear.assert_called_once_with(spec.terminal_id, spec.dispatch_id)


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

    def test_receipt_ledger_unreadable_skips_emit(self, spec_claude, caplog):
        """When receipt NDJSON exists but cannot be read (OSError), skip emit with warning.

        Fail-closed: treat the unreadable ledger as "cannot confirm dedup" and skip
        the receipt write rather than risking a silent double-emit. A WARNING is logged
        with the exception details.
        """
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec_claude)

        # Create the NDJSON so it passes .exists() check
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text('{"dispatch_id":"other-dispatch"}\n', encoding="utf-8")

        claude_result = _FakeClaudeResult(returncode=0)

        # Patch open to raise OSError for the receipt file read.
        # Only _receipt_exists_for_dispatch opens this path during GOVERN;
        # emit_dispatch_receipt is mocked and won't call open.
        _real_open = open
        def _selective_open(path, *args, **kwargs):
            if isinstance(path, Path):
                path = str(path)
            if str(receipt_path) in str(path):
                raise OSError("Permission denied")
            return _real_open(path, *args, **kwargs)

        import logging
        with caplog.at_level(logging.WARNING, logger="dispatch_envelope"):
            with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
                 patch("governance_emit.emit_unified_report", mock_report), \
                 patch("governance_emit.emit_dispatch_receipt", mock_receipt), \
                 patch("builtins.open", side_effect=_selective_open):
                result = run_envelope(spec_claude, lane="claude-subprocess")

        assert result.status == "success"
        assert result.returncode == 0
        assert result.receipt_path == receipt_path
        mock_report.assert_called_once()
        # Receipt emit is SKIPPED (fail-closed: unreadable ledger → skip to avoid double-emit)
        mock_receipt.assert_not_called()
        # A WARNING was logged with the exception details
        assert any(
            "cannot read receipt ledger" in record.message
            and "Permission denied" in record.message
            for record in caplog.records
        ), f"Expected WARNING about unreadable receipt ledger, got: {[r.message for r in caplog.records]}"

    def test_receipt_exists_for_dispatch_oserror_returns_true(self, tmp_path, caplog):
        """Unit test: _receipt_exists_for_dispatch returns True on OSError (fail-closed)."""
        receipt_path = tmp_path / "t0_receipts.ndjson"
        receipt_path.write_text('{"dispatch_id":"some-id"}\n', encoding="utf-8")

        _real_open = open
        def _raise_oserror(path, *args, **kwargs):
            if isinstance(path, Path):
                path = str(path)
            if str(receipt_path) in str(path):
                raise OSError("Permission denied")
            return _real_open(path, *args, **kwargs)

        import logging
        with caplog.at_level(logging.WARNING, logger="dispatch_envelope"):
            with patch("builtins.open", side_effect=_raise_oserror):
                result = dispatch_envelope._receipt_exists_for_dispatch(
                    receipt_path, "some-id"
                )

        # Fail-closed: unreadable ledger → return True (skip emit, no double-receipt)
        assert result is True
        # Warning logged with exception details
        assert any(
            "cannot read receipt ledger" in record.message
            for record in caplog.records
        ), f"Expected WARNING log, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Flag gate: claude - flag-off = legacy path; flag-on = envelope invoked
# ---------------------------------------------------------------------------


class TestFlagGateClaude:
    """claude via provider_dispatch is ALWAYS rejected at the door (PR-5).

    The old VNX_UNIFIED_ENVELOPE flag gate for the claude lane was removed:
    provider_dispatch is not a provider-lane for claude — the single-entry
    dispatch door owns all claude routing (DISPATCH_RULES provider->lane rule,
    'claude/Opus/Sonnet panelists and workers route via the tmux-spawn lane —
    NEVER provider_dispatch'). These tests pin that the claude flag-gate no
    longer exists: regardless of VNX_UNIFIED_ENVELOPE, claude is refused with
    EX_USAGE and neither dispatch path is invoked. (Previously the four tests
    here asserted a flag-gate that PR-5 deleted, leaving them permanently red
    — OI-919.)
    """

    _CLAUDE_ARGV = [
        "--provider", "claude",
        "--terminal-id", "T1",
        "--dispatch-id", "test-flag-gate-claude",
        "--instruction", "noop",
    ]

    def _assert_claude_rejected(self, monkeypatch, capsys):
        monkeypatch.setenv("VNX_SINGLE_ENTRY_DISPATCH", "0")
        monkeypatch.delenv("VNX_BENCH_SEED_MATERIALIZE", raising=False)
        monkeypatch.delenv("VNX_BENCH_CLAUDE_HEADLESS", raising=False)

        mock_legacy = MagicMock(return_value=0)
        mock_via_envelope = MagicMock(return_value=0)

        with patch.object(provider_dispatch, "_dispatch_claude", mock_legacy), \
             patch.object(provider_dispatch, "_dispatch_claude_via_envelope", mock_via_envelope):
            result = provider_dispatch.main(self._CLAUDE_ARGV)

        assert result == provider_dispatch._EX_USAGE
        mock_legacy.assert_not_called()
        mock_via_envelope.assert_not_called()
        assert "is not a provider-lane provider" in capsys.readouterr().err

    def test_flag_off_claude_rejected_at_door(self, monkeypatch, capsys):
        """VNX_UNIFIED_ENVELOPE unset: claude is still rejected, not legacy-routed."""
        monkeypatch.delenv("VNX_UNIFIED_ENVELOPE", raising=False)
        monkeypatch.delenv("VNX_UNIFIED_ENVELOPE_LANES", raising=False)
        self._assert_claude_rejected(monkeypatch, capsys)

    def test_flag_on_claude_rejected_at_door(self, monkeypatch, capsys):
        """VNX_UNIFIED_ENVELOPE=1 + claude-subprocess in lanes: still rejected."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "claude-subprocess")
        self._assert_claude_rejected(monkeypatch, capsys)

    def test_flag_on_claude_alias_rejected_at_door(self, monkeypatch, capsys):
        """VNX_UNIFIED_ENVELOPE=1 + "claude" alias in lanes: still rejected."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "claude")
        self._assert_claude_rejected(monkeypatch, capsys)

    def test_flag_on_wrong_lane_claude_rejected_at_door(self, monkeypatch, capsys):
        """VNX_UNIFIED_ENVELOPE=1 + claude NOT in lanes: still rejected."""
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", "codex,gemini")
        self._assert_claude_rejected(monkeypatch, capsys)


# ---------------------------------------------------------------------------
# Role-forwarding: ClaudeSubprocessAdapter must forward spec.role to spawn_claude
# ---------------------------------------------------------------------------


class TestClaudeAdapterForwardsRoleToSpawn:
    """ClaudeSubprocessAdapter.run() must forward spec.role to spawn_claude.

    Without role forwarding, SubprocessAdapter.deliver() falls back to the
    default capability profile — too restrictive for specialised workers
    (backend-developer, reviewer, etc.). This test ensures role is passed through.
    """

    def test_dispatch_envelope_forwards_role_to_spawn(self, spec_claude):
        """spawn_claude is called with role=spec.role (not None / missing)."""
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec_claude)

        claude_result = _FakeClaudeResult(returncode=0)
        captured_kwargs: dict = {}

        def _capturing_spawn_claude(**kwargs):
            captured_kwargs.update(kwargs)
            return claude_result

        with patch(
            "provider_spawns.claude_spawn.spawn_claude",
            side_effect=_capturing_spawn_claude,
        ), patch("governance_emit.emit_unified_report", mock_report), patch(
            "governance_emit.emit_dispatch_receipt", mock_receipt
        ):
            result = run_envelope(spec_claude, lane="claude-subprocess")

        assert result.status == "success"
        assert "role" in captured_kwargs, (
            "spawn_claude was not called with a 'role' kwarg — role is not forwarded"
        )
        assert captured_kwargs["role"] == spec_claude.role, (
            f"spawn_claude got role={captured_kwargs['role']!r}, "
            f"expected {spec_claude.role!r}"
        )

    def test_none_role_forwarded_as_none(self, tmp_path):
        """When spec.role is None, spawn_claude receives role=None (not absent)."""
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir(parents=True)
        (data_dir / "unified_reports").mkdir(parents=True)

        spec_no_role = EnvelopeSpec(
            dispatch_id="env-role-none-test",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            instruction="do something",
            role=None,
            pr_id=None,
            state_dir=state_dir,
            data_dir=data_dir,
        )
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec_no_role)
        claude_result = _FakeClaudeResult(returncode=0)
        captured_kwargs: dict = {}

        def _capturing_spawn_claude(**kwargs):
            captured_kwargs.update(kwargs)
            return claude_result

        with patch(
            "provider_spawns.claude_spawn.spawn_claude",
            side_effect=_capturing_spawn_claude,
        ), patch("governance_emit.emit_unified_report", mock_report), patch(
            "governance_emit.emit_dispatch_receipt", mock_receipt
        ):
            result = run_envelope(spec_no_role, lane="claude-subprocess")

        assert result.status == "success"
        assert "role" in captured_kwargs
        assert captured_kwargs["role"] is None


# ---------------------------------------------------------------------------
# Regression: ClaudeSubprocessAdapter captures completion_text from spawn result
# ---------------------------------------------------------------------------


class TestClaudeAdapterCapturesCompletionText:
    """ClaudeSubprocessAdapter.run() must propagate completion_text from ClaudeSpawnResult.

    Regression for bench-v4 finding (2026-06-03): claude lane returned empty
    completion_text for all outcomes, causing receipt 'Response: (no response captured)'
    and benchmark scores of 0/1 for codegen tasks.
    """

    def test_claude_subprocess_adapter_captures_completion_text_from_assistant_events(
        self, spec_claude
    ):
        """Adapter result.completion_text matches the text captured by spawn_claude."""
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec_claude)

        claude_result = _FakeClaudeResult(
            returncode=0,
            completion_text="def foo(): pass",
        )

        with patch(
            "provider_spawns.claude_spawn.spawn_claude",
            return_value=claude_result,
        ), patch("governance_emit.emit_unified_report", mock_report), patch(
            "governance_emit.emit_dispatch_receipt", mock_receipt
        ):
            from dispatch_envelope import ClaudeSubprocessAdapter
            adapter = ClaudeSubprocessAdapter()
            adapter_result = adapter.run(spec_claude)

        assert adapter_result.completion_text == "def foo(): pass", (
            f"Expected 'def foo(): pass', got {adapter_result.completion_text!r}"
        )


# ---------------------------------------------------------------------------
# receipt-quality PR-B2 fix-forward (Finding C): _govern() aggregates
# PreToolUse-hook tool-call signals (toolcall_signals.py) onto the receipt
# for the claude/subprocess-adapter lane, mirroring the wiring
# provider_dispatch._emit_governance already had
# (tests/test_receipt_v2_pr_b2_wiring.py) and the tmux-interactive lane's
# worker-authored receipt now also has
# (tests/test_toolcall_signals_tmux_lane_wiring.py).
# ---------------------------------------------------------------------------


class TestClaudeEnvelopeToolcallSignals:
    def _run(self, spec, claude_result):
        report_path, receipt_path, mock_report, mock_receipt = _stub_governance(spec)

        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=claude_result), \
             patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            result = run_envelope(spec, lane="claude-subprocess")

        return result, mock_receipt

    def test_toolcall_signals_aggregated_onto_receipt(self, spec_claude, monkeypatch, tmp_path):
        from toolcall_signals import record_toolcall_event

        signal_dir = tmp_path / "tmux-signal"
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "claude -p x"}}, blocked=True)
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
        monkeypatch.setenv("VNX_TMUX_SIGNAL_DIR", str(signal_dir))

        claude_result = _FakeClaudeResult(returncode=0)
        _, mock_receipt = self._run(spec_claude, claude_result)

        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["tool_call_count"] == 4
        assert mock_receipt.call_args[1]["tool_call_failures"] == 1
        assert mock_receipt.call_args[1]["tool_call_retries"] == 1

    def test_toolcall_signals_absent_when_signal_dir_unset(self, spec_claude, monkeypatch):
        monkeypatch.delenv("VNX_TMUX_SIGNAL_DIR", raising=False)

        claude_result = _FakeClaudeResult(returncode=0)
        _, mock_receipt = self._run(spec_claude, claude_result)

        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["tool_call_count"] is None
        assert mock_receipt.call_args[1]["tool_call_failures"] is None
        assert mock_receipt.call_args[1]["tool_call_retries"] is None


class TestEnvelopeCostUsd:
    """OI-882: the envelope must compute cost_usd from token_usage + wave7 prices.

    The envelope previously hardcoded ``cost_usd=None`` in ``_govern`` even
    though ``adapter_result.token_usage`` carried real tokens. All 45 null-cost
    deepseek-harness receipts in the ledger came through this path.
    """

    def _run_govern(self, spec, adapter_result, tmp_path):
        report_path = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        receipt_path = spec.state_dir / "t0_receipts.ndjson"
        receipt_path.write_text("")
        mock_report = MagicMock(return_value=report_path)
        mock_receipt = MagicMock(return_value=receipt_path)
        mock_cost_event = MagicMock()

        from datetime import datetime, timezone

        with patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt), \
             patch("provider_costs.emit_provider_cost", mock_cost_event):
            dispatch_envelope._govern(
                spec,
                adapter_result,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )
        return mock_report, mock_receipt

    def test_deepseek_harness_cost_usd_computed(self, tmp_path):
        """deepseek-harness tokens + wave7 prices yield a real cost_usd on the receipt."""
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir(parents=True)
        (data_dir / "unified_reports").mkdir(parents=True)
        spec = EnvelopeSpec(
            dispatch_id="oi882-test-001",
            terminal_id="T1",
            provider="deepseek-harness",
            model="deepseek-v4-flash",
            instruction="review",
            role="backend-developer",
            pr_id=None,
            state_dir=state_dir,
            data_dir=data_dir,
        )
        adapter_result = dispatch_envelope._AdapterResult(
            returncode=0,
            completion_text="done",
            status="success",
            token_usage={"input": 91439, "output": 38053, "cache_hit": 6087040},
            model="deepseek-v4-flash",
        )

        _, mock_receipt = self._run_govern(spec, adapter_result, tmp_path)

        mock_receipt.assert_called_once()
        cost_usd = mock_receipt.call_args[1]["cost_usd"]
        assert cost_usd is not None, "OI-882: cost_usd must no longer be hardcoded None"
        assert cost_usd > 0
        # flash = 0.14/0.28 per MTok; 91439 input + 38053 output ≈ $0.0235
        assert abs(cost_usd - 0.0234563) < 1e-6
        assert mock_receipt.call_args[1]["model"] == "deepseek-v4-flash"

    def test_resolved_model_wins_over_placeholder(self, tmp_path):
        """A placeholder spec.model is replaced by the adapter's resolved model for pricing."""
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir(parents=True)
        (data_dir / "unified_reports").mkdir(parents=True)
        spec = EnvelopeSpec(
            dispatch_id="oi882-test-002",
            terminal_id="T1",
            provider="deepseek-harness",
            model="default",  # placeholder — adapter resolves to v4-pro
            instruction="review",
            role="backend-developer",
            pr_id=None,
            state_dir=state_dir,
            data_dir=data_dir,
        )
        adapter_result = dispatch_envelope._AdapterResult(
            returncode=0,
            completion_text="done",
            status="success",
            token_usage={"input": 1000, "output": 500, "cache_hit": 0},
            model="deepseek-v4-pro",
        )

        _, mock_receipt = self._run_govern(spec, adapter_result, tmp_path)

        mock_receipt.assert_called_once()
        assert mock_receipt.call_args[1]["model"] == "deepseek-v4-pro"
        cost_usd = mock_receipt.call_args[1]["cost_usd"]
        assert cost_usd is not None
        # pro = 0.435/0.87 per MTok; 1000 input + 500 output
        assert abs(cost_usd - (0.435 * 0.001 + 0.87 * 0.0005)) < 1e-9

    def test_claude_lane_cost_usd_computed(self, spec_claude):
        """The claude lane through the envelope now carries a list-price estimate."""
        from datetime import datetime, timezone

        adapter_result = dispatch_envelope._AdapterResult(
            returncode=0,
            completion_text="done",
            status="success",
            token_usage={"input": 200, "output": 100, "cache_hit": 50},
            model="sonnet",
        )
        report_path = spec_claude.data_dir / "unified_reports" / f"{spec_claude.dispatch_id}.md"
        receipt_path = spec_claude.state_dir / "t0_receipts.ndjson"
        receipt_path.write_text("")
        mock_report = MagicMock(return_value=report_path)
        mock_receipt = MagicMock(return_value=receipt_path)

        with patch("governance_emit.emit_unified_report", mock_report), \
             patch("governance_emit.emit_dispatch_receipt", mock_receipt):
            dispatch_envelope._govern(
                spec_claude,
                adapter_result,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )

        mock_receipt.assert_called_once()
        cost_usd = mock_receipt.call_args[1]["cost_usd"]
        assert cost_usd is not None
        # sonnet = 3.00/15.00 per MTok
        assert abs(cost_usd - (3.0 * 0.0002 + 15.0 * 0.0001)) < 1e-9
