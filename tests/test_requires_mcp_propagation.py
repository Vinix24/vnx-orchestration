"""test_requires_mcp_propagation.py — OI-865: requires_mcp must survive the plan boundary.

The field exists in DispatchSpec (dispatch_spec.py:100) and is read by the door
(dispatch_cli.load_spec), but was dropped at compile_plan: ExecutionPlan had no
field and _execute_claude never forwarded it, so a dispatch staged with
requires_mcp:true lost its ambient MCP under scoped mode (blocks #1252's default
worker-capability scoping flip).

Every test in this file is RED on origin/main (the field does not exist there)
and GREEN once the propagation is wired end-to-end:
  spec.requires_mcp -> compile_plan -> ExecutionPlan.requires_mcp
                     -> _execute_claude -> lane.dispatch(requires_mcp=...)
                     -> tmux CLI --requires-mcp -> lane.dispatch(requires_mcp=...)
"""
from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path

from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import _execute_claude
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan, RuntimeSnapshot, compile_plan
from dispatch_spec import DispatchSpec, Isolation, Provider, ValidatedSpec
from tmux_interactive_dispatch import InteractiveDispatchResult, TmuxInteractiveDispatch, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_instruction_file(tmp_path: Path, text: str = "# MCP propagation test\n") -> Path:
    f = tmp_path / "instruction.md"
    f.write_text(text, encoding="utf-8")
    return f


def _make_vspec(*, requires_mcp: bool | None = None, tmp_path: Path) -> ValidatedSpec:
    """Build a ValidatedSpec; requires_mcp=None omits the field (uses the dataclass default)."""
    kwargs: dict = {}
    if requires_mcp is not None:
        kwargs["requires_mcp"] = requires_mcp
    ifile = _fake_instruction_file(tmp_path)
    spec = DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="mcp-propagate-001",
        staging_id="staging-mcp-001",
        instruction_file=ifile,
        role="backend-developer",
        target_slot="T1",
        gate="human-promoted",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
        **kwargs,
    )
    instruction_text = ifile.read_text(encoding="utf-8")
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


def _healthy_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(staging_promoted=True)


def _make_mcp_plan(tmp_path: Path, *, requires_mcp: bool) -> ExecutionPlan:
    """A valid claude-lane plan with a matching instruction sha256 for _execute_claude."""
    ifile = _fake_instruction_file(tmp_path)
    sha = hashlib.sha256(ifile.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id="mcp-exec-001",
        project_id="vnx-dev",
        provider=Provider.CLAUDE,
        model="sonnet",
        lane="claude_tmux_subscription",
        adapter="tmux_claude",
        target_id="ephemeral",
        billing="subscription",
        serialization_class="claude-tmux",
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="verify_strict",
        deadline_seconds=3600,
        base_ref="origin/main",
        dispatch_paths=(),
        instruction_file=ifile,
        route_reason="D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12",
        instruction_sha256=sha,
        requires_mcp=requires_mcp,
    )


# ---------------------------------------------------------------------------
# Plan boundary — compile_plan must carry requires_mcp (kernel DoD)
# ---------------------------------------------------------------------------

class TestCompilePlanPropagatesRequiresMcp:
    def test_true_reaches_plan(self, tmp_path: Path) -> None:
        plan = compile_plan(_make_vspec(requires_mcp=True, tmp_path=tmp_path), _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.requires_mcp is True, "requires_mcp:true must survive compile_plan"

    def test_false_reaches_plan(self, tmp_path: Path) -> None:
        plan = compile_plan(_make_vspec(requires_mcp=False, tmp_path=tmp_path), _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.requires_mcp is False, "requires_mcp:false must survive compile_plan"

    def test_missing_field_defaults_false(self, tmp_path: Path) -> None:
        plan = compile_plan(_make_vspec(tmp_path=tmp_path), _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.requires_mcp is False, (
            "a spec without the field must default to False, matching DispatchSpec's default"
        )

    def test_default_matches_todays_lane_default(self, tmp_path: Path) -> None:
        """A missing field lands on exactly the value today's lane already uses.

        The tmux lane's dispatch() has always defaulted requires_mcp=False, so a
        spec that omits the field gets the same ambient-MCP behavior as today —
        the default must NOT silently mean "no MCP" for dispatches that already
        keep their MCP.
        """
        plan = compile_plan(_make_vspec(tmp_path=tmp_path), _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        lane_default = inspect.signature(TmuxInteractiveDispatch.dispatch).parameters[
            "requires_mcp"
        ].default
        assert plan.requires_mcp is lane_default is False

    def test_requires_mcp_changes_digest(self, tmp_path: Path) -> None:
        """MCP access alters worker behavior, so it must perturb the permit fingerprint."""
        plan_true = compile_plan(_make_vspec(requires_mcp=True, tmp_path=tmp_path), _healthy_snapshot())
        plan_false = compile_plan(_make_vspec(requires_mcp=False, tmp_path=tmp_path), _healthy_snapshot())
        assert isinstance(plan_true, ExecutionPlan)
        assert isinstance(plan_false, ExecutionPlan)
        assert plan_true.digest() != plan_false.digest(), (
            "digest() must distinguish requires_mcp: true from false"
        )


# ---------------------------------------------------------------------------
# _execute_claude must forward the plan field to the lane
# ---------------------------------------------------------------------------

class TestExecuteClaudeForwardsRequiresMcp:
    def test_true_reaches_lane(self, tmp_path: Path) -> None:
        plan = _make_mcp_plan(tmp_path, requires_mcp=True)
        permit = issue_permit(plan)
        with patch(
            "tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch",
            return_value=MagicMock(success=True),
        ) as mock_dispatch:
            _execute_claude(plan, permit, state_dir=tmp_path / "state", data_dir=tmp_path)
        mock_dispatch.assert_called_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["requires_mcp"] is True

    def test_false_reaches_lane(self, tmp_path: Path) -> None:
        plan = _make_mcp_plan(tmp_path, requires_mcp=False)
        permit = issue_permit(plan)
        with patch(
            "tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch",
            return_value=MagicMock(success=True),
        ) as mock_dispatch:
            _execute_claude(plan, permit, state_dir=tmp_path / "state", data_dir=tmp_path)
        mock_dispatch.assert_called_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["requires_mcp"] is False


# ---------------------------------------------------------------------------
# tmux CLI --requires-mcp must reach the lane
# ---------------------------------------------------------------------------

class TestTmuxCliForwardsRequiresMcp:
    def _run_main(self, tmp_path: Path, *extra_args: str) -> tuple[int, dict]:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        seen: dict = {}

        def fake_dispatch(self_inner, instruction, dispatch_id, **kwargs):
            seen.update(kwargs)
            return InteractiveDispatchResult(success=True, dispatch_id=dispatch_id)

        with patch.object(TmuxInteractiveDispatch, "dispatch", fake_dispatch):
            with patch("tmux_interactive_dispatch._resolve_state_dir", return_value=state_dir):
                rc = main([
                    "--dispatch-id", "cli-mcp-flag",
                    "--instruction", "do it",
                    "--shared-worktree",
                    "--allow-unstaged", "--reason", "ci-test",
                    *extra_args,
                ])
        return rc, seen

    def test_flag_true_reaches_lane(self, tmp_path: Path) -> None:
        rc, seen = self._run_main(tmp_path, "--requires-mcp")
        assert rc == 0
        assert seen["requires_mcp"] is True

    def test_absent_defaults_false(self, tmp_path: Path) -> None:
        rc, seen = self._run_main(tmp_path)
        assert rc == 0
        assert seen["requires_mcp"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
