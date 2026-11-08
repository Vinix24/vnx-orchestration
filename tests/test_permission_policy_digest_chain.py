"""Permissions P4: the permission policy digest is part of the spec and of the plan digest.

Chain under test: permission_policy.policy_digest -> DispatchSpec.permission_policy_digest
-> ExecutionPlan.canonical_dict() -> ExecutionPlan.digest() -> ExecutionPermit.

Invariants pinned here:
- two specs that differ only in the write scope of their role (so only in policy digest)
  give different plan digests;
- a spec without the field keeps the exact plan digest it had before P4 (staged permits stay valid);
- role stays out of canonical_dict(); the policy digest goes in;
- a present-but-malformed digest is refused at validate() with a named reason.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_internal import issue_permit, require_permit
from dispatch_plan import ExecutionPlan, RuntimeSnapshot, compile_plan
from dispatch_spec import DispatchSpec, Provider, Reject, ValidatedSpec, validate
from permission_policy import compose_policy

_INSTRUCTION = "# Test instruction\n"
_SHA = hashlib.sha256(_INSTRUCTION.encode("utf-8")).hexdigest()


def _snapshot() -> RuntimeSnapshot:
    slots = ["T0", "T1", "T2", "T3"]
    return RuntimeSnapshot(
        staging_promoted=True,
        target_health={s: "healthy" for s in slots},
        target_capable={s: True for s in slots},
        model_pins={},
        claude_serial_enabled=True,
        constraint_verdicts=(),
        claude_api_metered=False,
    )


def _spec(tmp_path: Path, **overrides) -> DispatchSpec:
    ifile = tmp_path / "instruction.md"
    ifile.write_text(_INSTRUCTION, encoding="utf-8")
    base = dict(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="digest-chain-001",
        staging_id="staging-001",
        instruction_file=ifile,
        role="backend-developer",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
    )
    base.update(overrides)
    return DispatchSpec(**base)


def _plan(spec: DispatchSpec) -> ExecutionPlan:
    plan = compile_plan(
        ValidatedSpec(spec=spec, instruction_text=_INSTRUCTION, normalized_paths=(),
                      instruction_sha256=_SHA),
        _snapshot(),
    )
    assert isinstance(plan, ExecutionPlan), plan
    return plan


def _policy_digest(write_scope: list[str]) -> str:
    return compose_policy(
        {"allowed_tools": ["Read"], "file_write_scope": write_scope},
        None,
        is_fallback=False,
    ).policy_digest


def test_two_write_scopes_have_different_plan_digest(tmp_path: Path) -> None:
    narrow = _spec(tmp_path, permission_policy_digest=_policy_digest(["scripts/lib/"]))
    wide = _spec(tmp_path, permission_policy_digest=_policy_digest(["scripts/", "tests/"]))
    assert narrow.permission_policy_digest != wide.permission_policy_digest
    assert _plan(narrow).digest() != _plan(wide).digest()


def test_same_policy_digest_gives_same_plan_digest(tmp_path: Path) -> None:
    digest = _policy_digest(["scripts/lib/"])
    a = _plan(_spec(tmp_path, permission_policy_digest=digest))
    b = _plan(_spec(tmp_path, permission_policy_digest=digest))
    assert a.digest() == b.digest()


def test_absent_field_keeps_the_digest_it_had_before_p4(tmp_path: Path) -> None:
    plan = _plan(_spec(tmp_path))
    canonical = plan.canonical_dict()
    assert "permission_policy_digest" not in canonical
    # The pre-P4 digest is sha256 over exactly this dict: nothing was added for an absent field.
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    assert plan.digest() == hashlib.sha256(blob.encode()).hexdigest()
    assert plan.permission_policy_digest is None


def test_policy_digest_changes_the_digest_of_an_otherwise_equal_spec(tmp_path: Path) -> None:
    without = _plan(_spec(tmp_path))
    with_policy = _plan(_spec(tmp_path, permission_policy_digest=_policy_digest(["scripts/"])))
    assert without.digest() != with_policy.digest()


def test_canonical_dict_includes_policy_digest_but_not_role(tmp_path: Path) -> None:
    digest = _policy_digest(["scripts/lib/"])
    plan = _plan(_spec(tmp_path, permission_policy_digest=digest))
    canonical = plan.canonical_dict()
    assert canonical["permission_policy_digest"] == digest
    assert "role" not in canonical


def test_role_alone_does_not_change_the_plan_digest(tmp_path: Path) -> None:
    digest = _policy_digest(["scripts/lib/"])
    a = _plan(_spec(tmp_path, role="backend-developer", permission_policy_digest=digest))
    b = _plan(_spec(tmp_path, role="test-engineer", permission_policy_digest=digest))
    assert a.digest() == b.digest()


def test_permit_binds_the_policy_digest(tmp_path: Path) -> None:
    narrow = _plan(_spec(tmp_path, permission_policy_digest=_policy_digest(["scripts/lib/"])))
    wide = _plan(_spec(tmp_path, permission_policy_digest=_policy_digest(["scripts/"])))
    permit = issue_permit(narrow)
    require_permit(narrow, permit)
    with pytest.raises(PermissionError):
        require_permit(wide, permit)


def test_old_spec_without_the_field_still_validates_and_compiles(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert spec.permission_policy_digest is None
    vspec = validate(spec, project_id="vnx-dev", repo_root=tmp_path)
    assert isinstance(vspec, ValidatedSpec), vspec
    assert isinstance(compile_plan(vspec, _snapshot()), ExecutionPlan)


def test_valid_policy_digest_validates(tmp_path: Path) -> None:
    spec = _spec(tmp_path, permission_policy_digest=_policy_digest(["scripts/lib/"]))
    assert isinstance(validate(spec, project_id="vnx-dev", repo_root=tmp_path), ValidatedSpec)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "abc123",
        "0" * 63,
        "0" * 65,
        "G" * 64,
        ("a" * 64).upper(),
        "sha256:" + "a" * 64,
        ("a" * 64) + "\n",
        123,
        ["a" * 64],
    ],
)
def test_invalid_policy_digest_is_refused_with_a_named_reason(tmp_path: Path, bad: object) -> None:
    spec = replace(_spec(tmp_path), permission_policy_digest=bad)
    result = validate(spec, project_id="vnx-dev", repo_root=tmp_path)
    assert isinstance(result, Reject)
    assert result.code == "bad-permission-policy-digest"
    assert "permission_policy_digest" in result.reason


def test_bad_gate_is_still_refused_after_the_rule_extraction(tmp_path: Path) -> None:
    result = validate(_spec(tmp_path, gate="codex_gat"), project_id="vnx-dev", repo_root=tmp_path)
    assert isinstance(result, Reject)
    assert result.code == "bad-gate"
