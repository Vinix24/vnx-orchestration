#!/usr/bin/env python3
"""ADR-028 Phase 4 — judge-binding policy. The critical invariant: a SENSITIVE action can
NEVER bind to the judge without an explicit operator_approval, even when the judge is enabled."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import decision_binding as db  # noqa: E402


class TestFlagGate:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_JUDGE_ENABLED", raising=False)
        assert db.judge_binding_enabled() is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_ENABLED", "1")
        assert db.judge_binding_enabled() is True


class TestIsSensitive:
    def test_sensitive_actions(self):
        for a in ("merge_pr", "close_track", "override_gate", "push_main", "publish"):
            assert db.is_sensitive(a) is True

    def test_case_insensitive(self):
        assert db.is_sensitive("MERGE_PR") is True

    def test_routine_actions_not_sensitive(self):
        for a in ("skip", "re_dispatch", "analyze_failure", "escalate_to_t0"):
            assert db.is_sensitive(a) is False


class TestBindingVerdict:
    def test_judge_disabled_never_binding(self, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_JUDGE_ENABLED", raising=False)
        v = db.binding_verdict("skip")
        assert v["binding"] is False
        # sensitive action still flagged for approval even when disabled
        assert db.binding_verdict("merge_pr")["requires_operator_approval"] is True

    def test_enabled_routine_is_binding(self, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_ENABLED", "1")
        v = db.binding_verdict("skip")
        assert v["binding"] is True and v["requires_operator_approval"] is False

    def test_enabled_sensitive_without_approval_not_binding(self, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_ENABLED", "1")
        v = db.binding_verdict("merge_pr")
        assert v["binding"] is False and v["requires_operator_approval"] is True

    def test_enabled_sensitive_with_approval_binds(self, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_ENABLED", "1")
        v = db.binding_verdict("merge_pr", has_operator_approval=True)
        assert v["binding"] is True


class TestOperatorApproval:
    def test_records_approval_receipt(self, tmp_path):
        rec = db.record_operator_approval(
            "d1", "merge_pr", "vincent", note="ok", state_dir=tmp_path
        )
        assert rec["event"] == "operator_approval" and rec["operator"] == "vincent"
        led = tmp_path / db.APPROVAL_LEDGER
        assert led.exists()
        row = json.loads(led.read_text(encoding="utf-8").splitlines()[0])
        assert row["decision_id"] == "d1" and row["action"] == "merge_pr"
