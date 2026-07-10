#!/usr/bin/env python3
"""Tests for scripts/lib/decision_shadow.py — ADR-028 Phase 2 decision-judge SHADOW mode.

Zero-risk contract: default OFF (no-op), separate ledgers (never t0_receipts.ndjson),
comparator logs only, fail-open."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import decision_shadow as ds  # noqa: E402


CTX_FAILED = {"receipt": {"status": "failed"}}
Q = "advance or retry this dispatch?"


class TestFlagGate:
    def test_default_off_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_JUDGE_SHADOW", raising=False)
        assert ds.shadow_enabled() is False
        assert ds.record_advisory("d1", CTX_FAILED, Q, state_dir=tmp_path) is None
        # nothing written
        assert not (tmp_path / ds.ADVISORY_LEDGER).exists()

    def test_enabled_flag(self, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        assert ds.shadow_enabled() is True


class TestRecordAdvisory:
    def test_writes_advisory_ledger_when_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        advisory = ds.record_advisory("d1", CTX_FAILED, Q, state_dir=tmp_path)
        assert advisory is not None and advisory["action"] == "re_dispatch"
        led = tmp_path / ds.ADVISORY_LEDGER
        assert led.exists() and "decision_advisory" in led.read_text(encoding="utf-8")

    def test_does_not_touch_governed_receipt_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        ds.record_advisory("d1", CTX_FAILED, Q, state_dir=tmp_path)
        # ISOLATION: shadow output must never land in the governed audit trail.
        assert not (tmp_path / "t0_receipts.ndjson").exists()

    def test_custom_judge_injected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")

        class _J:
            def to_dict(self):
                return {"action": "custom", "confidence": 0.5}

        advisory = ds.record_advisory(
            "d1", {}, Q, judge=lambda c, q: _J(), state_dir=tmp_path
        )
        assert advisory["action"] == "custom"

    def test_fail_open_on_broken_judge(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")

        def _boom(context, question):
            raise ValueError("judge exploded")

        # must NOT raise into the real decision path
        assert ds.record_advisory("d1", {}, Q, judge=_boom, state_dir=tmp_path) is None


class TestComparator:
    def test_agree_and_disagree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        advisory = {"action": "re_dispatch", "confidence": 0.8}
        agree = ds.compare_and_log("d1", "re_dispatch", advisory, state_dir=tmp_path)
        disagree = ds.compare_and_log("d2", "skip", advisory, state_dir=tmp_path)
        assert agree["agree"] is True
        assert disagree["agree"] is False
        assert (tmp_path / ds.DIVERGENCE_LEDGER).exists()

    def test_noop_when_advisory_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        assert ds.compare_and_log("d1", "skip", None, state_dir=tmp_path) is None

    def test_noop_when_shadow_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_JUDGE_SHADOW", raising=False)
        assert ds.compare_and_log("d1", "skip", {"action": "skip"}, state_dir=tmp_path) is None


class TestDivergenceSummary:
    def test_summary_counts_and_rate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        adv = {"action": "re_dispatch", "confidence": 0.8}
        ds.compare_and_log("d1", "re_dispatch", adv, state_dir=tmp_path)  # agree
        ds.compare_and_log("d2", "re_dispatch", adv, state_dir=tmp_path)  # agree
        ds.compare_and_log("d3", "skip", adv, state_dir=tmp_path)          # disagree
        summary = ds.divergence_summary(state_dir=tmp_path)
        assert summary == {"total": 3, "agree": 2, "disagree": 1, "agree_rate": pytest.approx(2 / 3)}

    def test_summary_empty_ledger(self, tmp_path):
        summary = ds.divergence_summary(state_dir=tmp_path)
        assert summary == {"total": 0, "agree": 0, "disagree": 0, "agree_rate": None}
