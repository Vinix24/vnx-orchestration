"""tests/test_paid_lane_budget.py — golf 2B: daily spend cap for metered lanes.

Covers scripts/lib/paid_lane_budget.py in isolation:
  - provider -> paid-lane-key classification (architectural, not observed cost)
  - get_daily_budget_usd() env parsing + fail-closed <=0 semantics
  - spent_today_usd() reading the receipts ledger: date filter, provider
    filter, malformed-line tolerance, missing-cost tolerance
  - is_budget_exhausted() / enforce_daily_budget() incl. the operator override
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import paid_lane_budget as plb  # noqa: E402


# ---------------------------------------------------------------------------
# paid_lane_env_key / is_paid_lane — classification is architectural
# ---------------------------------------------------------------------------

class TestPaidLaneClassification:
    @pytest.mark.parametrize(
        "provider,expected_key",
        [
            ("deepseek-harness", "DEEPSEEK_API_KEY"),
            ("glm-harness", "OPENROUTER_API_KEY"),
            ("litellm:deepseek", "DEEPSEEK_API_KEY"),
            ("litellm:zai", "OPENROUTER_API_KEY"),
            ("litellm:openrouter", "OPENROUTER_API_KEY"),
            ("litellm:openrouter:openai/gpt-4o-mini", "OPENROUTER_API_KEY"),
            # case-insensitive
            ("GLM-HARNESS", "OPENROUTER_API_KEY"),
        ],
    )
    def test_paid_providers_map_to_their_key(self, provider, expected_key):
        assert plb.paid_lane_env_key(provider) == expected_key
        assert plb.is_paid_lane(provider) is True

    @pytest.mark.parametrize(
        "provider",
        [
            "claude", "codex", "gemini", "kimi", "local-gemma",
            "litellm:moonshot",  # own key (MOONSHOT_API_KEY), out of scope
            "litellm", "litellm:bedrock", "litellm:anthropic", "litellm:ollama",
            None, "", "   ",
        ],
    )
    def test_non_paid_providers_are_not_classified_paid(self, provider):
        assert plb.paid_lane_env_key(provider) is None
        assert plb.is_paid_lane(provider) is False

    def test_a_lane_that_reports_zero_cost_is_still_classified_paid(self):
        """The classification must never derive from observed cost_usd — a
        pricing-registry miss stamps cost_usd=0.0 on a real charge (see module
        docstring). is_paid_lane looks only at the provider string.
        """
        assert plb.is_paid_lane("glm-harness") is True
        assert plb.is_paid_lane("litellm:deepseek") is True


# ---------------------------------------------------------------------------
# get_daily_budget_usd
# ---------------------------------------------------------------------------

class TestDailyBudget:
    def test_default_budget(self, monkeypatch):
        monkeypatch.delenv(plb.ENV_DAILY_BUDGET, raising=False)
        assert plb.get_daily_budget_usd() == plb.DEFAULT_DAILY_BUDGET_USD

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.50")
        assert plb.get_daily_budget_usd() == 1.50

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "not-a-number")
        assert plb.get_daily_budget_usd() == plb.DEFAULT_DAILY_BUDGET_USD

    def test_negative_env_clamps_to_zero(self, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "-5")
        assert plb.get_daily_budget_usd() == 0.0


# ---------------------------------------------------------------------------
# spent_today_usd — reads the receipts ledger directly (see module docstring
# for why: cost_tracker.py::recent_cost_per_hour is the existing precedent
# for reading t0_receipts.ndjson for cost aggregation).
# ---------------------------------------------------------------------------

def _write_receipts(path: Path, receipts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in receipts:
            fh.write(json.dumps(r) + "\n")


def _today_ts(hour: int = 12) -> str:
    return datetime.now(timezone.utc).strftime(f"%Y-%m-%dT{hour:02d}:00:00Z")


def _yesterday_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z")


class TestSpentTodayUsd:
    def test_missing_ledger_is_zero(self, tmp_path):
        assert plb.spent_today_usd(tmp_path) == 0.0

    def test_empty_ledger_is_zero(self, tmp_path):
        (tmp_path / plb.RECEIPTS_FILE_NAME).write_text("")
        assert plb.spent_today_usd(tmp_path) == 0.0

    def test_finds_a_known_real_paid_lane_spend(self, tmp_path):
        """Nul-is-eerst-een-meetfout: prove the scan finds a KNOWN nonzero
        spend before trusting any zero result elsewhere in this file. Mirrors
        the shape of a real glm-harness receipt (cost_usd: 0.04359888, from
        2026-09-04 production data).
        """
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 0.04359888, "timestamp": _today_ts()},
        ])
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.04359888)

    def test_sums_multiple_paid_lane_receipts_today(self, tmp_path):
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 0.04, "timestamp": _today_ts(8)},
            {"provider": "glm-harness", "cost_usd": 0.05, "timestamp": _today_ts(9)},
            {"provider": "deepseek-harness", "cost_usd": 0.03, "timestamp": _today_ts(10)},
        ])
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.12)

    def test_ignores_non_paid_lane_providers(self, tmp_path):
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "claude", "cost_usd": 999.0, "timestamp": _today_ts()},
            {"provider": "kimi", "cost_usd": 999.0, "timestamp": _today_ts()},
            {"provider": "glm-harness", "cost_usd": 0.05, "timestamp": _today_ts()},
        ])
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.05)

    def test_ignores_receipts_from_a_prior_day(self, tmp_path):
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 50.0, "timestamp": _yesterday_ts()},
            {"provider": "glm-harness", "cost_usd": 0.05, "timestamp": _today_ts()},
        ])
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.05)

    def test_tolerates_malformed_lines(self, tmp_path):
        ledger = tmp_path / plb.RECEIPTS_FILE_NAME
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("w", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
            fh.write(json.dumps({"provider": "glm-harness", "cost_usd": 0.07, "timestamp": _today_ts()}) + "\n")
            fh.write("\n")
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.07)

    def test_missing_cost_usd_counts_as_zero_not_a_crash(self, tmp_path):
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": None, "timestamp": _today_ts()},
            {"provider": "glm-harness", "cost_usd": 0.02, "timestamp": _today_ts()},
        ])
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.02)

    def test_numeric_epoch_timestamp_is_handled(self, tmp_path):
        today_epoch = datetime.now(timezone.utc).replace(
            hour=12, minute=0, second=0, microsecond=0
        ).timestamp()
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 0.09, "timestamp": today_epoch},
        ])
        assert plb.spent_today_usd(tmp_path) == pytest.approx(0.09)


# ---------------------------------------------------------------------------
# is_budget_exhausted / enforce_daily_budget
# ---------------------------------------------------------------------------

class TestBudgetEnforcement:
    def test_non_paid_lane_never_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "0")
        assert plb.is_budget_exhausted("claude", tmp_path) is False
        # enforce_daily_budget must be a no-op (no raise) for a non-paid lane
        # even at budget=0.
        plb.enforce_daily_budget("kimi", tmp_path)

    def test_zero_budget_is_always_exhausted_for_paid_lane(self, tmp_path, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "0")
        assert plb.is_budget_exhausted("glm-harness", tmp_path) is True

    def test_under_budget_not_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "5.00")
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 0.37, "timestamp": _today_ts()},
        ])
        assert plb.is_budget_exhausted("glm-harness", tmp_path) is False
        plb.enforce_daily_budget("glm-harness", tmp_path)  # must not raise

    def test_over_budget_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 1.50, "timestamp": _today_ts()},
        ])
        assert plb.is_budget_exhausted("glm-harness", tmp_path) is True
        with pytest.raises(plb.PaidLaneBudgetExceededError) as excinfo:
            plb.enforce_daily_budget("glm-harness", tmp_path)
        assert "glm-harness" in str(excinfo.value)
        assert "1.50" in str(excinfo.value) or "1.500000" in str(excinfo.value)

    def test_exactly_at_budget_is_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 1.00, "timestamp": _today_ts()},
        ])
        assert plb.is_budget_exhausted("glm-harness", tmp_path) is True

    def test_a_different_paid_lane_shares_the_same_cap(self, tmp_path, monkeypatch):
        """The cap is on total paid-lane spend, not per-provider — a deepseek
        overspend must block a subsequent glm-harness dispatch too.
        """
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "deepseek-harness", "cost_usd": 1.20, "timestamp": _today_ts()},
        ])
        assert plb.is_budget_exhausted("litellm:zai", tmp_path) is True

    def test_operator_override_suppresses_the_raise(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        monkeypatch.setenv(plb.ENV_OVERRIDE, "1")
        _write_receipts(tmp_path / plb.RECEIPTS_FILE_NAME, [
            {"provider": "glm-harness", "cost_usd": 5.00, "timestamp": _today_ts()},
        ])
        import logging
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            plb.enforce_daily_budget("glm-harness", tmp_path)  # must not raise
        assert any("OVERRIDDEN" in rec.message for rec in caplog.records)
