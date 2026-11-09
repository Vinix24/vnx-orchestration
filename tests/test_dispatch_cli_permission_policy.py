"""Permissions P5: the door fills the policy digest at stage time and refuses on missing evidence.

Pinned here:
- a staged spec carries the digest of the policy composed for its role (one composition,
  ``permission_policy.compose_policy`` through ``resolve_worker_profile``);
- unreadable evidence (broken YAML, unknown source key, a role in no register) refuses the stage
  with a named reason and writes no spec; a fallback role and ``isolation: {}`` stage;
- enforcement stays OFF: with the flag unset the worker scope args are byte-identical;
- at fire time a changed digest is a plan warning under OFF and a refusal under ON, and a spec
  staged before P5 (digest ``None``) fires with one plan line.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_bridge
import dispatch_cli
from dispatch_plan import ExecutionPlan, RuntimeSnapshot, compile_plan
from dispatch_spec import DispatchSpec, Provider, Reject, ValidatedSpec
from permission_policy import PermissionPolicyRefused
from worker_permissions import build_claude_scope_args, resolve_worker_profile

_ROLE = "backend-developer"
_PROFILES = """\
version: 1
profiles:
  backend-developer:
    allowed_tools: [Read, Write, Edit, Bash]
    denied_tools: [WebFetch]
    file_write_scope: ["scripts/**", "tests/**"]
  empty-profile-role: {}
