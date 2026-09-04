"""tests/test_dispatch_envelope_paid_lane_budget.py — golf 2B wiring.

dispatch_cli.py (the door) calls run_envelope_plan directly for every
kimi/glm/deepseek dispatch fired through `vnx dispatch` — this is the REAL
production path for paid-lane spend (dispatch_cli.py itself is out of scope
for this change; run_envelope_plan is the un-evadable choke point it calls
into). This is the RED/GREEN behavior test: a paid-lane provider whose daily
budget is already exhausted must be refused BEFORE a worktree is even
created — not just logged, and not after money-shaped work already started.

Mirrors tests/test_dispatch_envelope_plan.py::TestEnvelopeWorktreeIsolation's
mocking pattern (create_dispatch_worktree / ProviderAdapter.run / _govern).
"""

from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_envelope import ProviderAdapter, run_envelope_plan  # noqa: E402
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_plan import ExecutionPlan  # noqa: E402
from dispatch_spec import Isolation, Provider  # noqa: E402
import paid_lane_budget as plb  # noqa: E402

_FAKE_WT_PATH = Path("/tmp/fake-worktrees/budget-test")


def _today_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00Z")


def _write_exhausted_ledger(state_dir: Path, *, provider: str = "glm-harness", cost_usd: float = 999.0) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = state_dir / "t0_receipts.ndjson"
    with ledger.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"provider": provider, "cost_usd": cost_usd, "timestamp": _today_ts()}) + "\n")


def _make_plan(tmp_path: Path, provider: Provider, dispatch_id: str) -> ExecutionPlan:
    instruction_file = tmp_path / f"instruction-{dispatch_id}.md"
    instruction_file.write_text("# Test dispatch\nDo something.", encoding="utf-8")
    sha256 = hashlib.sha256(instruction_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id="vnx-dev",
        provider=provider,
        model="model-under-test",
        lane="provider",
        adapter="provider",
        target_id="T1",
        billing="provider_metered",
        serialization_class=None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="D1",
        instruction_sha256=sha256,
    )


@pytest.fixture(autouse=True)
def _budget_env(monkeypatch):
    monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
    monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
    yield


class TestRunEnvelopePlanBudgetRefusal:
    def test_glm_harness_over_budget_refuses_before_worktree_creation(self, tmp_path):
        plan = _make_plan(tmp_path, Provider.GLM_HARNESS, "budget-envelope-glm")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _write_exhausted_ledger(state_dir)
        fake_receipt = state_dir / "t0_receipts.ndjson"

        adapter_calls: list = []

        def spy_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None, role=None):
            adapter_calls.append({"cwd": cwd})
            from envelope_types import _AdapterResult
            return _AdapterResult(returncode=0, completion_text="done", status="success")

        with patch("dispatch_worktree_isolation.create_dispatch_worktree") as mock_create, \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree") as mock_remove, \
             patch.object(ProviderAdapter, "run", spy_adapter_run), \
             patch("dispatch_envelope._govern", return_value=(None, fake_receipt)) as mock_govern:
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "failure"
        assert result.returncode == 1
        assert result.error is not None
        assert "budget" in result.error.lower()
        assert adapter_calls == [], "ProviderAdapter.run must NOT be called over budget"
        mock_create.assert_not_called(), "worktree must not be created for a refused dispatch"
        mock_remove.assert_not_called()
        mock_govern.assert_called_once()

    def test_litellm_deepseek_over_budget_refuses(self, tmp_path):
        """The cap is shared: a glm-harness overspend also blocks litellm:deepseek."""
        plan = _make_plan(tmp_path, Provider.LITELLM_DEEPSEEK, "budget-envelope-ds")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _write_exhausted_ledger(state_dir, provider="glm-harness")
        fake_receipt = state_dir / "t0_receipts.ndjson"

        with patch("dispatch_worktree_isolation.create_dispatch_worktree") as mock_create, \
             patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "failure"
        mock_create.assert_not_called()


class TestRunEnvelopePlanBudgetDoesNotOverBlock:
    def test_under_budget_still_creates_worktree_and_runs(self, tmp_path):
        plan = _make_plan(tmp_path, Provider.GLM_HARNESS, "budget-envelope-ok")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        adapter_calls: list = []

        def fake_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None, role=None):
            adapter_calls.append({"cwd": cwd})
            from envelope_types import _AdapterResult
            return _AdapterResult(returncode=0, completion_text="done", status="success")

        _fake_consumer_root = tmp_path / "consumer-root"
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(ProviderAdapter, "run", fake_adapter_run), \
             patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "success"
        mock_create.assert_called_once()
        assert adapter_calls, "ProviderAdapter.run should have been called (under budget)"

    def test_kimi_unaffected_by_exhausted_paid_lane_budget(self, tmp_path):
        """kimi is CLI-OAuth, not DEEPSEEK_API_KEY/OPENROUTER_API_KEY — must
        not be blocked by an exhausted paid-lane budget."""
        plan = _make_plan(tmp_path, Provider.KIMI, "budget-envelope-kimi")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _write_exhausted_ledger(state_dir)
        fake_receipt = state_dir / "t0_receipts.ndjson"

        def fake_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None, role=None):
            from envelope_types import _AdapterResult
            return _AdapterResult(returncode=0, completion_text="done", status="success")

        _fake_consumer_root = tmp_path / "consumer-root"
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(ProviderAdapter, "run", fake_adapter_run), \
             patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "success"
        mock_create.assert_called_once()
