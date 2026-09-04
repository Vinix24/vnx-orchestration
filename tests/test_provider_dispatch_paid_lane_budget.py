"""tests/test_provider_dispatch_paid_lane_budget.py — golf 2B wiring.

provider_dispatch.main() is the standalone-CLI side door onto the provider
lane (e.g. plan_gate_panel.py invokes this file as a subprocess directly,
bypassing dispatch_cli.py's door). This is the RED/GREEN behavior test: a
paid-lane provider whose daily budget is already exhausted must be refused
BEFORE the actual spawn handler runs — not just logged.

Mirrors tests/test_provider_dispatch_entry.py's pattern of patching the
provider's `_dispatch_*` function and asserting call/no-call.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch  # noqa: E402
import paid_lane_budget as plb  # noqa: E402


def _today_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00Z")


def _write_exhausted_ledger(state_dir: Path, *, provider: str = "glm-harness", cost_usd: float = 999.0) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = state_dir / "t0_receipts.ndjson"
    with ledger.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"provider": provider, "cost_usd": cost_usd, "timestamp": _today_ts()}) + "\n")


def _argv(provider: str, dispatch_id: str) -> list[str]:
    return [
        "--provider", provider,
        "--terminal-id", "T1",
        "--dispatch-id", dispatch_id,
        "--instruction", "noop",
    ]


@pytest.fixture(autouse=True)
def _budget_env(monkeypatch):
    """The autouse _vnx_data_dir_isolation fixture (conftest.py) already pins
    VNX_STATE_DIR to a fresh per-test tmp dir with no ledger present, so every
    test starts at spend=0. Pin the budget explicitly for readability.
    """
    monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
    monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
    yield


class TestPaidLaneBudgetRefusesSpawn:
    def test_glm_harness_over_budget_refuses_without_spawning(self, capsys):
        state_dir = Path(os.environ["VNX_STATE_DIR"])
        _write_exhausted_ledger(state_dir)

        with patch("provider_dispatch._dispatch_glm_harness") as mock_dispatch:
            rc = provider_dispatch.main(_argv("glm-harness", "budget-test-glm"))

        assert rc == 1
        mock_dispatch.assert_not_called()
        err = capsys.readouterr().err
        assert "budget" in err.lower()

    def test_litellm_zai_over_budget_refuses_without_spawning(self, capsys):
        """The cap is shared across paid lanes: a glm-harness overspend also
        blocks a litellm:zai dispatch, not just glm-harness itself."""
        state_dir = Path(os.environ["VNX_STATE_DIR"])
        _write_exhausted_ledger(state_dir, provider="glm-harness")

        with patch("provider_dispatch._dispatch_litellm") as mock_dispatch:
            rc = provider_dispatch.main(_argv("litellm:zai", "budget-test-zai"))

        assert rc == 1
        mock_dispatch.assert_not_called()

    def test_deepseek_harness_over_budget_refuses_without_spawning(self, capsys):
        state_dir = Path(os.environ["VNX_STATE_DIR"])
        _write_exhausted_ledger(state_dir, provider="deepseek-harness")

        with patch("provider_dispatch._dispatch_deepseek_harness") as mock_dispatch:
            rc = provider_dispatch.main(_argv("deepseek-harness", "budget-test-ds"))

        assert rc == 1
        mock_dispatch.assert_not_called()

    def test_refusal_writes_a_ledger_receipt(self):
        """Every refusal must land in t0_receipts.ndjson (provider_dispatch's
        own established contract for every pre-flight refusal — module
        docstring of _emit_refusal_receipt)."""
        state_dir = Path(os.environ["VNX_STATE_DIR"])
        _write_exhausted_ledger(state_dir)
        ledger = state_dir / "t0_receipts.ndjson"
        lines_before = ledger.read_text().count("\n")

        with patch("provider_dispatch._dispatch_glm_harness"):
            provider_dispatch.main(_argv("glm-harness", "budget-test-receipt"))

        lines_after = ledger.read_text().count("\n")
        assert lines_after == lines_before + 1
        last_line = [ln for ln in ledger.read_text().splitlines() if ln.strip()][-1]
        receipt = json.loads(last_line)
        assert receipt["dispatch_id"] == "budget-test-receipt"
        assert receipt["status"] == "blocked"


class TestPaidLaneBudgetDoesNotOverBlock:
    def test_under_budget_still_dispatches(self):
        """Sanity: with no prior spend recorded, a paid lane still runs (the
        gate must not block by default, only once the cap is actually met)."""
        with patch("provider_dispatch._dispatch_glm_harness", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(_argv("glm-harness", "budget-test-ok"))

        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_non_paid_lane_unaffected_by_exhausted_paid_lane_budget(self):
        """kimi draws on its own CLI-OAuth lane, not DEEPSEEK_API_KEY/
        OPENROUTER_API_KEY — an exhausted paid-lane budget must not block it."""
        state_dir = Path(os.environ["VNX_STATE_DIR"])
        _write_exhausted_ledger(state_dir)

        with patch("provider_dispatch._dispatch_kimi", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(_argv("kimi", "budget-test-kimi"))

        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_operator_override_allows_dispatch_through(self, monkeypatch):
        state_dir = Path(os.environ["VNX_STATE_DIR"])
        _write_exhausted_ledger(state_dir)
        monkeypatch.setenv(plb.ENV_OVERRIDE, "1")

        with patch("provider_dispatch._dispatch_glm_harness", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(_argv("glm-harness", "budget-test-override"))

        assert rc == 0
        mock_dispatch.assert_called_once()
