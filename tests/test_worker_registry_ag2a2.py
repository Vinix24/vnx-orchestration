"""AG-2a2 whole-pair resolution, pool selection and unchanged fleet."""
import logging
from pathlib import Path
import sqlite3
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import config_registry
import config_runtime
import config_store_db
import worker_registry as registry


@pytest.fixture(autouse=True)
def clean_terminal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_runtime, "_wired_for", {})
    monkeypatch.setattr(config_runtime, "_wire_status", None)
    monkeypatch.setattr(config_registry, "_db_resolver", None)
    monkeypatch.setattr(config_registry, "_default_project_id", None)
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    for terminal in ("T1", "T2", "T3"):
        for field in ("PROVIDER", "MODEL"):
            for prefix in ("VNX_", "VNX_OVERRIDE_"):
                monkeypatch.delenv(f"{prefix}{terminal}_{field}", raising=False)


def test_resolver_exists():
    assert callable(getattr(registry, "resolve_worker", None))


@pytest.mark.parametrize("layer", ["spec", "env"])
@pytest.mark.parametrize("pair,expected", [
    ({"provider": "kimi", "model": "kimi-k3"}, ("kimi", "kimi-k3")),
    ({"model": "kimi-k3"}, ("kimi", "kimi-k3")),
    ({"provider": "codex"}, ("codex", "gpt-6-astra")),
    ({"provider": "kimi"}, ("kimi", "kimi-k3")),
    ({"model": "kimi-code/k3"}, ("kimi", "kimi-code/k3")),
    ({"model": "gemini-2.5-pro"}, ("gemini", "gemini-2.5-pro")),
    ({"model": "kimi-k2-0905-default"}, ("litellm:moonshot", "kimi-k2-0905-default")),
    ({"model": "deepseek-v4-pro"}, "worker_pair_conflict"),
    ({"provider": "litellm:anthropic", "model": "sonnet"}, ("litellm:anthropic", "sonnet")),
    ({"provider": None, "model": ""}, ("claude", "sonnet")),
    ({}, ("claude", "sonnet")),
    ({"provider": "claude", "model": "kimi-k3"}, "worker_pair_conflict"),
    ({"model": "unknown-model"}, "worker_model_unknown"),
    ({"provider": "unknown-provider"}, "worker_model_unresolved"),
    ({"provider": "claude"}, ("claude", "sonnet")),
    ({"model": 7}, "worker_model_unknown"),
])
def test_pair_table(tmp_path, monkeypatch, layer, pair, expected):
    for key, value in pair.items() if layer == "env" else []:
        if value is not None:
            monkeypatch.setenv(f"VNX_T1_{key.upper()}", str(value))
    spec = pair if layer == "spec" else {}
    if isinstance(expected, str):
        with pytest.raises(ValueError, match=expected):
            registry.resolve_worker("T1", spec=spec, repo_root=tmp_path)
    else:
        result = registry.resolve_worker("T1", spec=spec, repo_root=tmp_path)
        assert (result["provider"], result["model"]) == expected
        assert result["source"] == (layer if any(pair.values()) else "default")


def test_spec_beats_env_as_whole_pair(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("VNX_T1_PROVIDER", "claude")
    with caplog.at_level(logging.INFO, logger=registry.__name__):
        result = registry.resolve_worker("T1", spec={"model": "kimi-k3"}, repo_root=tmp_path)
    assert result == dict(provider="kimi", model="kimi-k3", source="spec", overridden=["env"])
    assert [record.getMessage() for record in caplog.records if record.name == registry.__name__] == [
        "worker_resolution: provider=kimi model=kimi-k3 source=spec overridden=[env]"]


def write_registry(tmp_path, data):
    path = tmp_path / ".vnx" / "vnx_workers.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def project_config(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    conn = sqlite3.connect(state / "runtime_coordination.db")
    try:
        config_store_db.ensure_config_tables(conn)
        for project, provider, model in (
            ("worker-project", "kimi", "kimi-k3"),
            ("worker-project-shadow", "codex", "gpt-6-astra"),
        ):
            for field, value in (("PROVIDER", provider), ("MODEL", model)):
                conn.execute(
                    "INSERT INTO project_config (project_id, config_key, config_value, updated_by) "
                    "VALUES (?, ?, ?, 'test')", (project, f"VNX_T1_{field}", value))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("VNX_PROJECT_ID", "worker-project-shadow")
    return state


@pytest.mark.parametrize("with_registry", [False, True])
@pytest.mark.parametrize("project_id", [None, "worker-project"])
def test_project_config_pair_wins_unwired(tmp_path, monkeypatch, project_config, with_registry, project_id):
    if with_registry:
        write_registry(tmp_path, yaml.safe_load(
            (ROOT / "scripts/lib/defaults/vnx_workers.default.yaml").read_text()))
    if project_id is None:
        monkeypatch.setenv("VNX_PROJECT_ID", "worker-project")
    assert config_registry._db_resolver is None
    result = registry.resolve_worker("T1", repo_root=tmp_path, project_id=project_id)
    assert result == dict(provider="kimi", model="kimi-k3", source="env",
                          overridden=["registry"] if with_registry else [])


@pytest.mark.parametrize("wired", [False, True])
@pytest.mark.parametrize("failure", ["corrupt", "unreadable"])
def test_project_config_db_failure_refuses(tmp_path, project_config, wired, failure):
    write_registry(tmp_path, yaml.safe_load(
        (ROOT / "scripts/lib/defaults/vnx_workers.default.yaml").read_text()))
    if wired:
        assert config_runtime.autowire(project_id="worker-project")
    db = project_config / "runtime_coordination.db"
    if failure == "corrupt":
        broken = db.with_suffix(".tmp")
        broken.write_bytes(b"not a SQLite database" * 100)
        broken.replace(db)
    else:
        db.rename(db.with_suffix(".original"))
        db.mkdir()  # A directory cannot be opened as SQLite, even by a privileged runner.
    with pytest.raises(ValueError, match="^worker_config_unreadable: project_config_error$"):
        registry.resolve_worker("T1", repo_root=tmp_path, project_id="worker-project")


@pytest.mark.parametrize("project_id", [None, "worker-project"])
def test_missing_store_ignores_previously_wired_resolver(tmp_path, monkeypatch, project_config, project_id):
    assert config_runtime.autowire(project_id="worker-project")
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "missing-state"))
    monkeypatch.delenv("VNX_PROJECT_ID")
    result = registry.resolve_worker("T1", repo_root=tmp_path, project_id=project_id)
    assert (result["provider"], result["model"], result["source"]) == ("claude", "sonnet", "default")


