#!/usr/bin/env python3
"""ADR-028 Phase 3 — decision judge fast-path. Verify the classifier only short-circuits
TRIVIAL no-ops, and that DecisionRouter.decide() is behaviour-unchanged when the flag is off."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import decision_fast_path as fp  # noqa: E402
from llm_decision_router import DecisionRouter  # noqa: E402


class TestClassify:
    def test_clean_receipt_is_trivial_skip(self):
        res = fp.classify({"receipt": {"status": "ok"}}, "skip")
        assert res is not None and res.action == "skip" and res.backend_used == "fast-path"

    def test_failed_receipt_defers_to_judge(self):
        assert fp.classify({"receipt": {"status": "failed"}}, "re_dispatch") is None

    def test_clean_but_silent_defers_to_judge(self):
        assert fp.classify(
            {"receipt": {"status": "ok"}, "terminal_silence_seconds": 1200}, "skip"
        ) is None

    def test_clean_but_escalated_defers_to_judge(self):
        assert fp.classify({"receipt": {"status": "ok"}, "needs_review": True}, "skip") is None

    def test_no_receipt_defers_to_judge(self):
        assert fp.classify({"terminal_silence_seconds": 10}, "skip") is None

    def test_string_silence_is_coerced(self):
        # codex gate: a numeric STRING silence must still count as a pending signal.
        assert fp.classify(
            {"receipt": {"status": "ok"}, "terminal_silence_seconds": "1200"}, "skip"
        ) is None

    def test_unparseable_silence_defers(self):
        assert fp.classify(
            {"receipt": {"status": "ok"}, "terminal_silence_seconds": "soon"}, "skip"
        ) is None

    def test_other_escalation_keys_defer(self):
        for k in ("needs_human", "error", "failure", "incident"):
            assert fp.classify({"receipt": {"status": "ok"}, k: True}, "skip") is None, k

    def test_invalid_silence_values_defer(self):
        # codex re-gate: bool / NaN / inf are not valid durations -> must defer, not skip.
        for bad in (True, False, float("nan"), float("inf"), float("-inf"), "nan", "-inf"):
            assert fp.classify(
                {"receipt": {"status": "ok"}, "terminal_silence_seconds": bad}, "skip"
            ) is None, bad

    def test_valid_low_silence_still_trivial(self):
        # a genuine small numeric silence is still a clean no-op.
        res = fp.classify(
            {"receipt": {"status": "ok"}, "terminal_silence_seconds": 12.5}, "skip"
        )
        assert res is not None and res.action == "skip"


class TestFlagGate:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_FAST_PATH", raising=False)
        assert fp.fast_path_enabled() is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_FAST_PATH", "1")
        assert fp.fast_path_enabled() is True


class TestDecideWiring:
    def test_flag_off_unchanged_behaviour(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_FAST_PATH", raising=False)
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        # clean receipt -> rule-based says skip, but via the BACKEND (not fast-path).
        result = router.decide({"receipt": {"status": "ok"}}, "skip")
        assert result.action == "skip" and result.backend_used == "dry-run"

    def test_flag_on_trivial_uses_fast_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_FAST_PATH", "1")
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        result = router.decide({"receipt": {"status": "ok"}}, "skip")
        assert result.action == "skip" and result.backend_used == "fast-path"

    def test_flag_on_nontrivial_falls_through_to_backend(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_FAST_PATH", "1")
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        # failed receipt is non-trivial -> fast-path returns None -> backend decides.
        result = router.decide({"receipt": {"status": "failed"}}, "re_dispatch")
        assert result.action == "re_dispatch" and result.backend_used == "dry-run"
