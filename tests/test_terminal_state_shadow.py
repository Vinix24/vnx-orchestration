#!/usr/bin/env python3
"""Tests for terminal_state.json shadow writer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import terminal_state_core as ts  # noqa: E402


def test_schema_validation_accepts_required_fields():
    record = {
        "terminal_id": "T1",
        "status": "claimed",
        "claimed_by": "dispatch-1",
        "claimed_at": "2026-02-10T12:00:00+00:00",
        "lease_expires_at": "2026-02-10T12:01:00+00:00",
        "last_activity": "2026-02-10T12:00:05+00:00",
        "version": 1,
    }
    ts.validate_terminal_record(record)


def test_schema_validation_rejects_missing_required_fields():
    bad_record = {
        "terminal_id": "T1",
        "status": "claimed",
        "version": 1,
    }
    bad_record.pop("status")
    with pytest.raises(ts.TerminalStateValidationError):
        ts.validate_terminal_record(bad_record)


def test_atomic_write_uses_temp_and_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    replace_calls = []
    real_replace = ts.os.replace

    def _tracking_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(ts.os, "replace", _tracking_replace)

    ts.update_terminal_state(
        tmp_path,
        ts.TerminalUpdate(
            terminal_id="T1",
            status="claimed",
            claimed_by="dispatch-1",
            claimed_at="2026-02-10T12:00:00+00:00",
            lease_expires_at="2026-02-10T12:01:00+00:00",
            last_activity="2026-02-10T12:00:01+00:00",
        ),
    )

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert src != dst
    assert str(dst).endswith("terminal_state.json")
    assert not list(tmp_path.glob("terminal_state.json.*.tmp"))


def test_version_increments_per_terminal_update(tmp_path: Path):
    r1 = ts.update_terminal_state(
        tmp_path,
        ts.TerminalUpdate(
            terminal_id="T1",
            status="claimed",
            claimed_by="dispatch-1",
            claimed_at="2026-02-10T12:00:00+00:00",
            lease_expires_at="2026-02-10T12:01:00+00:00",
            last_activity="2026-02-10T12:00:01+00:00",
        ),
    )
    r2 = ts.update_terminal_state(
        tmp_path,
        ts.TerminalUpdate(
            terminal_id="T1",
            status="active",
            lease_expires_at="2026-02-10T12:02:00+00:00",
            last_activity="2026-02-10T12:01:30+00:00",
        ),
    )
    r3 = ts.update_terminal_state(
        tmp_path,
        ts.TerminalUpdate(
            terminal_id="T2",
            status="claimed",
            claimed_by="dispatch-2",
            claimed_at="2026-02-10T12:10:00+00:00",
            lease_expires_at="2026-02-10T12:11:00+00:00",
            last_activity="2026-02-10T12:10:01+00:00",
        ),
    )

    assert r1["version"] == 1
    assert r2["version"] == 2
    assert r3["version"] == 1

    state_doc = json.loads((tmp_path / "terminal_state.json").read_text(encoding="utf-8"))
    assert state_doc["terminals"]["T1"]["version"] == 2
    assert state_doc["terminals"]["T2"]["version"] == 1


def test_clear_claim_has_precedence_over_claim_fields(tmp_path: Path):
    ts.update_terminal_state(
        tmp_path,
        ts.TerminalUpdate(
            terminal_id="T1",
            status="claimed",
            claimed_by="dispatch-1",
            claimed_at="2026-02-10T12:00:00+00:00",
            lease_expires_at="2026-02-10T12:01:00+00:00",
            last_activity="2026-02-10T12:00:01+00:00",
        ),
    )

    record = ts.update_terminal_state(
        tmp_path,
        ts.TerminalUpdate(
            terminal_id="T1",
            status="idle",
            claimed_by="dispatch-should-be-ignored",
            clear_claim=True,
            last_activity="2026-02-10T12:02:00+00:00",
        ),
    )

    assert record["claimed_by"] is None
    assert record["claimed_at"] is None
    assert record["lease_expires_at"] is None
