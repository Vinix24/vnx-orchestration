"""AG-2c: the door and D4 share registry resolution; pins only check builders."""
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest
import yaml

from test_worker_registry_ag2a2 import clean_terminal_env, registry, write_registry
from test_dispatch_plan import _make_spec, _make_vspec, _healthy_snapshot
from dispatch_cli import _override_gate_and_effective_model
from dispatch_plan import ModelPin, Reject, RuntimeSnapshot, _resolve_model, compile_plan
from dispatch_spec import Provider
from providers import provider_registry


@pytest.fixture
def project(tmp_path, monkeypatch, clean_terminal_env):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    data = yaml.safe_load((Path(registry.__file__).parent / "defaults/vnx_workers.default.yaml").read_text())
    for worker in data["workers"]:
        worker["model"] = "opus"
    write_registry(tmp_path, data)
    return tmp_path


@pytest.fixture(params=["door", "D4"])
def resolve(request, project):
    def run(spec):
        pins = {spec.target_slot: ModelPin("sonnet", "default")}
        if request.param == "door":
            verdicts, _, model = _override_gate_and_effective_model(spec, spec.provider == Provider.CLAUDE, pins)
            return model, verdicts
        warnings = []
        model = _resolve_model(spec, RuntimeSnapshot(model_pins=pins, repo_root=project),
                               spec.provider == Provider.CLAUDE, warnings)
        return model, warnings
    return run


@pytest.mark.parametrize("slot", ["T1", "T2", "T3", "pool"])
def test_registry_beats_pin(project, resolve, slot):
    spec = _make_spec(tmp_path=project, target_slot=slot)
    expected = registry.resolve_worker(None if slot == "pool" else slot, repo_root=project)
    assert resolve(spec)[0] == expected["model"] == "opus"


@pytest.mark.parametrize("model", ["sonnet", "haiku"])
def test_spec_wins(project, resolve, model):
    assert resolve(_make_spec(tmp_path=project, model=model))[0] == model


def test_env_wins(project, resolve, monkeypatch):
    monkeypatch.setenv("VNX_T1_MODEL", "haiku")
    assert resolve(_make_spec(tmp_path=project))[0] == "haiku"


@pytest.mark.parametrize("provider", [p for p in Provider if p != Provider.AUTO])
def test_provider_only(project, resolve, provider):
    spec = _make_spec(tmp_path=project, provider=provider)
    expected = "opus" if provider == Provider.CLAUDE else registry.resolve_worker(
        "T1", spec={"provider": provider.value}, repo_root=project)["model"]
    assert resolve(spec)[0] == expected


@pytest.mark.parametrize("failure", ["missing", "null", "malformed", "unknown", "unreadable", "provider", "yaml"])
def test_unresolved_provider_blocks(project, resolve, monkeypatch, failure):
    data = yaml.safe_load(Path(provider_registry._REGISTRY_PATH).read_text())
    cfg = data["providers"]["openai"]
    cfg.pop("default_model")
    if failure != "missing":
        cfg["default_model"] = {"null": None, "malformed": [], "unknown": "unknown", "unreadable": None, "provider": None, "yaml": None}[failure]
    if failure == "provider":
        del data["providers"]["openai"]
    path = project / "catalogue.yaml"
    path.write_text(yaml.safe_dump(data))
    if failure == "yaml":
        path.write_text("providers: [")
    if failure == "unreadable":
        path = project / "absent.yaml"
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    model, verdicts = resolve(_make_spec(tmp_path=project, provider=Provider.CODEX))
    failure = model if isinstance(model, Reject) else next(v for v in verdicts if v.severity == "blocking")
    message = failure.reason if isinstance(failure, Reject) else failure.message
    assert all(value in message for value in ("T1", "codex", "openai"))
    assert isinstance(compile_plan(_make_vspec(tmp_path=project, provider=Provider.CODEX),
                                  replace(_healthy_snapshot(), repo_root=project)), Reject)


def test_pin_warning_keeps_registry_model(project):
    snapshot = replace(_healthy_snapshot(model_pins={"T1": ModelPin("sonnet", "default")}), repo_root=project)
    plan = compile_plan(_make_vspec(tmp_path=project), snapshot)
    assert plan.model == "opus"
    assert any("model-tier" in warning for warning in plan.warnings)


@pytest.mark.parametrize("contents", ["[", "null", "schema_version: 999", "unreadable"])
def test_bad_worker_registry_blocks(project, resolve, contents):
    path = project / ".vnx/vnx_workers.yaml"
    if contents == "unreadable":
        path.rename(path.with_suffix(".original"))
        path.mkdir()
    else:
        path.write_text(contents)
    model, verdicts = resolve(_make_spec(tmp_path=project))
    assert isinstance(model, Reject) or any(v.severity == "blocking" for v in verdicts)


def test_d4_preserves_door_checked_model(project):
    spec = _make_vspec(tmp_path=project)
    _, _, checked = _override_gate_and_effective_model(spec.spec, True, {})
    snapshot = replace(_healthy_snapshot(), effective_model=checked, repo_root=project)
    (project / ".vnx/vnx_workers.yaml").write_text("schema_version: 999")
    assert compile_plan(spec, snapshot).model == checked == "opus"
