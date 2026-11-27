"""Characterisation tests for golf-schoon R33 (dlv-a1709dc563e0): analyze, compile_plan, validate."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_parameter_tracker import (
    DispatchOutcome, DispatchParameterTracker, DispatchParameters, Insight,
)
from dispatch_plan import ConstraintVerdict, ModelPin, RuntimeSnapshot, compile_plan
from dispatch_spec import (
    DispatchPath, DispatchSpec, Provider, Reject, ValidatedSpec, validate,
)

def _seed(tracker: DispatchParameterTracker, n: int = 24) -> None:
    for i in range(n):
        role = "backend-developer" if i < 13 else ("test-engineer" if i < 21 else "")
        cog = "high" if i % 2 == 0 else ("medium" if i % 4 == 1 else "low")
        tracker.capture_parameters(f"d-{i:03d}", DispatchParameters(
            instruction_char_count=3000 if i % 2 else 1000,
            context_item_count=(3, 0, 1)[i % 3],
            repo_map_symbol_count=5 if i % 4 < 2 else 0,
            role=role, cognition=cog, model="sonnet", terminal="T1", file_count=2,
        ))
        tracker.capture_outcome(f"d-{i:03d}", DispatchOutcome(
            cqs=None if i % 7 == 0 else 50.0 + i, success=i % 5 != 0,
            completion_minutes=10.0 + i, test_count=1, committed=True, lines_changed=i,
        ))


def test_analyze_below_minimum_returns_empty(tmp_path):
    tracker = DispatchParameterTracker(state_dir=tmp_path)
    _seed(tracker, n=19)
    assert tracker.analyze() == []
    assert tracker.analyze(min_experiments=19) != []


def test_analyze_full_insight_list(tmp_path):
    tracker = DispatchParameterTracker(state_dir=tmp_path)
    _seed(tracker)
    assert tracker.analyze() == [
        Insight("context_items", ">= 2 items", "0 items", "success_rate_%", 75.0, 87.5, 8, 8),
        Insight("role", "backend-developer", "test-engineer", "avg_completion_min", 16.0, 26.5, 13, 8),
        Insight("repo_map", "with symbols", "no symbols", "avg_cqs", 60.5, 62.9, 10, 10),
        # instruction_chars and cognition tie on effect size: the stable sort keeps rule order.
        Insight("instruction_chars", "> 2000 chars", "<= 2000 chars", "avg_cqs", 61.6, 61.8, 10, 10),
        Insight("cognition", "high", "medium/low", "avg_cqs", 61.8, 61.6, 10, 10),
    ]


def test_analyze_thin_groups_drop_insights(tmp_path):
    tracker = DispatchParameterTracker(state_dir=tmp_path)
    for i in range(20):
        tracker.capture_parameters(f"t-{i}", DispatchParameters(500, 0, 0, "only-role", "high", "m", "T1", 0))
        tracker.capture_outcome(f"t-{i}", DispatchOutcome(70.0, True, 5.0, 0, False, 0))
    assert tracker.analyze() == []


def _spec(tmp_path: Path, **kw) -> DispatchSpec:
    f = tmp_path / "instruction.md"
    f.write_text("# Test instruction\n", encoding="utf-8")
    base = dict(
        schema_version=1, project_id="vnx-dev", dispatch_id="test-dispatch-001",
        staging_id="staging-001", instruction_file=f, role="backend-developer",
        target_slot="T1", gate="codex_gate", dispatch_paths=(), provider=Provider.CLAUDE,
    )
    base.update(kw)
    return DispatchSpec(**base)


def _vspec(spec: DispatchSpec) -> ValidatedSpec:
    return ValidatedSpec(spec=spec, instruction_text="x", normalized_paths=spec.dispatch_paths,
                         instruction_sha256=hashlib.sha256(b"x").hexdigest())


def _snap(**kw) -> RuntimeSnapshot:
    slots = ("T0", "T1", "T2", "T3", "custom")
    base = dict(staging_promoted=True, target_health={s: "healthy" for s in slots},
                target_capable={s: True for s in slots})
    base.update(kw)
    return RuntimeSnapshot(**base)


ALL_RULES = "D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12"


@pytest.mark.parametrize("spec_kw,snap_kw,expected", [
    ({}, {}, ("claude_headless", "claude_subprocess", "subscription", "sonnet", "ephemeral", "claude-tmux", ())),
    ({}, {"claude_api_metered": True, "claude_serial_enabled": False},
     ("claude_headless", "claude_subprocess", "api_metered", "sonnet", "ephemeral", None, ())),
    ({"model": "opus"}, {"model_pins": {"T1": ModelPin("sonnet", "floor")}},
     ("claude_headless", "claude_subprocess", "subscription", "opus", "ephemeral", "claude-tmux",
      ("model-tier: requested opus over pinned sonnet for T1 (default semantics — request honoured)",))),
    ({"model": "opus"}, {"model_pins": {"T1": ModelPin("sonnet", "default")}},
     ("claude_headless", "claude_subprocess", "subscription", "opus", "ephemeral", "claude-tmux",
      ("model-tier: requested opus over pinned sonnet for T1 (default semantics — request honoured)",))),
    ({}, {"model_pins": {"T1": ModelPin("opus", "default")}},
     ("claude_headless", "claude_subprocess", "subscription", "sonnet", "ephemeral", "claude-tmux",
      ("model-tier: requested sonnet over pinned opus for T1 (default semantics — request honoured)",))),
    ({"model": "opus"}, {"model_pins": {"T1": ModelPin("opus", "floor")}},
     ("claude_headless", "claude_subprocess", "subscription", "opus", "ephemeral", "claude-tmux", ())),
    ({"allow_headless": True, "headless_reason": "why"},
     {"constraint_verdicts": (ConstraintVerdict("c1", "warn", "careful"),)},
     ("claude_headless", "claude_subprocess", "subscription", "sonnet", "ephemeral", "claude-tmux",
      ("constraint-warn: c1: careful", "HEADLESS lane opted-in: why"))),
    ({"provider": Provider.KIMI}, {}, ("provider", "provider", "subscription", "kimi-k3", "T1", None, ())),
    ({"provider": Provider.LOCAL_GEMMA, "model": "g"}, {},
     ("provider", "provider", "local", "g", "T1", None, ())),
    ({"provider": Provider.CODEX, "target_id_override": "custom"}, {},
     ("provider", "provider", "provider_metered", "gpt-6-astra", "custom", None, ())),
])
def test_compile_plan_fields(tmp_path, spec_kw, snap_kw, expected):
    plan = compile_plan(_vspec(_spec(tmp_path, **spec_kw)), _snap(**snap_kw))
    got = (plan.lane, plan.adapter, plan.billing, plan.model, plan.target_id,
           plan.serialization_class, plan.warnings)
    assert got == expected
    assert plan.route_reason == ALL_RULES
    assert plan.seed_materialize is False and plan.require_worktree is True


@pytest.mark.parametrize("spec_kw,snap_kw,code,reason", [
    ({}, {"staging_promoted": False}, "ADR-006", "dispatch rejected: staging not promoted (ADR-006 gate)"),
    ({"role": "ghost"}, {"valid_roles": frozenset({"b", "a"})}, "unknown-role",
     "role 'ghost' is not a known agent role; valid roles: a, b"),
    ({}, {"valid_roles": frozenset()}, "unknown-role",
     "role 'backend-developer' is not a known agent role; valid roles: "
     "(none discovered — agents/ registry unavailable)"),
    ({"task_class": "zz"}, {"valid_task_classes": frozenset()}, "unknown-task-class",
     "task_class 'zz' is not a known routing_by_task class; valid task classes: "
     "(none discovered — routing_recommendations.yaml unavailable)"),
    ({"role": "ghost", "task_class": "zz"},
     {"valid_roles": frozenset(), "valid_task_classes": frozenset()}, "unknown-role", None),
    ({}, {"constraint_verdicts": (ConstraintVerdict("w", "warn", "m"),
                                  ConstraintVerdict("blk", "blocking", "stop"))}, "blk", "stop"),
    ({"provider": Provider.AUTO}, {}, "unresolved-provider",
     "AUTO must be resolved by the capability seam before compile_plan"),
    ({"provider": Provider.CODEX}, {"target_health": {"T1": "offline"}}, "R-6",
     "target 'T1' is not healthy (status='offline')"),
    ({"provider": Provider.CODEX}, {"target_capable": {"T1": False}, "target_health": {"T1": "healthy"}},
     "R-5", "target 'T1' is not capable for this dispatch"),
    ({"provider": Provider.CODEX, "target_id_override": "nope"}, {}, "R-6",
     "target 'nope' is not healthy (status=None)"),
])
def test_compile_plan_rejects(tmp_path, spec_kw, snap_kw, code, reason):
    result = compile_plan(_vspec(_spec(tmp_path, **spec_kw)), _snap(**snap_kw))
    assert isinstance(result, Reject) and result.code == code
    if reason is not None:
        assert result.reason == reason


def test_compile_plan_unknown_skill_before_constraints(tmp_path):
    snap = _snap(repo_root=tmp_path, constraint_verdicts=(ConstraintVerdict("blk", "blocking", "s"),))
    result = compile_plan(_vspec(_spec(tmp_path, skill="no-such-skill")), snap)
    assert result.code == "unknown-skill"
    assert result.reason.startswith("skill 'no-such-skill' not found; looked for ")


def _validate(tmp_path, **kw):
    return validate(_spec(tmp_path, **kw), project_id="vnx-dev", repo_root=tmp_path)


def test_validate_success_shape(tmp_path):
    dp = (DispatchPath(PurePosixPath("scripts/./x.py")),)
    out = _validate(tmp_path, dispatch_paths=dp, work_ref="origin/feat/x", tier_from="tier-low")
    assert isinstance(out, ValidatedSpec)
    assert out.instruction_text == "# Test instruction\n"
    assert out.instruction_sha256 == hashlib.sha256(b"# Test instruction\n").hexdigest()
    assert out.normalized_paths == (DispatchPath(PurePosixPath("scripts/x.py")),)


@pytest.mark.parametrize("kw,code,reason", [
    ({"project_id": "other"}, "project-mismatch",
     "spec.project_id='other' != authoritative project_id='vnx-dev'; caller cannot redirect state to another project"),
    ({"dispatch_id": "bad id"}, "bad-dispatch-id", "dispatch_id 'bad id' does not match id regex"),
    ({"instruction_file": Path("rel.md")}, "instruction-unreadable", "instruction_file must be absolute, got rel.md"),
    ({"role": "  "}, "bad-role", "role must be a non-empty string"),
    ({"target_slot": "T9"}, "bad-target-slot", "target_slot must be one of ['T0', 'T1', 'T2', 'T3'], got 'T9'"),
    ({"model": " "}, "bad-model", "model must be a non-empty string when set"),
    ({"dispatch_paths": (DispatchPath(PurePosixPath("/abs")),)}, "bad-path", None),
    ({"deadline_seconds": 10}, "bad-deadline", "deadline_seconds must be in [300, 14400], got 10"),
    ({"allow_headless": True, "headless_reason": " "}, "headless-reason-required", None),
    ({"allow_headless": True, "headless_reason": "r", "provider": Provider.KIMI}, "headless-claude-only",
     "allow_headless is only valid for provider=claude, got provider='kimi'; headless is a claude-only lane"),
    ({"work_ref": "origin/bad..ref"}, "bad-work-ref", None),
    # First failing rule wins: the earlier rule's code is returned.
    ({"deadline_seconds": 10, "work_ref": "bad..ref"}, "bad-deadline", None),
    ({"allow_headless": True, "track_id": " "}, "headless-reason-required", None),
])
def test_validate_rejects(tmp_path, kw, code, reason):
    out = _validate(tmp_path, **kw)
    assert isinstance(out, Reject) and out.code == code
    if reason is not None:
        assert out.reason == reason


def test_validate_instruction_file_states(tmp_path):
    missing = tmp_path / "missing.md"
    assert _validate(tmp_path, instruction_file=missing).reason.startswith("instruction_file not accessible: ")
    assert _validate(tmp_path, instruction_file=tmp_path).reason == f"instruction_file is not a regular file: {tmp_path}"
    real = tmp_path / "real.md"
    real.write_text("hi", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    assert _validate(tmp_path, instruction_file=link).reason == f"instruction_file must not be a symlink: {link}"
    raw = tmp_path / "raw.md"
    raw.write_bytes(b"\xff\xfe\xfa")
    result = _validate(tmp_path, instruction_file=raw)
    assert result.code == "instruction-unreadable"
    assert result.reason.startswith("instruction_file is not valid UTF-8: ")
    mismatch = _validate(tmp_path, instruction_sha256="0" * 64).reason
    assert mismatch.startswith("instruction_file sha256 mismatch: spec declared 000000000000…, computed ")
