"""F2 part 2: the door and provider_dispatch refuse client and personal content on external lanes.

Every root below is a tmp directory written into a tmp boundary file: no operator path appears
here. The lane functions of provider_dispatch are mocked, so no provider and no claude process
starts.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import constraint_enforcer as legacy_enforcer
import content_class
import provider_dispatch as pd
from dispatch_cli import build_runtime_snapshot, load_spec
from dispatch_spec import Reject, validate
from providers import constraint_enforcer as live_enforcer

SHIPPED_YAML = _LIB / "providers" / "provider_constraints.yaml"

EXTERNAL_LANES = [
    ("deepseek-harness", "_dispatch_deepseek_harness"),
    ("glm-harness", "_dispatch_glm_harness"),
    ("kimi", "_dispatch_kimi"),
    ("litellm:openrouter", "_dispatch_litellm"),
]
ALLOWED_LANES = [
    ("codex", "_dispatch_codex"),
    ("local-gemma", "_dispatch_local_gemma"),
]


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.client_root = tmp / "client-root"
        self.personal_root = tmp / "personal-root"
        self.client = self.client_root / "group" / "acme"
        self.personal = self.personal_root / "notes"
        self.fabric = tmp / "fabric-checkout"
        self.other = tmp / "elsewhere"
        for directory in (self.client, self.personal, self.fabric, self.other):
            directory.mkdir(parents=True)
        (self.fabric / ".vnx-project-id").write_text("vnx-dev\n", encoding="utf-8")
        self.boundary_file = tmp / "content_boundary.json"
        self.write_boundary()

    def write_boundary(self, **overrides: object) -> None:
        data = {
            "version": 1,
            "client_roots": [str(self.client_root)],
            "personal_roots": [str(self.personal_root)],
            "client_project_ids": ["pacompany-engine"],
            "provider_exceptions": {"pacompany-engine": ["deepseek"]},
        }
        data.update(overrides)
        self.boundary_file.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(w.boundary_file))
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(w.fabric))
    for key in ("VNX_OVERRIDE_CONTENT_BOUNDARY", "VNX_OVERRIDE_CONTENT_BOUNDARY_REASON",
                "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(w.fabric)
    return w


# ---------------------------------------------------------------------------
# provider_dispatch (the side door)
# ---------------------------------------------------------------------------

def _argv(provider: str, *, instruction: str = "review the notes", dispatch_id: str = "d-content-1"):
    argv = [
        "--provider", provider, "--terminal-id", "T1", "--dispatch-id", dispatch_id,
        "--instruction", instruction, "--role", "backend-developer",
    ]
    if provider == "local-gemma":
        argv += ["--model", "gemma-4b-local"]
    return argv


def _run_main(provider: str, lane_name: str, **kwargs):
    with patch.object(pd, lane_name, return_value=0) as lane:
        rc = pd.main(_argv(provider, **kwargs))
    return rc, lane


def _refusals(dispatch_id: str) -> list[dict]:
    receipts = pd._resolve_state_dir() / "t0_receipts.ndjson"
    if not receipts.exists():
        return []
    rows = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("dispatch_id") == dispatch_id]


@pytest.mark.parametrize("provider,lane_name", EXTERNAL_LANES)
def test_b1_client_cwd_refuses_external_lane(world, monkeypatch, provider, lane_name):
    monkeypatch.chdir(world.client)
    rc, lane = _run_main(provider, lane_name)
    assert rc == 1
    lane.assert_not_called()
    receipts = _refusals("d-content-1")
    assert receipts and "content-boundary" in receipts[-1]["failure_reason"]


@pytest.mark.parametrize("provider,lane_name", ALLOWED_LANES)
def test_b2_client_cwd_allows_claude_codex_and_local_lanes(world, monkeypatch, provider, lane_name):
    monkeypatch.chdir(world.client)
    rc, lane = _run_main(provider, lane_name)
    assert rc == 0
    lane.assert_called_once()
    assert not any("content-boundary" in (r.get("failure_reason") or "") for r in _refusals("d-content-1"))


def test_b3_business_panel_env_in_client_cwd_is_still_refused(world, monkeypatch):
    store = world.tmp / "vnx-dev"
    store.mkdir()
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.setenv("VNX_DATA_DIR", str(store))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.chdir(world.client)
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    lane.assert_not_called()


def test_personal_cwd_refuses_external_lane(world, monkeypatch):
    monkeypatch.chdir(world.personal)
    rc, lane = _run_main("kimi", "_dispatch_kimi")
    assert rc == 1
    lane.assert_not_called()


def test_b9_fabric_cwd_still_allows_external_lanes(world):
    for provider, lane_name in EXTERNAL_LANES:
        rc, lane = _run_main(provider, lane_name, dispatch_id=f"d-fabric-{provider.replace(':', '-')}")
        assert rc == 0, provider
        lane.assert_called_once()


def test_client_path_in_instruction_refuses_from_fabric_cwd(world):
    text = f"read {world.client}/proposal.md and summarise it"
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness", instruction=text)
    assert rc == 1
    lane.assert_not_called()


def test_glob_only_client_path_in_instruction_is_not_a_signal(world):
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness",
                         instruction=f"never read {world.client_root}/*")
    assert rc == 0
    lane.assert_called_once()


def test_b6_override_without_reason_stays_blocking(world, monkeypatch):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    lane.assert_not_called()


def test_b6_override_with_reason_dispatches_and_logs_the_reason(world, monkeypatch, caplog):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "operator wants the full panel")
    with caplog.at_level(logging.WARNING):
        rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness", dispatch_id="d-override-1")
    assert rc == 0
    lane.assert_called_once()
    logged = [r.getMessage() for r in caplog.records if "content boundary override" in r.getMessage()]
    assert logged and "d-override-1" in logged[0] and "operator wants the full panel" in logged[0]


def test_b6_blank_reason_does_not_count(world, monkeypatch):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "   ")
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    lane.assert_not_called()


# B10 ------------------------------------------------------------------------

def test_b10_project_id_only_signal_allows_the_excepted_provider(world, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "pacompany-engine")
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 0
    lane.assert_called_once()


@pytest.mark.parametrize("provider,lane_name", [
    ("glm-harness", "_dispatch_glm_harness"),
    ("kimi", "_dispatch_kimi"),
])
def test_b10_project_id_only_signal_refuses_other_external_lanes(world, monkeypatch, provider, lane_name):
    monkeypatch.setenv("VNX_PROJECT_ID", "pacompany-engine")
    rc, lane = _run_main(provider, lane_name)
    assert rc == 1
    lane.assert_not_called()


def test_b10_client_cwd_beats_the_project_id_exception(world, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "pacompany-engine")
    monkeypatch.chdir(world.client)
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    lane.assert_not_called()


def test_b10_client_path_in_text_beats_the_project_id_exception(world, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "pacompany-engine")
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness",
                         instruction=f"compare with {world.client}/offer.md")
    assert rc == 1
    lane.assert_not_called()


def test_b10_unlisted_project_has_no_exception(world):
    boundary = content_class.load_boundary(world.boundary_file)
    assert content_class.allowed_exception_providers("seocrawler-v2", boundary) == frozenset()
    assert content_class.allowed_exception_providers("pacompany-engine", boundary) == {"deepseek"}


# Unreadable marker (P12) ---------------------------------------------------

@pytest.fixture
def unreadable_marker(world):
    if os.geteuid() == 0:
        pytest.skip("root can read a mode 000 file")
    outer = world.tmp / "outer"
    (outer / "inner").mkdir(parents=True)
    marker = outer / ".vnx-project-id"
    marker.write_text("something\n", encoding="utf-8")
    marker.chmod(0)
    yield outer / "inner"
    marker.chmod(0o644)


def test_unreadable_marker_refuses_external_lane_and_allows_codex(world, unreadable_marker, monkeypatch):
    monkeypatch.chdir(unreadable_marker)
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    lane.assert_not_called()
    receipts = _refusals("d-content-1")
    assert receipts and "content-boundary-unreadable" in receipts[-1]["failure_reason"]
    rc, lane = _run_main("codex", "_dispatch_codex", dispatch_id="d-content-2")
    assert rc == 0
    lane.assert_called_once()


def test_unreadable_marker_override_needs_the_same_reason(world, unreadable_marker, monkeypatch):
    monkeypatch.chdir(unreadable_marker)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "checked by hand")
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness", dispatch_id="d-content-3")
    assert rc == 0


def test_unreadable_marker_under_a_client_root_is_refused_as_client(world, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can read a mode 000 file")
    marker = world.client / ".vnx-project-id"
    marker.write_text("x\n", encoding="utf-8")
    marker.chmod(0)
    try:
        monkeypatch.chdir(world.client)
        dc = content_class.classify_dispatch(cwd=Path.cwd(), project_root=world.fabric,
                                             boundary=content_class.load_boundary(world.boundary_file))
        assert dc.cls == "client" and not dc.unreadable
        rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
        assert rc == 1
        reason = _refusals("d-content-1")[-1]["failure_reason"]
        assert "[content-boundary]" in reason and "[content-boundary-unreadable]" not in reason
    finally:
        marker.chmod(0o644)


def test_plain_unknown_cwd_is_not_restricted(world, monkeypatch):
    monkeypatch.chdir(world.other)
    dc = content_class.classify_dispatch(
        cwd=Path.cwd(), project_root=world.other,
        boundary=content_class.load_boundary(world.boundary_file))
    assert dc.cls == "unknown" and not dc.restricted and not dc.unreadable
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 0
    lane.assert_called_once()


def test_classifier_failure_blocks_with_a_receipt(world, monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(content_class, "classify_dispatch", boom)
    rc, lane = _run_main("deepseek-harness", "_dispatch_deepseek_harness")
    assert rc == 1
    lane.assert_not_called()
    assert "content-boundary-unavailable" in _refusals("d-content-1")[-1]["failure_reason"]


# ---------------------------------------------------------------------------
# The door (build_runtime_snapshot)
# ---------------------------------------------------------------------------

def _snapshot(world: World, provider: str, instruction: str = "Role: backend-developer\n\nDo the work.\n",
              project_id: str = "vnx-dev", dispatch_paths: list[str] | None = None):
    staging_id = "20261015-staging-content"
    data_dir = world.tmp / "vnx-data"
    bundle = data_dir / "dispatches" / "pending" / staging_id
    bundle.mkdir(parents=True, exist_ok=True)
    inst = bundle / "instruction.md"
    inst.write_text(instruction, encoding="utf-8")
    spec = {
        "schema_version": 1, "project_id": project_id, "dispatch_id": "20261015-content-dispatch",
        "staging_id": staging_id, "instruction_file": str(inst), "role": "backend-developer",
        "target_slot": "T1", "gate": "codex_gate", "dispatch_paths": [{"path": p, "access": "read"} for p in dispatch_paths or []],
        "provider": provider,
        "model": None, "deadline_seconds": 3600, "isolation": "worktree",
    }
    spec_file = bundle / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    vspec = validate(load_spec(spec_file), project_id=project_id, repo_root=_REPO_ROOT)
    assert not isinstance(vspec, Reject), vspec
    return build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)


def _codes(snapshot, severity: str | None = None) -> list[str]:
    return [v.code for v in snapshot.constraint_verdicts if severity is None or v.severity == severity]


@pytest.mark.parametrize("provider", ["deepseek-harness", "glm-harness", "kimi"])
def test_b4_door_client_cwd_blocks_external_provider(world, monkeypatch, provider):
    monkeypatch.chdir(world.client)
    snapshot = _snapshot(world, provider)
    blocking = [v for v in snapshot.constraint_verdicts if v.code == "content-boundary"]
    assert blocking and blocking[0].severity == "blocking"
    assert "client" in blocking[0].message and "cwd (client_root)" in blocking[0].message


def test_b4_door_fabric_cwd_has_no_content_verdict(world):
    codes = _codes(_snapshot(world, "deepseek-harness"))
    assert "content-boundary" not in codes and "content-boundary-unreadable" not in codes


def test_b4_door_fabric_cwd_with_client_path_in_instruction_blocks(world):
    text = f"Role: backend-developer\n\nSummarise {world.client}/brief.md\n"
    snapshot = _snapshot(world, "deepseek-harness", instruction=text)
    verdict = [v for v in snapshot.constraint_verdicts if v.code == "content-boundary"]
    assert verdict and verdict[0].severity == "blocking"
    assert "instruction text (text_path)" in verdict[0].message
    assert str(world.client) not in verdict[0].message


def test_door_client_cwd_allows_claude_and_codex(world, monkeypatch):
    monkeypatch.chdir(world.client)
    for provider in ("claude", "codex"):
        assert "content-boundary" not in _codes(_snapshot(world, provider)), provider


def test_door_override_with_reason_is_a_warn_verdict(world, monkeypatch):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    assert "content-boundary" in _codes(_snapshot(world, "deepseek-harness"), "blocking")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "full panel wanted")
    snapshot = _snapshot(world, "deepseek-harness")
    verdict = [v for v in snapshot.constraint_verdicts if v.code == "content-boundary"]
    assert verdict and verdict[0].severity == "warn" and verdict[0].override_applied
    assert "full panel wanted" in verdict[0].message


def test_door_unconfigured_boundary_warns_and_blocks_nothing(world, monkeypatch):
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(world.tmp / "missing.json"))
    monkeypatch.chdir(world.client)
    snapshot = _snapshot(world, "deepseek-harness")
    assert "content-boundary-unconfigured" in _codes(snapshot, "warn")
    assert not [c for c in _codes(snapshot, "blocking") if c.startswith("content-boundary")]


def test_door_configured_boundary_has_no_unconfigured_warning(world):
    assert "content-boundary-unconfigured" not in _codes(_snapshot(world, "codex"))


def test_door_unreadable_marker_blocks_external_provider_only(world, unreadable_marker, monkeypatch):
    monkeypatch.chdir(unreadable_marker)
    assert "content-boundary-unreadable" in _codes(_snapshot(world, "deepseek-harness"), "blocking")
    assert "content-boundary-unreadable" not in _codes(_snapshot(world, "codex"))


def test_door_project_id_exception_allows_only_the_named_provider(world):
    allowed = _codes(_snapshot(world, "deepseek-harness", project_id="pacompany-engine"))
    refused = _codes(_snapshot(world, "glm-harness", project_id="pacompany-engine"), "blocking")
    assert "content-boundary" not in allowed
    assert "content-boundary" in refused


# ---------------------------------------------------------------------------
# The rule engine
# ---------------------------------------------------------------------------

def _enforcer() -> live_enforcer.ConstraintEnforcer:
    return live_enforcer.ConstraintEnforcer()


def _content_codes(provider: str, content, env=None, sub_provider=None) -> list:
    out = _enforcer().check_constraints(provider=provider, sub_provider=sub_provider,
                                        content_class=content, env=env or {})
    return [v for v in out if v.code.startswith("content-boundary")]


@pytest.mark.parametrize("provider", ["claude", "codex", "local-gemma"])
def test_allowlist_providers_pass_for_restricted_content(provider):
    assert _content_codes(provider, "client") == []
    assert _content_codes(provider, "personal") == []


@pytest.mark.parametrize("provider,sub", [
    ("deepseek-harness", None), ("glm-harness", None), ("kimi", None), ("gemini", None),
    ("litellm:openrouter", None), ("litellm", "zai"), ("auto", None), ("mystery-lane", None),
    (None, None),
])
def test_everything_else_is_refused_for_restricted_content(provider, sub):
    found = _content_codes(provider, "client", sub_provider=sub)
    assert [v.severity for v in found] == ["blocking"]


def test_content_class_none_means_the_rule_does_not_apply():
    assert _content_codes("deepseek-harness", None) == []


@pytest.mark.parametrize("cls", ["fabric", "own", "unknown"])
def test_unrestricted_classes_do_not_trigger_the_rule(cls):
    assert _content_codes("deepseek-harness", cls) == []


def test_override_needs_a_reason_and_becomes_warn():
    env = {"VNX_OVERRIDE_CONTENT_BOUNDARY": "1"}
    assert [v.severity for v in _content_codes("kimi", "client", env=env)] == ["blocking"]
    env["VNX_OVERRIDE_CONTENT_BOUNDARY_REASON"] = "because"
    found = _content_codes("kimi", "client", env=env)
    assert [(v.severity, v.override_applied) for v in found] == [("warn", True)]


def test_exception_provider_names_cover_the_harness_and_litellm_routes():
    dc = content_class.DispatchClass("client", "pacompany-engine", content_class.SRC_CLIENT_PROJECT_ID,
                                     "project id (client_project_id)",
                                     exception_providers=frozenset({"deepseek"}))
    assert _content_codes("deepseek-harness", dc) == []
    assert _content_codes("litellm:deepseek", dc) == []
    assert _content_codes("glm-harness", dc)


def test_b5_unknown_rule_type_makes_the_live_enforcer_raise(tmp_path):
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "constraints": [
        {"id": "x", "rule": "forbid_everything", "audit_severity": "blocking"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown rule type 'forbid_everything'"):
        live_enforcer.ConstraintEnforcer(path)


def test_b5_unknown_rule_type_makes_the_legacy_enforcer_raise(tmp_path):
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "constraints": [
        {"id": "x", "rule": "forbid_everything", "audit_severity": "blocking"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown rule type 'forbid_everything'"):
        legacy_enforcer.ConstraintEnforcer(path)


def test_a_constraint_without_a_rule_makes_both_enforcers_raise(tmp_path):
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "constraints": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        live_enforcer.ConstraintEnforcer(path)
    with pytest.raises(ValueError):
        legacy_enforcer.ConstraintEnforcer(path)


def test_shipped_yaml_loads_through_both_enforcers():
    live_enforcer.ConstraintEnforcer()
    legacy_enforcer.ConstraintEnforcer()


def test_shipped_yaml_rules_are_all_applied_by_the_live_enforcer():
    rules = {c["rule"] for c in yaml.safe_load(SHIPPED_YAML.read_text(encoding="utf-8"))["constraints"]}
    assert rules <= live_enforcer.APPLIED_RULES, rules - live_enforcer.APPLIED_RULES


def test_legacy_enforcer_knows_every_shipped_rule_and_skips_what_it_cannot_apply():
    rules = {c["rule"] for c in yaml.safe_load(SHIPPED_YAML.read_text(encoding="utf-8"))["constraints"]}
    known = legacy_enforcer.APPLIED_RULES | legacy_enforcer.KNOWN_NOT_APPLIED_RULES
    assert rules <= known
    assert legacy_enforcer.KNOWN_NOT_APPLIED_RULES == {"forbid_import", "require_content_route"}
    legacy_enforcer.ConstraintEnforcer().enforce(provider="codex")


def test_check_route_script_runs_on_the_shipped_yaml():
    proc = subprocess.run(
        ["bash", str(_REPO_ROOT / "scripts" / "guardrails" / "check_route.sh"), "--provider", "codex"],
        capture_output=True, text=True, check=False, cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "route allowed" in proc.stdout


def test_content_boundary_constraint_is_blocking_with_a_reason_required():
    entries = {c["id"]: c for c in yaml.safe_load(SHIPPED_YAML.read_text(encoding="utf-8"))["constraints"]}
    entry = entries["content-boundary"]
    assert entry["audit_severity"] == "blocking"
    assert entry["override_allowed"] is True and entry["override_requires_reason"] is True
    assert entry["required_content_route"]["allowed_providers"] == ["anthropic", "openai", "local_gemma"]
    assert entry["required_content_route"]["content_class"] == ["client", "personal"]


# ---------------------------------------------------------------------------
# classify_dispatch
# ---------------------------------------------------------------------------

def test_classify_dispatch_takes_the_most_restrictive_signal(world):
    boundary = content_class.load_boundary(world.boundary_file)
    dc = content_class.classify_dispatch(
        cwd=world.fabric, project_root=world.fabric,
        dispatch_paths=["scripts/x.py:write", f"{world.personal}/a.md:read"], boundary=boundary)
    assert dc.cls == "personal" and dc.signal == "dispatch path (personal_root)"


def test_classify_dispatch_resolves_relative_paths_against_the_project_root(world):
    boundary = content_class.load_boundary(world.boundary_file)
    rel = os.path.relpath(world.client, world.client_root.parent)
    dc = content_class.classify_dispatch(
        cwd=world.fabric, project_root=world.client_root.parent, dispatch_paths=[f"{rel}/**"],
        boundary=boundary)
    assert dc.cls == "client"


def test_classify_dispatch_prefers_a_non_exception_signal_on_equal_rank(world):
    boundary = content_class.load_boundary(world.boundary_file)
    dc = content_class.classify_dispatch(
        cwd=world.client, project_root=world.fabric, project_id="pacompany-engine", boundary=boundary)
    assert dc.source == "client_root" and dc.exception_providers == frozenset()
    only = content_class.classify_dispatch(
        cwd=world.fabric, project_root=world.fabric, project_id="pacompany-engine", boundary=boundary)
    assert only.source == "client_project_id" and only.exception_providers == {"deepseek"}


# ---------------------------------------------------------------------------
# Glob paths and the content boundary (PR #2063 fix round 2)
# ---------------------------------------------------------------------------

def _glob_world(world: World) -> Path:
    """A project root that holds a restricted root at ``clients/acme``."""
    project = world.tmp / "project"
    (project / "clients" / "acme").mkdir(parents=True)
    (project / "scripts").mkdir()
    world.write_boundary(client_roots=[str(project / "clients" / "acme")])
    return project


def _glob_class(world: World, project: Path, declared: str) -> content_class.DispatchClass:
    boundary = content_class.load_boundary(world.boundary_file)
    return content_class.classify_dispatch(
        cwd=world.fabric, project_root=project, dispatch_paths=[declared], boundary=boundary)


def test_glob_matching_a_restricted_root_under_the_static_prefix_is_restricted(world):
    project = _glob_world(world)
    dc = _glob_class(world, project, "clients/*/**:read")
    assert dc.cls == "client" and dc.signal == "dispatch path (client_root)"


def test_glob_codex_case_is_refused_at_the_side_door(world, monkeypatch):
    project = _glob_world(world)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    argv = _argv("deepseek-harness") + ["--dispatch-paths", "clients/*/**:read"]
    with patch.object(pd, "_dispatch_deepseek_harness", return_value=0) as lane:
        rc = pd.main(argv)
    assert rc == 1
    lane.assert_not_called()


def test_glob_codex_case_is_refused_at_the_door(world, monkeypatch):
    project = _glob_world(world)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    snapshot = _snapshot(world, "deepseek-harness", dispatch_paths=["clients/*/**"])
    assert "content-boundary" in _codes(snapshot, "blocking")


def test_double_star_markdown_from_a_root_holding_a_restricted_root_is_restricted(world):
    project = _glob_world(world)
    assert _glob_class(world, project, "**/*.md:read").cls == "client"


def test_glob_that_cannot_reach_the_restricted_root_is_not_restricted(world):
    project = _glob_world(world)
    assert not _glob_class(world, project, "scripts/**:read").restricted
    assert not _glob_class(world, project, "clients/other-*/**:read").restricted


def test_glob_with_its_static_prefix_inside_a_restricted_root_is_restricted(world):
    project = _glob_world(world)
    assert _glob_class(world, project, "clients/acme/docs/*.md:read").cls == "client"


def test_a_plain_path_above_a_restricted_root_keeps_todays_behaviour(world):
    project = _glob_world(world)
    assert not _glob_class(world, project, "clients:read").restricted


@pytest.mark.parametrize("pattern,rel,expected", [
    (("**",), ("a", "b"), True),
    (("*", "**"), ("a", "b"), True),
    (("*",), ("a", "b"), False),
    (("a*", "b"), ("ax", "b"), True),
    (("a*", "c"), ("ax", "b"), False),
    (("**", "b", "*.md"), ("a", "b"), True),
])
def test_pattern_reaches_matches_component_by_component(pattern, rel, expected):
    assert content_class._pattern_reaches(pattern, rel) is expected


def test_pattern_resolution_error_counts_as_restricted(world, monkeypatch):
    project = _glob_world(world)
    monkeypatch.setattr(content_class, "_pattern_reaches",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _glob_class(world, project, "clients/*/**:read").cls == "client"


# ---------------------------------------------------------------------------
# Applied override: the ledger record comes first (ADR-005)
# ---------------------------------------------------------------------------

def test_applied_override_is_in_the_ledger_before_the_handler_runs(world, monkeypatch):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "operator wants the full panel")
    seen: list[list[dict]] = []

    def handler(*_a, **_k):
        seen.append(_refusals("d-override-ledger"))
        return 0

    with patch.object(pd, "_dispatch_deepseek_harness", side_effect=handler):
        rc = pd.main(_argv("deepseek-harness", dispatch_id="d-override-ledger"))
    assert rc == 0
    assert len(seen) == 1
    events = [r for r in seen[0] if r.get("event_type") == "content_boundary_override_applied"]
    assert len(events) == 1
    event = events[0]
    assert event["constraint_id"] == "content-boundary"
    assert event["provider"] == "deepseek-harness" and event["model"]
    assert event["content_class"] == "client" and event["signal"] == "cwd (client_root)"
    assert event["override_reason"] == "operator wants the full panel"
    assert event["override_applied"] is True


def test_failing_ledger_write_refuses_the_override(world, monkeypatch, capsys):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "operator wants the full panel")

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pd, "_record_content_override", broken)
    with patch.object(pd, "_dispatch_deepseek_harness", return_value=0) as lane:
        rc = pd.main(_argv("deepseek-harness", dispatch_id="d-override-fail"))
    assert rc == 1
    lane.assert_not_called()
    assert "audit record could not be written" in capsys.readouterr().err


def test_door_applied_override_is_written_to_the_receipt_ledger(world, monkeypatch):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "full panel wanted")
    snapshot = _snapshot(world, "deepseek-harness")
    assert "content-boundary-audit-unwritten" not in _codes(snapshot)
    ledger = world.tmp / "vnx-data" / "state" / "t0_receipts.ndjson"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    events = [r for r in rows if r.get("event_type") == "content_boundary_override_applied"]
    assert len(events) == 1
    assert events[0]["dispatch_id"] == "20261015-content-dispatch"
    assert events[0]["override_reason"] == "full panel wanted"


def test_door_failing_ledger_write_blocks_the_override(world, monkeypatch):
    monkeypatch.chdir(world.client)
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY", "1")
    monkeypatch.setenv("VNX_OVERRIDE_CONTENT_BOUNDARY_REASON", "full panel wanted")
    import append_receipt

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(append_receipt, "append_receipt_payload", broken)
    snapshot = _snapshot(world, "deepseek-harness")
    assert "content-boundary-audit-unwritten" in _codes(snapshot, "blocking")
