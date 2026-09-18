"""test_requires_mcp_propagation.py — OI-865: requires_mcp must survive the plan boundary.

The field exists in DispatchSpec (dispatch_spec.py:100) and is read by the door
(dispatch_cli.load_spec), but was dropped at compile_plan: ExecutionPlan had no
field, so a dispatch staged with requires_mcp:true lost its ambient MCP under
scoped mode (blocks #1252's default worker-capability scoping flip).

The tests here pin the plan boundary:
  spec.requires_mcp -> compile_plan -> ExecutionPlan.requires_mcp

The tmux lane's half of the chain (``_execute_claude`` forwarding the plan field to
``lane.dispatch(requires_mcp=...)`` and the lane CLI's ``--requires-mcp``) went with
that lane on 2026-09-18. Nothing in ``dispatch_envelope``'s headless path reads
``plan.requires_mcp``, so that half has no counterpart on the surviving lane.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_plan import ExecutionPlan, RuntimeSnapshot, compile_plan
from dispatch_spec import DispatchSpec, Provider, ValidatedSpec


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
        gate="codex_gate",
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

    def test_requires_mcp_changes_digest(self, tmp_path: Path) -> None:
        """MCP access alters worker behavior, so it must perturb the permit fingerprint."""
        plan_true = compile_plan(_make_vspec(requires_mcp=True, tmp_path=tmp_path), _healthy_snapshot())
        plan_false = compile_plan(_make_vspec(requires_mcp=False, tmp_path=tmp_path), _healthy_snapshot())
        assert isinstance(plan_true, ExecutionPlan)
        assert isinstance(plan_false, ExecutionPlan)
        assert plan_true.digest() != plan_false.digest(), (
            "digest() must distinguish requires_mcp: true from false"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
