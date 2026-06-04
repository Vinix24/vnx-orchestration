"""Tests for worker_runner — contract for the YAML-config refactor.

These tests run against the seed (hardcoded QUEUES) AND must still pass
against the refactored version. Do not modify them.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def isolated_workdir(tmp_path, monkeypatch):
    """Run each test in an isolated CWD so config/ doesn't leak between tests."""
    monkeypatch.chdir(tmp_path)
    seed_module = Path(__file__).resolve().parent.parent / "worker_runner.py"
    target = tmp_path / "worker_runner.py"
    target.write_text(seed_module.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("worker_runner", None)
    return tmp_path


def _reload(monkeypatch, env_value=None):
    if env_value is None:
        monkeypatch.delenv("WORKER_QUEUES", raising=False)
    else:
        monkeypatch.setenv("WORKER_QUEUES", env_value)
    sys.modules.pop("worker_runner", None)
    return importlib.import_module("worker_runner")


def test_default_queues_when_no_config(isolated_workdir, monkeypatch):
    mod = _reload(monkeypatch)
    queues = mod.load_queues() if hasattr(mod, "load_queues") else mod.QUEUES
    assert queues == ["default"], f"expected ['default'], got {queues}"


def test_yaml_config_loads(isolated_workdir, monkeypatch):
    cfg_dir = isolated_workdir / "config"
    cfg_dir.mkdir()
    (cfg_dir / "worker_queues.yaml").write_text(
        yaml.safe_dump({"queues": ["alpha", "beta", "gamma"]}),
        encoding="utf-8",
    )
    mod = _reload(monkeypatch)
    queues = mod.load_queues() if hasattr(mod, "load_queues") else mod.QUEUES
    assert queues == ["alpha", "beta", "gamma"], f"yaml load failed: {queues}"


def test_env_var_overrides_yaml(isolated_workdir, monkeypatch):
    cfg_dir = isolated_workdir / "config"
    cfg_dir.mkdir()
    (cfg_dir / "worker_queues.yaml").write_text(
        yaml.safe_dump({"queues": ["yaml1", "yaml2"]}),
        encoding="utf-8",
    )
    mod = _reload(monkeypatch, env_value="env_a,env_b,env_c")
    queues = mod.load_queues() if hasattr(mod, "load_queues") else mod.QUEUES
    assert queues == ["env_a", "env_b", "env_c"], f"env override failed: {queues}"


def test_module_attribute_is_loaded(isolated_workdir, monkeypatch):
    cfg_dir = isolated_workdir / "config"
    cfg_dir.mkdir()
    (cfg_dir / "worker_queues.yaml").write_text(
        yaml.safe_dump({"queues": ["one", "two"]}),
        encoding="utf-8",
    )
    mod = _reload(monkeypatch)
    assert isinstance(mod.QUEUES, list), "QUEUES must be a list attribute"
    assert mod.QUEUES == ["one", "two"], f"QUEUES module attr not resolved: {mod.QUEUES}"


def test_yaml_parse_error_falls_back(isolated_workdir, monkeypatch):
    cfg_dir = isolated_workdir / "config"
    cfg_dir.mkdir()
    (cfg_dir / "worker_queues.yaml").write_text(
        "queues: [unclosed\n",
        encoding="utf-8",
    )
    mod = _reload(monkeypatch)
    queues = mod.load_queues() if hasattr(mod, "load_queues") else mod.QUEUES
    assert queues == ["default"], f"malformed yaml should fall back: {queues}"
