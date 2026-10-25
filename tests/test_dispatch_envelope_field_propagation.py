"""test_dispatch_envelope_field_propagation.py — OI-982 + OI-985: ExecutionPlan → EnvelopeSpec field propagation.

Verifies that:
1. pr_id flows from DispatchSpec → ExecutionPlan → EnvelopeSpec in both
   run_envelope_plan (provider lane) and run_envelope_headless_plan.
2. Chain-link fields (parent_dispatch, task_class, tier_from, tier_to) flow
   from ExecutionPlan → EnvelopeSpec in run_envelope_headless_plan.

These tests MUST fail on the pre-fix code (origin/main) and go green on the
fix branch. Pure unit assertions — no worker spawn.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import (
    EnvelopeSpec,
    ProviderAdapter,
    _AdapterResult,
    run_envelope_headless_plan,
    run_envelope_plan,
)
from dispatch_internal import issue_permit
from dispatch_plan import (
    ExecutionPlan,
    RuntimeSnapshot,
    compile_plan,
)
from dispatch_spec import (
    DispatchPath,
    DispatchSpec,
    Isolation,
    Provider,
    ValidatedSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_instruction_file(tmp_path: Path) -> Path:
    f = tmp_path / "instruction.md"
    f.write_text("# Test instruction\n", encoding="utf-8")
    return f


def _make_spec_with_pr_id(
    *,
    pr_id: Optional[str],
    provider: Provider = Provider.CODEX,
    target_slot: str = "T1",
    model: str | None = None,
    allow_headless: bool = False,
    tmp_path: Path,
) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-prid-prop",
        staging_id="staging-prid",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot=target_slot,
        gate="codex_gate",
        dispatch_paths=(),
        provider=provider,
        model=model,
        pr_id=pr_id,
        allow_headless=allow_headless,
        headless_reason="test" if allow_headless else None,
    )


def _make_vspec_from_spec(spec: DispatchSpec) -> ValidatedSpec:
    instruction_text = "# Test instruction\n"
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


def _healthy_snapshot(
    *,
    parent_dispatch: Optional[str] = None,
    task_class: Optional[str] = None,
    tier_from: Optional[str] = None,
    tier_to: Optional[str] = None,
) -> RuntimeSnapshot:
    all_slots = ["T0", "T1", "T2", "T3"]
    return RuntimeSnapshot(
        staging_promoted=True,
        target_health={slot: "healthy" for slot in all_slots},
        target_capable={slot: True for slot in all_slots},
        parent_dispatch=parent_dispatch,
        task_class=task_class,
        tier_from=tier_from,
        tier_to=tier_to,
    )


def _fake_adapter_success() -> _AdapterResult:
    return _AdapterResult(returncode=0, completion_text="done", status="success")


# ---------------------------------------------------------------------------
# Test 1 — pr_id propagation: DispatchSpec → ExecutionPlan → EnvelopeSpec
# ---------------------------------------------------------------------------


class TestPrIdPropagation:
    """OI-982: pr_id must flow from DispatchSpec through both envelope entry points."""

    def test_pr_id_in_compiled_plan(self, tmp_path: Path) -> None:
        """compile_plan copies pr_id from DispatchSpec onto ExecutionPlan."""
        spec = _make_spec_with_pr_id(pr_id="123", provider=Provider.CODEX, tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.pr_id == "123", (
            f"Expected plan.pr_id='123', got {plan.pr_id!r}"
        )

    def test_pr_id_from_plan_to_envelope_spec_via_run_envelope_plan(
        self, tmp_path: Path
    ) -> None:
        """run_envelope_plan passes plan.pr_id to EnvelopeSpec (provider lane)."""
        spec = _make_spec_with_pr_id(pr_id="123", provider=Provider.CODEX, tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        captured_specs: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured_specs.append(spec_arg)
            return (None, fake_receipt)

        _fake_consumer_root = tmp_path / "consumer-root"
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(ProviderAdapter, "run", return_value=_fake_adapter_success()), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir
            )

        assert result.status == "success"
        assert captured_specs, "_govern was not called — EnvelopeSpec never constructed"
        assert captured_specs[0].pr_id == "123", (
            f"run_envelope_plan: expected EnvelopeSpec.pr_id='123', "
            f"got {captured_specs[0].pr_id!r}"
        )

    def test_pr_id_from_plan_to_envelope_spec_via_run_envelope_headless_plan(
        self, tmp_path: Path
    ) -> None:
        """run_envelope_headless_plan passes plan.pr_id to EnvelopeSpec (headless lane)."""
        spec = _make_spec_with_pr_id(
            pr_id="123", provider=Provider.CLAUDE, allow_headless=True, tmp_path=tmp_path
        )
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert plan.lane == "claude_headless", f"Expected claude_headless, got {plan.lane}"
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        captured_specs: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured_specs.append(spec_arg)
            return (None, fake_receipt)

        # This test asserts field PROPAGATION (pr_id), not push/PR enforcement —
        # which has its own dedicated coverage in test_pr_enforcement.py and
        # test_lane_matrix_row7_push_pr.py (confirmed green, 27 tests). Stubbing
        # the worktree allocator here (same form as TestPrIdPropagation's provider
        # lane test above) narrows this test's scope to what it actually claims to
        # test. Running the real allocator would drag in real `git worktree add`,
        # `ls-remote`, `git push` and `gh pr create` and rewrite a genuine success
        # to status="failure" on any CI runner where those cannot succeed — the
        # exact CI flake this dispatch fixes.
        _fake_consumer_root = tmp_path / "consumer-root"
        with patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          return_value=_fake_adapter_success()), \
             patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        assert result.status == "success"
        assert captured_specs, "_govern was not called — EnvelopeSpec never constructed"
        assert captured_specs[0].pr_id == "123", (
            f"run_envelope_headless_plan: expected EnvelopeSpec.pr_id='123', "
            f"got {captured_specs[0].pr_id!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — chain-link field propagation through run_envelope_headless_plan
# ---------------------------------------------------------------------------


class TestChainLinkPropagationHeadless:
    """OI-985: chain-link fields must flow to EnvelopeSpec in the headless lane."""

    def test_chainlink_fields_in_headless_envelope_spec(self, tmp_path: Path) -> None:
        """All four chain-link fields reach EnvelopeSpec via run_envelope_headless_plan."""
        spec = _make_spec_with_pr_id(
            pr_id=None, provider=Provider.CLAUDE, allow_headless=True, tmp_path=tmp_path
        )
        vspec = _make_vspec_from_spec(spec)
        snapshot = _healthy_snapshot(
            parent_dispatch="20260801-000000-parent-dispatch",
            task_class="build",
            tier_from="T1",
            tier_to="T2",
        )
        plan = compile_plan(vspec, snapshot)
        assert plan.lane == "claude_headless"
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        captured_specs: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured_specs.append(spec_arg)
            return (None, fake_receipt)

        # Asserts chain-link field PROPAGATION, not push/PR enforcement (covered
        # in test_pr_enforcement.py + test_lane_matrix_row7_push_pr.py, 27 green).
        # Stub the worktree allocator in the same form as the provider-lane test
        # in TestPrIdPropagation so the real `git worktree add`/`git push`/`gh pr
        # create` path cannot rewrite a genuine success to failure on a CI runner
        # that lacks git/gh/network — the source of this dispatch's CI flake.
        _fake_consumer_root = tmp_path / "consumer-root"
        with patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          return_value=_fake_adapter_success()), \
             patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        assert result.status == "success"
        assert captured_specs, "_govern was not called"

        env_spec = captured_specs[0]
        assert env_spec.parent_dispatch == "20260801-000000-parent-dispatch", (
            f"Expected parent_dispatch on EnvelopeSpec, got {env_spec.parent_dispatch!r}"
        )
        assert env_spec.task_class == "build", (
            f"Expected task_class='build', got {env_spec.task_class!r}"
        )
        assert env_spec.tier_from == "T1", (
            f"Expected tier_from='T1', got {env_spec.tier_from!r}"
        )
        assert env_spec.tier_to == "T2", (
            f"Expected tier_to='T2', got {env_spec.tier_to!r}"
        )


# ---------------------------------------------------------------------------
# Test 3 — OI-865: requires_mcp travels on the claude_headless lane
# ---------------------------------------------------------------------------


class TestRequiresMcpPropagation:
    """OI-865: the plan's explicit MCP opt-in must reach EnvelopeSpec and the spawn argv.

    Before this class the claude_headless lane dropped it: EnvelopeSpec had no
    such field and ClaudeSubprocessAdapter.run called spawn_claude without it,
    so a Requires-MCP dispatch was still force-emptied. ``False`` is the value
    every existing spec carries, and its argv must stay unchanged.
    """

    @staticmethod
    def _plan(tmp_path: Path, requires_mcp: bool) -> ExecutionPlan:
        spec = replace(
            _make_spec_with_pr_id(
                pr_id=None, provider=Provider.CLAUDE, allow_headless=True, tmp_path=tmp_path,
            ),
            requires_mcp=requires_mcp,
        )
        return compile_plan(_make_vspec_from_spec(spec), _healthy_snapshot())

    def _captured_spec(self, tmp_path: Path, requires_mcp: bool) -> EnvelopeSpec:
        plan = self._plan(tmp_path, requires_mcp)
        assert plan.lane == "claude_headless", f"Expected claude_headless, got {plan.lane}"
        assert plan.requires_mcp is requires_mcp
        permit = issue_permit(plan)

        state_dir, data_dir = tmp_path / "state", tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        receipt = state_dir / "t0_receipts.ndjson"
        receipt.touch()
        captured: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured.append(spec_arg)
            return (None, receipt)

        with patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          return_value=_fake_adapter_success()), \
             patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=tmp_path / "consumer-root"), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir, role=plan.role,
            )

        assert result.status == "success"
        assert captured, "_govern was not called — EnvelopeSpec never constructed"
        return captured[0]

    @pytest.mark.parametrize("requires_mcp", [True, False])
    def test_plan_requires_mcp_reaches_envelope_spec(
        self, tmp_path: Path, requires_mcp: bool
    ) -> None:
        env_spec = self._captured_spec(tmp_path, requires_mcp)
        assert env_spec.requires_mcp is requires_mcp, (
            "run_envelope_headless_plan dropped plan.requires_mcp on EnvelopeSpec "
            f"(got {getattr(env_spec, 'requires_mcp', '<missing field>')!r})"
        )

    @staticmethod
    def _adapter_spec(tmp_path: Path, requires_mcp: bool) -> EnvelopeSpec:
        extra = {"requires_mcp": True} if requires_mcp else {}
        return EnvelopeSpec(
            dispatch_id="requires-mcp-propagation",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            instruction="do the thing",
            role="backend-developer",
            pr_id=None,
            state_dir=tmp_path / "state",
            data_dir=tmp_path / "data",
            **extra,
        )

    @pytest.mark.parametrize("requires_mcp", [True, False])
    def test_adapter_forwards_requires_mcp_to_spawn_claude(
        self, tmp_path: Path, requires_mcp: bool
    ) -> None:
        from types import SimpleNamespace

        from envelope_adapters_claude import ClaudeSubprocessAdapter

        spawn_result = SimpleNamespace(
            returncode=0, error=None, timed_out=False, stopped_early=False,
            completion_text="done", session_id="sess-1", model=None,
            token_usage={"input_tokens": 1, "output_tokens": 1},
        )
        with patch("provider_spawns.claude_spawn.spawn_claude",
                   return_value=spawn_result) as mock_spawn:
            ClaudeSubprocessAdapter().run(self._adapter_spec(tmp_path, requires_mcp))

        mock_spawn.assert_called_once()
        assert mock_spawn.call_args.kwargs["requires_mcp"] is requires_mcp

    @staticmethod
    def _deliver_argv(requires_mcp: bool) -> list:
        import subprocess
        from unittest.mock import MagicMock

        from subprocess_adapter import SubprocessAdapter

        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = 4321
        proc.poll.return_value = None
        proc.returncode = None
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            result = SubprocessAdapter().deliver(
                "T1", "requires-mcp-argv", instruction="do the thing",
                model="sonnet", role="backend-developer", requires_mcp=requires_mcp,
            )
        assert result.success is True, result.failure_reason
        return mock_popen.call_args[0][0]

    @pytest.mark.parametrize("requires_mcp,force_empty", [(True, False), (False, True)])
    def test_spawn_argv_mcp_posture(self, requires_mcp: bool, force_empty: bool) -> None:
        from worker_permissions import EMPTY_MCP_CONFIG, MCP_NAMESPACE_DENY

        argv = self._deliver_argv(requires_mcp)
        assert ("--mcp-config" in argv) is force_empty
        assert ("--strict-mcp-config" in argv) is force_empty
        if force_empty:
            assert argv[argv.index("--mcp-config") + 1] == EMPTY_MCP_CONFIG
            assert MCP_NAMESPACE_DENY in argv[argv.index("--disallowedTools") + 1].split(",")
        else:
            assert MCP_NAMESPACE_DENY not in argv
