"""Tests for gate_recorder.record_terminal_result (OI-1093).

record_terminal_result is the single write path a hand-authored JSON cannot
route through unnoticed: it refuses to persist a terminal (pass/fail) gate
result that carries no producer identity (dispatch_id).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_recorder import record_terminal_result


def test_terminal_pass_without_dispatch_id_is_refused(tmp_path):
    payload = {
        "gate": "kimi_gate",
        "pr_id": "378",
        "status": "passed",
        "contract_hash": "abc123",
        "report_path": "/tmp/report.md",
    }
    with pytest.raises(ValueError, match="producer identity"):
        record_terminal_result(
            gate="kimi_gate", pr_id="378",
            result_path=tmp_path / "pr-378-kimi_gate.json",
            payload=payload,
        )
    assert not (tmp_path / "pr-378-kimi_gate.json").exists(), (
        "refused write must not leave a partial file behind"
    )


def test_terminal_fail_without_dispatch_id_is_refused(tmp_path):
    payload = {"gate": "kimi_gate", "pr_id": "378", "status": "fail"}
    with pytest.raises(ValueError, match="producer identity"):
        record_terminal_result(
            gate="kimi_gate", pr_id="378",
            result_path=tmp_path / "pr-378-kimi_gate.json",
            payload=payload,
        )


def test_terminal_result_with_dispatch_id_writes_unchanged(tmp_path):
    payload = {
        "gate": "kimi_gate",
        "pr_id": "904",
        "status": "pass",
        "provider": "kimi",
        "model": "kimi-k2-7-code",
        "dispatch_id": "kimi-gate-pr904-1782239641",
    }
    out = tmp_path / "pr-904-kimi_gate.json"
    result_path = record_terminal_result(
        gate="kimi_gate", pr_id="904", result_path=out, payload=payload,
    )
    assert result_path == out
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_non_terminal_result_without_dispatch_id_is_not_held_to_identity(tmp_path):
    """A non-terminal status (e.g. pending) is not gate evidence yet, so it is
    not required to carry producer identity."""
    payload = {"gate": "kimi_gate", "pr_id": "1", "status": "pending"}
    out = tmp_path / "pr-1-kimi_gate.json"
    record_terminal_result(gate="kimi_gate", pr_id="1", result_path=out, payload=payload)
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_write_is_atomic_no_tmp_file_left_behind(tmp_path):
    payload = {"gate": "kimi_gate", "pr_id": "905", "status": "pass", "dispatch_id": "kimi-gate-pr905-1"}
    out = tmp_path / "pr-905-kimi_gate.json"
    record_terminal_result(gate="kimi_gate", pr_id="905", result_path=out, payload=payload)
    assert out.exists()
    assert not (tmp_path / "pr-905-kimi_gate.json.tmp").exists()


def test_creates_parent_directory(tmp_path):
    out = tmp_path / "nested" / "results" / "pr-1-kimi_gate.json"
    payload = {"gate": "kimi_gate", "pr_id": "1", "status": "pass", "dispatch_id": "kimi-gate-pr1-1"}
    record_terminal_result(gate="kimi_gate", pr_id="1", result_path=out, payload=payload)
    assert out.exists()
