"""test_receipt_v2_pr_b2_wiring.py — receipt-quality PR-B2 end-to-end wiring tests.

Covers the two _emit_governance (scripts/lib/provider_dispatch.py) changes landed
in this PR:

  - OI-819 (codex PR-B1 review, Finding 2): the ``session_id`` kwarg passed to
    ``governance_emit.emit_dispatch_receipt`` now prefers the spawn result's own
    ``session_id`` over ``args.session_id``/``$VNX_SESSION_ID``.
  - Tool-call signal aggregation: ``VNX_TMUX_SIGNAL_DIR``'s ``toolcalls.ndjson``
    (scripts/lib/toolcall_signals.py) is folded into ``tool_call_count`` /
    ``tool_call_failures`` / ``tool_call_retries`` on the emitted ReceiptV2.

Mirrors tests/test_receipt_v2_pr4_wiring.py's capture-the-real-kwargs pattern
(patch governance_emit.emit_dispatch_receipt with a side_effect that both
records and delegates) so these prove the WIRING, not just the pure functions
already unit-tested in isolation (test_toolcall_signals.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import governance_emit  # noqa: E402
import provider_dispatch  # noqa: E402
from toolcall_signals import record_toolcall_event  # noqa: E402


def _read_lines(receipts_path: Path) -> list:
    if not receipts_path.exists():
        return []
    return [json.loads(l) for l in receipts_path.read_text().splitlines() if l.strip()]


def _run_emit_governance(monkeypatch, tmp_path, *, args, result, provider="claude"):
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)

    captured: Dict[str, Any] = {}
    real_emit = governance_emit.emit_dispatch_receipt

    def _capture(**kwargs):
        captured.update(kwargs)
        return real_emit(**kwargs)

    now = datetime.now(timezone.utc)
    with patch("governance_emit.emit_dispatch_receipt", side_effect=_capture):
        provider_dispatch._emit_governance(args, provider, "some-model", result, now, now, "success")

    receipts_path = tmp_path / "state" / "t0_receipts.ndjson"
    return captured, _read_lines(receipts_path)


# ---------------------------------------------------------------------------
# OI-819 — session_id preference order: result > args > env
# ---------------------------------------------------------------------------


def test_session_id_prefers_result_over_args_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_SESSION_ID", "env-session-loses")
    args = argparse.Namespace(
        dispatch_id="b2-sessid-result-wins", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None, session_id="args-session-loses",
    )
    result = SimpleNamespace(
        completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1},
        session_id="result-session-wins",
    )
    captured, _ = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)
    assert captured["session_id"] == "result-session-wins"


def test_session_id_falls_back_to_args_when_result_has_none(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_SESSION_ID", "env-session-loses")
    args = argparse.Namespace(
        dispatch_id="b2-sessid-args-wins", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None, session_id="args-session-wins",
    )
    # A result object with no session_id concept at all (mirrors _ClaudeResult
    # in provider_dispatch.py, which carries only completion_text/token_usage).
    result = SimpleNamespace(completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1})
    captured, _ = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)
    assert captured["session_id"] == "args-session-wins"


def test_session_id_falls_back_to_env_when_result_and_args_have_none(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_SESSION_ID", "env-session-wins")
    args = argparse.Namespace(
        dispatch_id="b2-sessid-env-wins", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None,
    )
    result = SimpleNamespace(completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1})
    captured, _ = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)
    assert captured["session_id"] == "env-session-wins"


def test_session_id_none_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.delenv("VNX_SESSION_ID", raising=False)
    args = argparse.Namespace(
        dispatch_id="b2-sessid-none", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None,
    )
    result = SimpleNamespace(completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1})
    captured, _ = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)
    assert captured["session_id"] is None


# ---------------------------------------------------------------------------
# Tool-call signal aggregation wiring
# ---------------------------------------------------------------------------


def test_toolcall_signals_aggregated_onto_receipt(monkeypatch, tmp_path):
    signal_dir = tmp_path / "tmux-signal"
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "claude -p x"}}, blocked=True)
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
    monkeypatch.setenv("VNX_TMUX_SIGNAL_DIR", str(signal_dir))

    args = argparse.Namespace(
        dispatch_id="b2-toolcalls-present", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None,
    )
    result = SimpleNamespace(completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1})

    captured, lines = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)

    assert captured["tool_call_count"] == 4
    assert captured["tool_call_failures"] == 1
    assert captured["tool_call_retries"] == 1
    line = lines[-1]
    assert line["tool_call_count"] == 4
    assert line["tool_call_failures"] == 1
    assert line["tool_call_retries"] == 1


def test_toolcall_signals_absent_when_signal_dir_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("VNX_TMUX_SIGNAL_DIR", raising=False)

    args = argparse.Namespace(
        dispatch_id="b2-toolcalls-absent", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None,
    )
    result = SimpleNamespace(completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1})

    captured, lines = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)

    assert captured["tool_call_count"] is None
    assert captured["tool_call_failures"] is None
    assert captured["tool_call_retries"] is None
    line = lines[-1]
    assert "tool_call_count" not in line
    assert "tool_call_failures" not in line
    assert "tool_call_retries" not in line


def test_toolcall_signals_absent_when_signal_dir_empty(monkeypatch, tmp_path):
    """VNX_TMUX_SIGNAL_DIR set but no toolcalls.ndjson written -> still None,
    never a crash, never a fabricated zero."""
    signal_dir = tmp_path / "tmux-signal-empty"
    signal_dir.mkdir()
    monkeypatch.setenv("VNX_TMUX_SIGNAL_DIR", str(signal_dir))

    args = argparse.Namespace(
        dispatch_id="b2-toolcalls-empty-dir", terminal_id="T1", instruction="do thing",
        pr_id=None, mandate_id=None,
    )
    result = SimpleNamespace(completion_text="done", token_usage={"input_tokens": 1, "output_tokens": 1})

    captured, _ = _run_emit_governance(monkeypatch, tmp_path, args=args, result=result)

    assert captured["tool_call_count"] is None