"""
_GOLDEN_SCOPE_ARGS = [
    "--permission-mode", "acceptEdits",
    "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
    "--allowedTools", "Read,Write,Edit,Bash",
    "--disallowedTools", "WebFetch,Bash(git stash:*),mcp__*",
]
_INSTRUCTION = "# instruction\n"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / ".vnx").mkdir(parents=True)
    (root / ".vnx" / "worker_permissions.yaml").write_text(_PROFILES, encoding="utf-8")
    for role in (_ROLE, "empty-profile-role"):
        (root / "agents" / role).mkdir(parents=True)
        (root / "agents" / role / "CLAUDE.md").write_text("# role\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(root))
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
    monkeypatch.delenv("VNX_WORKER_ENFORCEMENT_SKIP", raising=False)
    return root


def _stage(tmp_path: Path, role: str = _ROLE, dispatch_id: str = "p5-stage-001") -> Path:
    return dispatch_bridge.stage_spec_bundle(
        instruction_text=_INSTRUCTION,
        dispatch_id=dispatch_id,
        role=role,
        target_slot="T1",
        project_id="vnx-dev",
        data_dir=tmp_path / "data",
    )


def _pending(tmp_path: Path) -> Path:
    return tmp_path / "data" / "dispatches" / "pending"


def _spec_and_plan(tmp_path: Path, digest: "str | None") -> "tuple[DispatchSpec, ExecutionPlan]":
    ifile = tmp_path / "instruction.md"
    ifile.write_text(_INSTRUCTION, encoding="utf-8")
    spec = DispatchSpec(
        schema_version=1, project_id="vnx-dev", dispatch_id="p5-fire-001",
        staging_id="staging-001", instruction_file=ifile, role=_ROLE, target_slot="T1",
        gate="codex_gate", dispatch_paths=(), provider=Provider.CLAUDE,
        permission_policy_digest=digest,
    )
    slots = ["T0", "T1", "T2", "T3"]
    snapshot = RuntimeSnapshot(
        staging_promoted=True, target_health={s: "healthy" for s in slots},
        target_capable={s: True for s in slots}, model_pins={},
        claude_serial_enabled=True, constraint_verdicts=(), claude_api_metered=False,
    )
    plan = compile_plan(
        ValidatedSpec(spec=spec, instruction_text=_INSTRUCTION, normalized_paths=(),
                      instruction_sha256=hashlib.sha256(_INSTRUCTION.encode()).hexdigest()),
        snapshot,
    )
    assert isinstance(plan, ExecutionPlan), plan
    return spec, plan


def test_staged_spec_carries_policy_digest(project: Path, tmp_path: Path) -> None:
    spec = json.loads(_stage(tmp_path).read_text(encoding="utf-8"))
    expected = resolve_worker_profile(_ROLE).policy.policy_digest
    assert spec["permission_policy_digest"] == expected
    assert len(expected) == 64


def test_staged_spec_loads_with_the_digest(project: Path, tmp_path: Path) -> None:
    loaded = dispatch_cli.load_spec(_stage(tmp_path))
    assert loaded.permission_policy_digest == resolve_worker_profile(_ROLE).policy_digest


def test_unreadable_profile_refuses_stage(project: Path, tmp_path: Path) -> None:
    (project / ".vnx" / "worker_permissions.yaml").write_text("profiles: [unclosed\n", encoding="utf-8")
    with pytest.raises(PermissionPolicyRefused) as info:
        _stage(tmp_path)
    assert info.value.reason == "source-broken-yaml"
    assert "source-broken-yaml" in str(info.value)
    assert not _pending(tmp_path).exists() or not list(_pending(tmp_path).iterdir())


def test_stage_cli_exits_non_zero_with_named_reason_and_no_spec(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (project / ".vnx" / "worker_permissions.yaml").write_text("profiles: [unclosed\n", encoding="utf-8")
    instruction = tmp_path / "instruction.md"
    instruction.write_text(_INSTRUCTION, encoding="utf-8")
    rc = dispatch_bridge._main_stage([
        "--instruction", str(instruction), "--dispatch-id", "p5-cli-001", "--role", _ROLE,
        "--slot", "T1", "--data-dir", str(tmp_path / "data"),
    ])
    assert rc != 0
    assert "source-broken-yaml" in capsys.readouterr().err
    assert not list(tmp_path.glob("data/dispatches/pending/*/dispatch-spec.json"))


@pytest.mark.parametrize(
    ("profiles", "reason"),
    [
        ("version: 1\nprofiles:\n  backend-developer:\n    surprise_key: [x]\n", "unknown-source-key"),
        ("", "source-empty"),
        ("version: 1\nprofiles: nope\n", "source-malformed"),
    ],
)
def test_other_unprovable_sources_refuse_stage(
    project: Path, tmp_path: Path, profiles: str, reason: str
) -> None:
    (project / ".vnx" / "worker_permissions.yaml").write_text(profiles, encoding="utf-8")
    with pytest.raises(PermissionPolicyRefused) as info:
        _stage(tmp_path)
    assert info.value.reason == reason
    assert not list(tmp_path.glob("data/dispatches/pending/*/*"))


def test_unreadable_profile_path_refuses_stage(project: Path, tmp_path: Path) -> None:
    (project / ".vnx" / "worker_permissions.yaml").unlink()
    (project / ".vnx" / "worker_permissions.yaml").mkdir()
    with pytest.raises(PermissionPolicyRefused):
        _stage(tmp_path)
    assert not list(tmp_path.glob("data/dispatches/pending/*/*"))


def test_role_in_no_register_refuses_stage(project: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionPolicyRefused):
        _stage(tmp_path, role="no-such-role")
    assert not list(tmp_path.glob("data/dispatches/pending/*/*"))


def test_broken_isolation_config_refuses_stage(project: Path, tmp_path: Path) -> None:
    (project / "agents" / _ROLE / "config.yaml").write_text("isolation: [unclosed\n", encoding="utf-8")
    with pytest.raises(PermissionPolicyRefused):
        _stage(tmp_path)
    assert not list(tmp_path.glob("data/dispatches/pending/*/*"))


def test_fallback_role_stages_with_a_digest(project: Path, tmp_path: Path) -> None:
    profile = resolve_worker_profile("empty-profile-role")
    assert profile.is_fallback is True
    spec = json.loads(_stage(tmp_path, role="empty-profile-role").read_text(encoding="utf-8"))
    assert spec["permission_policy_digest"] == profile.policy_digest


def test_empty_isolation_stages(project: Path, tmp_path: Path) -> None:
    (project / "agents" / _ROLE / "config.yaml").write_text("isolation: {}\n", encoding="utf-8")
    spec = json.loads(_stage(tmp_path).read_text(encoding="utf-8"))
    assert spec["permission_policy_digest"] == resolve_worker_profile(_ROLE).policy_digest


def test_empty_role_is_left_to_the_door(project: Path, tmp_path: Path) -> None:
    spec = json.loads(_stage(tmp_path, role="").read_text(encoding="utf-8"))
    assert spec["role"] == ""
    assert spec["permission_policy_digest"] is None


def test_escalation_stage_carries_the_digest(project: Path, tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    import providers.smart_router.tier_routing as tier_routing

    monkeypatch.setattr(tier_routing, "escalate_tier", lambda *a, **k: SimpleNamespace(
        action="climb", unknown_class=False, tier_to="mid", tier_from="low",
        parent_dispatch="p5-parent-001", notify_operator=False))
    monkeypatch.setattr(tier_routing, "resolve_tier_route",
                        lambda *a, **k: SimpleNamespace(provider="claude", model="sonnet"))
    spec_file = dispatch_bridge.stage_escalation_bundle(
        rejected_dispatch_id="p5-parent-001", tier_from="low", failure_class="x",
        instruction_text=_INSTRUCTION, dispatch_id="p5-esc-001", role=_ROLE, target_slot="T1",
        project_id="vnx-dev", data_dir=tmp_path / "data",
    )
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    assert spec["permission_policy_digest"] == resolve_worker_profile(_ROLE).policy_digest


def test_scope_args_reversible_with_flag_unset(project: Path, tmp_path: Path) -> None:
    """Enforcement OFF: staging and the fire check leave the worker scope args byte-identical."""
    before = build_claude_scope_args(resolve_worker_profile(_ROLE))
    assert before == _GOLDEN_SCOPE_ARGS
    spec_file = _stage(tmp_path)
    spec, plan = _spec_and_plan(tmp_path, dispatch_cli.load_spec(spec_file).permission_policy_digest)
    assert isinstance(dispatch_cli._policy_fire_check(plan, spec), ExecutionPlan)
    assert build_claude_scope_args(resolve_worker_profile(_ROLE)) == before


def test_fire_matching_digest_adds_no_line(project: Path, tmp_path: Path) -> None:
    spec, plan = _spec_and_plan(tmp_path, resolve_worker_profile(_ROLE).policy_digest)
    assert dispatch_cli._policy_fire_check(plan, spec).warnings == plan.warnings


def test_fire_mismatch_warns_under_off(project: Path, tmp_path: Path) -> None:
    stale = "a" * 64
    spec, plan = _spec_and_plan(tmp_path, stale)
    checked = dispatch_cli._policy_fire_check(plan, spec)
    assert isinstance(checked, ExecutionPlan)
    line = checked.warnings[-1]
    assert stale in line and resolve_worker_profile(_ROLE).policy_digest in line


def test_fire_mismatch_refuses_under_on(project: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    spec, plan = _spec_and_plan(tmp_path, "a" * 64)
    checked = dispatch_cli._policy_fire_check(plan, spec)
    assert isinstance(checked, Reject)
    assert checked.code == "permission-policy-mismatch"


def test_fire_unprovable_policy_warns_under_off_and_refuses_under_on(
    project: Path, tmp_path: Path, monkeypatch
) -> None:
    digest = resolve_worker_profile(_ROLE).policy_digest
    (project / ".vnx" / "worker_permissions.yaml").write_text("profiles: [unclosed\n", encoding="utf-8")
    spec, plan = _spec_and_plan(tmp_path, digest)
    off = dispatch_cli._policy_fire_check(plan, spec)
    assert isinstance(off, ExecutionPlan) and "source-broken-yaml" in off.warnings[-1]
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    assert isinstance(dispatch_cli._policy_fire_check(plan, spec), Reject)


def test_none_spec_fires_with_pre_policy_line(project: Path, tmp_path: Path, monkeypatch) -> None:
    spec, plan = _spec_and_plan(tmp_path, None)
    checked = dispatch_cli._policy_fire_check(plan, spec)
    assert isinstance(checked, ExecutionPlan)
    assert checked.warnings == plan.warnings + ("permission policy: pre-policy spec",)
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    assert isinstance(dispatch_cli._policy_fire_check(plan, spec), ExecutionPlan)


def test_compile_plan_checked_passes_a_reject_through(project: Path, tmp_path: Path, monkeypatch) -> None:
    reject = Reject("some-code", "because")
    monkeypatch.setattr(dispatch_cli, "compile_plan", lambda *a, **k: reject)
    assert dispatch_cli._compile_plan_checked(None, None) is reject
