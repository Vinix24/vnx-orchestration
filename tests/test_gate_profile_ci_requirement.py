"""OF-2c (ADR-092 decision 4): ``gate_profile.ci_requirement``, the one CI rule with four
sources; ``required_stack``, ``review_stack`` and ``_ci_gate_source`` pinned to origin/main."""
from __future__ import annotations

import pytest
import yaml

from review_stack_support import isolate_config_registry

import config_registry
import gate_profile
from dispatch_spec import ReviewGateConfigError
from forge_protection_drift import YamlFetchResult
from gate_profile import BranchProtectionUnreadable, ci_requirement

APP = 4869217


def _none(_project_id, _key):
    return None


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "global" / "config.json"))
    monkeypatch.delenv("VNX_HOME", raising=False)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    yield from isolate_config_registry(monkeypatch)


def _protection(contexts):
    return yaml.safe_dump({
        "branch": "main",
        "required_status_checks": {"strict": False, "checks": [
            {"context": c, "app_id": APP if c.startswith("vnx-gate/") else 15368} for c in contexts]},
        "pending_checks": [], "required_pull_request_reviews": None,
        **{f: False for f in ("enforce_admins", "required_signatures", "required_linear_history",
                              "allow_force_pushes", "allow_deletions", "allow_fork_syncing",
                              "block_creations", "lock_branch", "required_conversation_resolution")},
        "restrictions": None, "repo": {"allow_auto_merge": False}, "rulesets": [],
        "app": {"slug": "vnx-gate", "app_id": APP},
    })


def _dials(tmp_path, level):
    return _write(tmp_path, "governance_enforcement.yaml", f"checks:\n  ci_green_required:\n    level: {level}\n")


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _req(tmp_path, *, level=1, **kw):
    kw.setdefault("project_root", tmp_path / "proj")
    return ci_requirement(None, resolver=_none, config_path=_dials(tmp_path, level), **kw)


