#!/usr/bin/env python3
"""Tests for dispatch-agent classified failure surfacing (OI-844).

Before this fix, `vnx dispatch-agent`'s failure branch printed the same generic
paragraph ("A common cause is a missing or unauthenticated worker CLI... see
the dispatch log under .vnx-data/ for the classified failure reason") for
every failure cause. Quota exhaustion, a missing binary, and a spawn timeout
were indistinguishable to the operator even though every delivery lane already
writes a classified `failure_reason` onto the terminal receipt.

These tests cover both halves of the fix:
  1. `_read_classified_failure_reason` correctly extracts the reason from a
     real t0_receipts.ndjson-shaped file, for two distinct causes.
  2. The CLI's failure branch actually surfaces that distinct reason instead
     of the generic paragraph, end-to-end, keyed by the dispatch's own
     (deterministic, patched) dispatch_id.

VNX_DISPATCH_LEGACY=1 forces deliver_via_door onto the legacy (mocked)
`deliver_with_recovery` branch, matching the existing preflight test harness —
no real dispatch is staged or spawned.
"""

from __future__ import annotations

import json
import shutil
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

_REAL_WHICH = shutil.which


def _make_agent(base: Path, name: str = "hello-world", default_instruction: str = "Say hi") -> Path:
    agent_dir = base / "examples" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
    (agent_dir / "config.yaml").write_text(
        f'governance_profile: minimal\ndefault_instruction: "{default_instruction}"\n'
    )
    return agent_dir


class _FakeUUID:
    def __init__(self, hex_value: str):
        self.hex = hex_value


def _write_receipt(receipts_file: Path, dispatch_id: str, failure_reason: str) -> None:
    receipts_file.parent.mkdir(parents=True, exist_ok=True)
    with receipts_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "event_type": "subprocess_completion",
            "dispatch_id": dispatch_id,
            "status": "failed",
            "failure_reason": failure_reason,
        }) + "\n")


def _run_dispatch_with_fixed_id(
    tmp_path: Path, monkeypatch, *, uuid_hex: str, door_success: bool = False,
) -> tuple:
    """Invoke vnx_dispatch_agent with a deterministic dispatch_id and an
    explicit, isolated VNX_DATA_DIR so the test controls exactly what
    t0_receipts.ndjson contains. Returns (returncode, dispatch_id, data_dir).
    """
    _make_agent(tmp_path)
    monkeypatch.setenv("VNX_DISPATCH_LEGACY", "1")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    from vnx_cli import _engine
    with patch.object(_engine, "engine_root", return_value=tmp_path), \
         patch("vnx_cli.commands.dispatch_agent._engine.ensure_engine_on_path"), \
         patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("vnx_cli.commands.dispatch_agent.uuid.uuid4", return_value=_FakeUUID(uuid_hex)), \
         patch.dict(
             "sys.modules",
             {"subprocess_dispatch": MagicMock(deliver_with_recovery=lambda **kw: door_success)},
         ):
        from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
        args = Namespace(agent="hello-world", instruction=None, model="sonnet", project_dir=str(tmp_path))
        rc = vnx_dispatch_agent(args)

    dispatch_id = f"D-{uuid_hex[:8]}"
    return rc, dispatch_id, data_dir


