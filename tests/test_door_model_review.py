"""PR 3233: provider overrides and consumer registry authority at the door/D4."""
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest
import yaml

from test_door_worker_pair import project, resolve
from test_worker_registry_ag2a2 import clean_terminal_env, write_registry
from test_dispatch_plan import _make_spec, _make_vspec, _healthy_snapshot
from dispatch_cli import build_runtime_snapshot
from dispatch_plan import Reject, RuntimeSnapshot, _resolve_model, compile_plan
from dispatch_spec import Provider


OVERRIDES = [
    (Provider.CODEX, "VNX_CODEX_MODEL", "gpt-5.5", "gpt-5.4"),
    (Provider.CODEX, "VNX_CODEX_DEFAULT_MODEL", "gpt-5.5", "gpt-5.4"),
    (Provider.KIMI, "VNX_KIMI_MODEL", "kimi-k2-7", "kimi-k3"),
    (Provider.LITELLM_DEEPSEEK, "VNX_LITELLM_MODEL", "deepseek-v4-flash", "deepseek-v4-pro"),
    (Provider.LITELLM_ZAI, "VNX_LITELLM_MODEL", "glm-5.3", "glm-5.2"),
    (Provider.LITELLM_MOONSHOT, "VNX_LITELLM_MODEL", "kimi-k2-6", "kimi-k2-0905-default"),
]


@pytest.mark.parametrize("provider,name,value,explicit", OVERRIDES)
@pytest.mark.parametrize("via_door", [True, False], ids=["door", "D4"])
@pytest.mark.parametrize("with_spec", [False, True], ids=["env", "spec"])
def test_operator_model_precedence(project, monkeypatch, provider, name, value, explicit, via_door, with_spec):
    for _, env_name, _, _ in OVERRIDES:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv(name, value)
    vspec = _make_vspec(tmp_path=project, provider=provider, model=explicit if with_spec else None)
    expected = explicit if with_spec else value
    snapshot = replace(_healthy_snapshot(), repo_root=project)
    if via_door:
        measured = build_runtime_snapshot(vspec, data_dir=project / "data", spec_file=project / "spec.json")
        assert measured.effective_model == expected
        snapshot = replace(snapshot, effective_model=measured.effective_model)
    assert compile_plan(vspec, snapshot).model == expected


def test_consumer_registry_beats_cwd(project, monkeypatch, resolve, tmp_path_factory):
    other = tmp_path_factory.mktemp("other-git-project")
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    data = yaml.safe_load((project / ".vnx/vnx_workers.yaml").read_text())
    for worker in data["workers"]:
        worker["model"] = "haiku"
    write_registry(other, data)
    monkeypatch.chdir(other)
    assert resolve(_make_spec(tmp_path=project))[0] == "opus"


def test_unresolvable_root_rejects_outside_git(tmp_path, monkeypatch, clean_terminal_env):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VNX_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("VNX_CANONICAL_ROOT", raising=False)
    result = _resolve_model(_make_spec(tmp_path=tmp_path), RuntimeSnapshot(), True, [])
    assert isinstance(result, Reject)
    assert result.code == "worker_model_unresolved"
    assert all(v in result.reason for v in ("T1", "claude", "anthropic"))


@pytest.mark.parametrize("shape", ["terminal_id", "role", "workers-null", "workers-map", "workers-empty-map", "entry", "pool-entry"])
def test_wrong_registry_shape_rejects(project, resolve, shape):
    path = project / ".vnx/vnx_workers.yaml"
    data = yaml.safe_load(path.read_text())
    if shape in ("terminal_id", "role"):
        del data["workers"][0][shape]
    elif shape == "pool-entry":
        data["pools"][0] = None
    elif shape == "entry":
        data["workers"][0] = None
    else:
        data["workers"] = {"workers-null": None, "workers-map": {"T1": {}}, "workers-empty-map": {}}[shape]
    path.write_text(yaml.safe_dump(data))
    result, verdicts = resolve(_make_spec(tmp_path=project))
    failure = result if isinstance(result, Reject) else next(v for v in verdicts if v.severity == "blocking")
    assert failure.code == "worker_model_unresolved"
    message = failure.reason if isinstance(failure, Reject) else failure.message
    assert all(v in message for v in ("T1", "claude", "anthropic"))


@pytest.mark.parametrize("error", [RuntimeError, OSError, ValueError])
def test_door_root_failure_is_blocking(project, monkeypatch, error):
    import dispatch_worktree_isolation
    def unavailable():
        raise error("consumer root unavailable")
    monkeypatch.setattr(dispatch_worktree_isolation, "resolve_consumer_project_root", unavailable)
    from dispatch_cli import _override_gate_and_effective_model
    verdicts, _, model = _override_gate_and_effective_model(_make_spec(tmp_path=project), True, {})
    assert model == ""
    assert verdicts[0].code == "worker_model_unresolved"
    assert all(v in verdicts[0].message for v in ("T1", "claude", "anthropic", "consumer root unavailable"))


def test_codex_lane_override_beats_spawn_default(project, monkeypatch, resolve):
    monkeypatch.setenv("VNX_CODEX_MODEL", "gpt-5.5")
    monkeypatch.setenv("VNX_CODEX_DEFAULT_MODEL", "gpt-5.4")
    assert resolve(_make_spec(tmp_path=project, provider=Provider.CODEX))[0] == "gpt-5.5"


def test_legacy_registry_remains_cached_and_errors_visible(project, monkeypatch):
    import worker_registry
    monkeypatch.setitem(vars(worker_registry), "WORKER_REGISTRY", None)
    with pytest.raises(AttributeError, match="no attribute"):
        worker_registry.__getattr__("unknown_registry_attribute")
    path = project / ".vnx/vnx_workers.yaml"
    contents = path.read_text()
    path.write_text("[")
    with pytest.raises(yaml.YAMLError):
        worker_registry.list_workers()
    path.write_text(contents)
    assert worker_registry.by_id("T1").model == "opus"
    assert worker_registry.WORKER_REGISTRY is worker_registry.__getattr__("WORKER_REGISTRY")