def test_flag_zero_dial_one_no_yaml_no_chain_entry_is_not_required(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    (tmp_path / "proj").mkdir()
    assert _req(tmp_path, level=1) == (False, [])


def test_registry_default_alone_counts_where_ci_gate_source_did_not(tmp_path):
    assert config_registry.resolve_with_source("VNX_CI_GATE_REQUIRED", None, _none) == ("1", "default")
    assert gate_profile._ci_gate_source(None, _none) is None
    assert _req(tmp_path) == (True, ["default"])


@pytest.mark.parametrize("env, row, source", [("VNX_CI_GATE_REQUIRED", None, "env"),
                                             ("VNX_OVERRIDE_CI_GATE_REQUIRED", None, "override"),
                                             (None, "true", "project_config")])
def test_flag_names_its_source(tmp_path, monkeypatch, env, row, source):
    if env:
        monkeypatch.setenv(env, "yes")
    assert ci_requirement(None, resolver=lambda _p, k: row if k == "VNX_CI_GATE_REQUIRED" else None,
                          config_path=_dials(tmp_path, 1), project_root=tmp_path) == (True, [source])


@pytest.mark.parametrize("raw", ["maybe", "", "2"])
def test_a_flag_neither_true_nor_false_refuses(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", raw)
    with pytest.raises(ReviewGateConfigError, match="neither true nor false"):
        _req(tmp_path)


def test_an_unreadable_config_store_refuses(tmp_path):
    def broken(_p, _k):
        raise OSError("db locked")
    with pytest.raises(ReviewGateConfigError, match="cannot be read"):
        ci_requirement(None, resolver=broken, config_path=_dials(tmp_path, 1), project_root=tmp_path / "proj")


@pytest.mark.parametrize("level, asks", [(1, False), (0, False), (2, True), (3, True)])
def test_dial_alone(tmp_path, monkeypatch, level, asks):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    assert _req(tmp_path, level=level) == (asks, ["dial:ci_green_required"] if asks else [])


class TestBranchProtection:
    """V5.2's three cases, plus what counts as a CI context and the unreadable file."""

    @pytest.fixture(autouse=True)
    def _flag_off(self, monkeypatch):
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

    def test_only_project_copy_with_a_ci_context_requires(self, tmp_path):
        path = _write(tmp_path / "proj", ".vnx/branch_protection.yaml", _protection(["Profile A", "vnx-gate/door"]))
        assert _req(tmp_path) == (True, [f"branch_protection:{path}"])

    def test_both_paths_absent_asks_nothing(self, tmp_path):
        (tmp_path / "proj").mkdir()
        assert _req(tmp_path) == (False, [])

    def test_both_present_the_project_copy_wins(self, tmp_path):
        root = tmp_path / "proj"
        project = _write(root, ".vnx/branch_protection.yaml", _protection(["vnx-gate/review"]))
        _write(root, "scripts/forge/branch_protection.yaml", _protection(["Profile A"]))
        assert _req(tmp_path) == (False, [])
        project.write_text(_protection(["Profile A"]), encoding="utf-8")
        _write(root, "scripts/forge/branch_protection.yaml", _protection(["vnx-gate/review"]))
        assert _req(tmp_path) == (True, [f"branch_protection:{project}"])

    def test_fabric_path_is_the_fallback(self, tmp_path):
        path = _write(tmp_path / "proj", "scripts/forge/branch_protection.yaml", _protection(["VNX CI"]))
        assert _req(tmp_path) == (True, [f"branch_protection:{path}"])

    def test_vnx_gate_contexts_alone_are_not_ci(self, tmp_path):
        _write(tmp_path / "proj", ".vnx/branch_protection.yaml", _protection(["vnx-gate/review", "vnx-gate/door"]))
        assert _req(tmp_path) == (False, [])

    @pytest.mark.parametrize("text", ["branch: [unclosed", "branch: main\n", ""])
    def test_invalid_yaml_raises_the_named_error(self, tmp_path, text):
        _write(tmp_path / "proj", ".vnx/branch_protection.yaml", text)
        with pytest.raises(BranchProtectionUnreadable, match="branch_protection_unreadable"):
            _req(tmp_path)

    def test_undecodable_file_is_unreadable_not_absent(self, tmp_path):
        _write(tmp_path / "proj", ".vnx/branch_protection.yaml", "x").write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(BranchProtectionUnreadable):
            _req(tmp_path)

    def test_prefetched_main_copy_is_used_instead_of_the_checkout(self, tmp_path):
        _write(tmp_path / "proj", ".vnx/branch_protection.yaml", _protection(["vnx-gate/door"]))
        fetched = (".vnx/branch_protection.yaml", YamlFetchResult(_protection(["Profile A"]), False, None))
        assert _req(tmp_path, protection_yaml=fetched) == (True, ["branch_protection:.vnx/branch_protection.yaml"])

    def test_prefetched_not_found_asks_nothing(self, tmp_path):
        assert _req(tmp_path, protection_yaml=("", YamlFetchResult(None, True, None))) == (False, [])

    @pytest.mark.parametrize("result", [YamlFetchResult(None, False, "gh faalde"),
                                        YamlFetchResult("branch: [", False, None)])
    def test_prefetched_error_or_invalid_raises(self, tmp_path, result):
        with pytest.raises(BranchProtectionUnreadable):
            _req(tmp_path, protection_yaml=("scripts/forge/branch_protection.yaml", result))


def test_ci_gate_in_the_review_chain_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "codex_gate,ci_gate")
    assert _req(tmp_path) == (True, ["review_stack:env"])


def test_every_source_named_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "codex_gate,ci_gate")
    path = _write(tmp_path / "proj", ".vnx/branch_protection.yaml", _protection(["Profile A"]))
    assert _req(tmp_path, level=3) == (
        True, ["default", "dial:ci_green_required", f"branch_protection:{path}", "review_stack:env"])


class TestNoCallerChanged:
    """The values these return on origin/main b2a09414 for the same inputs."""

    @pytest.mark.parametrize("raw, expected", [(None, None), ("1", "env"), ("0", None), ("maybe", None)])
    def test_ci_gate_source(self, monkeypatch, raw, expected):
        if raw is not None:
            monkeypatch.setenv("VNX_CI_GATE_REQUIRED", raw)
        assert gate_profile._ci_gate_source(None, _none) == expected

    def test_review_stack(self):
        assert gate_profile.review_stack(None, gate_profile.resolve(None, resolver=_none), _none) == (
            ["codex_gate", "deepseek_gate"], "default")

    def test_required_stack_ignores_yaml_and_default_flag(self, tmp_path, monkeypatch):
        _write(tmp_path / "proj", ".vnx/branch_protection.yaml", _protection(["Profile A"]))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "proj"))
        assert gate_profile.required_stack(None, resolver=_none, config_path=_dials(tmp_path, 1)) == (
            ["codex_gate", "deepseek_gate"], {"codex_gate": "default", "deepseek_gate": "default"})
        assert gate_profile.required_stack(None, resolver=_none, config_path=_dials(tmp_path, 2)) == (
            ["ci_gate"], {"ci_gate": "enforcement"})