class TestReadClassifiedFailureReason:
    """Unit coverage for the extraction helper against a controlled receipts file."""

    def test_extracts_reason_for_matching_dispatch_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        receipts_file = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipt(receipts_file, "D-aaa11111", "CLI binary not found in PATH: 'claude'")

        from vnx_cli.commands.dispatch_agent import _read_classified_failure_reason  # type: ignore[import]
        reason = _read_classified_failure_reason("D-aaa11111", "vnx-dev")
        assert reason == "CLI binary not found in PATH: 'claude'"

    def test_two_distinct_dispatches_yield_two_distinct_reasons(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        receipts_file = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipt(receipts_file, "D-quota0001", "rate limit or HTTP 429 — quota exhausted")
        _write_receipt(receipts_file, "D-timeout01", "interactive_ready_timeout")

        from vnx_cli.commands.dispatch_agent import _read_classified_failure_reason  # type: ignore[import]
        quota_reason = _read_classified_failure_reason("D-quota0001", "vnx-dev")
        timeout_reason = _read_classified_failure_reason("D-timeout01", "vnx-dev")

        assert quota_reason == "rate limit or HTTP 429 — quota exhausted"
        assert timeout_reason == "interactive_ready_timeout"
        assert quota_reason != timeout_reason

    def test_no_matching_receipt_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        receipts_file = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipt(receipts_file, "D-someother", "unrelated reason")

        from vnx_cli.commands.dispatch_agent import _read_classified_failure_reason  # type: ignore[import]
        assert _read_classified_failure_reason("D-notfound1", "vnx-dev") is None

    def test_missing_receipts_file_returns_none_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "does-not-exist"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.dispatch_agent import _read_classified_failure_reason  # type: ignore[import]
        assert _read_classified_failure_reason("D-anything01", "vnx-dev") is None

    def test_non_failure_status_is_ignored(self, tmp_path, monkeypatch):
        """A 'done' receipt for the same dispatch_id must not be reported as a
        failure reason (status is not in HARD_FAILURE_STATUSES)."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        receipts_file = tmp_path / "state" / "t0_receipts.ndjson"
        receipts_file.parent.mkdir(parents=True, exist_ok=True)
        with receipts_file.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "dispatch_id": "D-succeeded",
                "status": "done",
                "failure_reason": None,
            }) + "\n")

        from vnx_cli.commands.dispatch_agent import _read_classified_failure_reason  # type: ignore[import]
        assert _read_classified_failure_reason("D-succeeded", "vnx-dev") is None


class TestFailureMessageSurfacesClassifiedReason:
    """End-to-end: the CLI's own stderr differs per failure cause."""

    def test_missing_binary_and_timeout_produce_distinct_messages(self, tmp_path, monkeypatch, capsys):
        # The receipt is written BEFORE invocation, keyed by the dispatch_id the
        # (patched) uuid will produce — mirrors a real dispatch, where the failing
        # lane's receipt is already on disk by the time the CLI reports failure.
        uuid_hex_1 = "aaaaaaaaaaaa"
        data_dir_1 = tmp_path / "run1" / "data"
        _write_receipt(
            data_dir_1 / "state" / "t0_receipts.ndjson",
            f"D-{uuid_hex_1[:8]}",
            "CLI binary not found in PATH: 'claude'",
        )
        rc1, _dispatch_id1, _data_dir1 = _run_dispatch_with_fixed_id(
            tmp_path / "run1", monkeypatch, uuid_hex=uuid_hex_1,
        )
        message1 = capsys.readouterr().err

        uuid_hex_2 = "bbbbbbbbbbbb"
        data_dir_2 = tmp_path / "run2" / "data"
        _write_receipt(
            data_dir_2 / "state" / "t0_receipts.ndjson",
            f"D-{uuid_hex_2[:8]}",
            "interactive_ready_timeout",
        )
        rc2, _dispatch_id2, _data_dir2 = _run_dispatch_with_fixed_id(
            tmp_path / "run2", monkeypatch, uuid_hex=uuid_hex_2,
        )
        message2 = capsys.readouterr().err

        assert rc1 == 1 and rc2 == 1
        assert "CLI binary not found in PATH: 'claude'" in message1
        assert "interactive_ready_timeout" in message2
        assert message1 != message2
        # Neither classified message should still be pointing the operator
        # elsewhere to go find what the code can now say directly.
        assert "CLI binary not found in PATH: 'claude'" not in message2
        assert "interactive_ready_timeout" not in message1

    def test_no_receipt_falls_back_to_generic_message(self, tmp_path, monkeypatch, capsys):
        rc, _dispatch_id, _data_dir = _run_dispatch_with_fixed_id(
            tmp_path, monkeypatch, uuid_hex="cccccccccccc",
        )
        message = capsys.readouterr().err
        assert rc == 1
        assert "A common cause is a missing or unauthenticated worker CLI" in message
