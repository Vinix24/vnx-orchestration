#!/usr/bin/env python3
"""test_toolcall_signals_tmux_lane_wiring.py — receipt-quality PR-B2 fix-forward (Finding C).

Codex gate (PR #1235) flagged that PreToolUse hooks write tool-call signals
into $VNX_TMUX_SIGNAL_DIR on every tmux-interactive dispatch, but nothing
ever aggregated them for the receipt THAT lane actually writes: the
worker-authored completion receipt (event_type=subprocess_completion),
appended directly via scripts/append_receipt.py -- the exact command the
tmux lane's Completion Protocol footer instructs every worker to run at the
end of its dispatch. Only the provider/subprocess lanes' ReceiptV2 path
(governance_emit.emit_dispatch_receipt) aggregated signals before this fix;
the tmux lane's own receipt (append_receipt_payload / Path 2, enriched by
append_receipt_internals.enrichment._enrich_completion_receipt) never did,
so the feature was inert on its primary (and today only) signal-producing
lane.

Mirrors tests/test_append_receipt.py's real-subprocess pattern (invokes the
actual append_receipt.py CLI, not a mock) so this proves the WIRING through
the real enrichment pipeline, not just the pure aggregator already unit-
tested in isolation (tests/test_toolcall_signals.py).

Sibling coverage: tests/test_receipt_v2_pr_b2_wiring.py (provider_dispatch
lane), tests/test_dispatch_envelope.py (dispatch_envelope._govern, the
subprocess/claude-adapter lane).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
APPEND_SCRIPT = SCRIPTS_DIR / "append_receipt.py"

sys.path.insert(0, str(SCRIPTS_LIB))

from toolcall_signals import record_toolcall_event  # noqa: E402


def _build_env(tmp_path: Path, *, signal_dir: "Optional[Path]" = None) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    if signal_dir is not None:
        env["VNX_TMUX_SIGNAL_DIR"] = str(signal_dir)
    else:
        env.pop("VNX_TMUX_SIGNAL_DIR", None)
    return env


def _run_append(payload: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APPEND_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def _build_completion_receipt(dispatch_id: str) -> dict:
    """Mirrors the tmux lane's Completion Protocol footer payload shape --
    the worker-authored receipt every tmux-spawn dispatch appends directly
    via append_receipt.py at the end of its run."""
    return {
        "event_type": "subprocess_completion",
        "receipt_kind": "dispatch",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "terminal_id": "T1",
        "status": "done",
        "source": "tmux_interactive",
        "timestamp": "2026-07-28T10:00:00Z",
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": "sonnet",
        "lane": "tmux_interactive",
    }


def _last_receipt(tmp_path: Path) -> dict:
    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = receipts_file.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def test_tmux_lane_completion_receipt_carries_toolcall_signals(tmp_path: Path):
    signal_dir = tmp_path / "tmux-signal"
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "claude -p x"}}, blocked=True)
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)

    env = _build_env(tmp_path, signal_dir=signal_dir)
    receipt = _build_completion_receipt("b2fix-tmux-toolcalls-present")

    result = _run_append(json.dumps(receipt), env)
    assert result.returncode == 0, result.stderr

    stored = _last_receipt(tmp_path)
    assert stored["tool_call_count"] == 4
    assert stored["tool_call_failures"] == 1
    assert stored["tool_call_retries"] == 1


def test_tmux_lane_completion_receipt_omits_signals_when_dir_unset(tmp_path: Path):
    env = _build_env(tmp_path, signal_dir=None)
    receipt = _build_completion_receipt("b2fix-tmux-toolcalls-absent")

    result = _run_append(json.dumps(receipt), env)
    assert result.returncode == 0, result.stderr

    stored = _last_receipt(tmp_path)
    assert "tool_call_count" not in stored
    assert "tool_call_failures" not in stored
    assert "tool_call_retries" not in stored


def test_tmux_lane_completion_receipt_omits_signals_when_dir_empty(tmp_path: Path):
    """VNX_TMUX_SIGNAL_DIR set but no toolcalls.ndjson written yet (e.g. a
    worker whose dispatch made no Bash/Task tool calls before completing) --
    still None, never a crash, never a fabricated zero."""
    signal_dir = tmp_path / "tmux-signal-empty"
    signal_dir.mkdir()
    env = _build_env(tmp_path, signal_dir=signal_dir)
    receipt = _build_completion_receipt("b2fix-tmux-toolcalls-empty-dir")

    result = _run_append(json.dumps(receipt), env)
    assert result.returncode == 0, result.stderr

    stored = _last_receipt(tmp_path)
    assert "tool_call_count" not in stored


def test_tmux_lane_completion_receipt_never_overwrites_caller_supplied_signals(tmp_path: Path):
    """setdefault contract: a receipt that already carries tool_call_count
    (e.g. a replay/backfill receipt) is never overwritten by aggregation."""
    signal_dir = tmp_path / "tmux-signal-conflict"
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)

    env = _build_env(tmp_path, signal_dir=signal_dir)
    receipt = _build_completion_receipt("b2fix-tmux-toolcalls-preset")
    receipt["tool_call_count"] = 99

    result = _run_append(json.dumps(receipt), env)
    assert result.returncode == 0, result.stderr

    stored = _last_receipt(tmp_path)
    assert stored["tool_call_count"] == 99
