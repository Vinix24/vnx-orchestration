#!/usr/bin/env python3
"""ADR-028 Phase 2 — verify DecisionRouter.decide() is wired to the shadow (fail-open,
flag-gated) and that the shadow NEVER affects the authoritative decision."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from llm_decision_router import DecisionRouter  # noqa: E402
import decision_shadow as ds  # noqa: E402

CTX_FAILED = {"receipt": {"status": "failed"}}  # rule-based -> re_dispatch


class TestDecideShadowWiring:
    def test_shadow_off_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_DECISION_JUDGE_SHADOW", raising=False)
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        result = router.decide(CTX_FAILED, "re_dispatch")
        assert result.action == "re_dispatch"
        assert not (tmp_path / "state" / ds.ADVISORY_LEDGER).exists()
        assert not (tmp_path / "state" / ds.DIVERGENCE_LEDGER).exists()

    def test_shadow_on_writes_ledgers_and_result_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        result = router.decide(CTX_FAILED, "re_dispatch")
        # authoritative decision is unchanged by the shadow
        assert result.action == "re_dispatch"
        sd = tmp_path / "state"
        assert (sd / ds.ADVISORY_LEDGER).exists()
        assert (sd / ds.DIVERGENCE_LEDGER).exists()
        # rule-based shadow judge agrees with the rule-based router on this context
        summary = ds.divergence_summary(state_dir=sd)
        assert summary["total"] == 1 and summary["agree"] == 1

    def test_shadow_never_touches_governed_receipts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        router.decide(CTX_FAILED, "re_dispatch")
        assert not (tmp_path / "state" / "t0_receipts.ndjson").exists()

    def test_broken_shadow_judge_does_not_break_decide(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DECISION_JUDGE_SHADOW", "1")
        # point the shadow judge at a backend that will fail to import/execute cleanly;
        # decide() must still return the authoritative rule-based decision.
        monkeypatch.setenv("VNX_DECISION_JUDGE_BACKEND", "ollama")
        router = DecisionRouter(data_dir=tmp_path, backend="dry-run")
        result = router.decide(CTX_FAILED, "re_dispatch")
        assert result.action == "re_dispatch"  # unaffected, fail-open