def test_config_store_discovery_error_refuses(tmp_path, monkeypatch):
    import vnx_paths

    def unreadable_paths():
        raise OSError("project store lookup failed")

    monkeypatch.delenv("VNX_STATE_DIR")
    monkeypatch.setattr(vnx_paths, "resolve_paths", unreadable_paths)
    with pytest.raises(ValueError, match="^worker_config_unreadable: project_config_error$"):
        registry.resolve_worker("T1", repo_root=tmp_path, project_id="worker-project")


@pytest.mark.parametrize("default_pair", [None, {"provider": "kimi", "model": "kimi-k3"}])
def test_mixed_pool(tmp_path, default_pair):
    data = yaml.safe_load((ROOT / "scripts/lib/defaults/vnx_workers.default.yaml").read_text())
    data["workers"][1].update(provider="kimi", model="kimi-k3")
    if default_pair:
        data["pools"][0]["default_pair"] = default_pair
    write_registry(tmp_path, data)
    if default_pair is None:
        with pytest.raises(ValueError, match="pool_default_ambiguous.*default"):
            registry.resolve_worker(repo_root=tmp_path)
    else:
        result = registry.resolve_worker(repo_root=tmp_path)
        assert (result["provider"], result["model"], result["source"]) == ("kimi", "kimi-k3", "registry")


@pytest.mark.parametrize("pair", [None, {}, [], {"provider": "kimi"},
    {"provider": "claude", "model": "kimi-k3"}, {"provider": "kimi", "model": "missing"}])
def test_invalid_pool_default(pair):
    with pytest.raises(ValueError):
        registry._build_registry({"pools": [{"pool_id": "mixed", "default_pair": pair}]})


@pytest.mark.parametrize("target", ["T1", "T2", "T3", None])
def test_fleet_matches_current_pin(target):
    constraints = yaml.safe_load((ROOT / "scripts/lib/providers/provider_constraints.yaml").read_text())
    pin = next(c for c in constraints["constraints"] if c["id"] == "workers-kimi-pinned")
    assert pin["pin_semantics"] == "default"
    result = registry.resolve_worker(target, repo_root=ROOT)
    assert (result["provider"], result["model"]) == ("claude", pin["required_route"]["model"])


@pytest.mark.parametrize("data,error", [
    (None, "schema_version"), ({"schema_version": 2}, "schema_version"),
    ({"schema_version": 1, "pools": [{"pool_id": "default"}], "workers": []}, "worker_model_unresolved"),
])
def test_invalid_registry_stays_visible(tmp_path, data, error):
    write_registry(tmp_path, data)
    with pytest.raises(ValueError, match=error):
        registry.resolve_worker(repo_root=tmp_path)


def test_unreadable_registry_stays_visible(tmp_path):
    path = tmp_path / ".vnx" / "vnx_workers.yaml"
    path.mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        registry.resolve_worker(repo_root=tmp_path)


@pytest.mark.parametrize("target", ["T1", "T2", None])
@pytest.mark.parametrize("env_model", [None, "sonnet"])
def test_project_precedence_and_missing_target_fallback(tmp_path, monkeypatch, target, env_model):
    data = yaml.safe_load((ROOT / "scripts/lib/defaults/vnx_workers.default.yaml").read_text())
    data["workers"] = [data["workers"][1]]
    data["workers"][0].update(provider="kimi", model="kimi-k3", pool_id="other")
    data["pools"][0]["pool_id"] = "other"
    write_registry(tmp_path, data)
    if env_model:
        monkeypatch.setenv("VNX_T1_MODEL", env_model)
    result = registry.resolve_worker(target, repo_root=tmp_path)
    assert (result["model"], result["source"]) == (
        (env_model, "env") if target == "T1" and env_model else
        ("kimi-k3", "registry") if target == "T1" else ("sonnet", "default"))


@pytest.mark.parametrize("spec", [[], {"model": 0}, {"provider": [], "model": "sonnet"}])
def test_malformed_spec_refuses(tmp_path, spec):
    with pytest.raises(ValueError):
        registry.resolve_worker("T1", repo_root=tmp_path, spec=spec)


def test_config_read_failure_is_visible(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("invalid JSON")
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(path))
    with pytest.raises(ValueError, match="worker_config_unreadable"):
        registry.resolve_worker("T1", repo_root=tmp_path)


def test_missing_default_and_unknown_target_refuse(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="worker_model_unresolved"):
        registry.resolve_worker("T99", repo_root=tmp_path)
    monkeypatch.setattr(registry, "__file__", str(tmp_path / "worker_registry.py"))
    with pytest.raises(FileNotFoundError, match="vnx_workers.default.yaml"):
        registry.resolve_worker("T1", repo_root=tmp_path)
