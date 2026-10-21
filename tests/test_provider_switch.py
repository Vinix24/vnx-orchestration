"""Behaviour of scripts/lib/provider_switch.py (provider-schakelaar A).

Never touches the operator's ~/.vnx: conftest pins VNX_PROVIDER_SWITCH_FILE into tmp_path per
test, and every test here works on that tmp file.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pwd
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Iterable, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import provider_switch as ps  # noqa: E402

FAMILIES = ps.SWITCHABLE_FAMILIES


def strip_ambient_overrides(monkeypatch) -> None:
    """Remove every ambient VNX_OVERRIDE_* variable, not only the provider brakes conftest
    strips: a test that needs one sets it itself, after the fixture."""
    for key in [k for k in os.environ if k.startswith("VNX_OVERRIDE_")]:
        monkeypatch.delenv(key)


@pytest.fixture
def switch_file(monkeypatch) -> Path:
    strip_ambient_overrides(monkeypatch)
    path = ps.switch_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def no_project(monkeypatch):
    """Decisions in these tests are for an explicit project or for none at all."""
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    import vnx_paths
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {"VNX_STATE_DIR": "/nonexistent-vnx-state/state"})


@pytest.fixture
def writable(monkeypatch, tmp_path):
    """The live write path: no dispatch marker and a working directory outside any repo."""
    for marker in ps.DISPATCH_MARKER_ENV:
        monkeypatch.delenv(marker, raising=False)
    strip_ambient_overrides(monkeypatch)
    outside = tmp_path / "outside-any-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return outside


def write_doc(path: Path, document) -> None:
    path.write_text(json.dumps(document) if not isinstance(document, str) else document, encoding="utf-8")


def entry(enabled: bool, **extra):
    return {"enabled": enabled, "changed_at": "2026-10-12T08:00:00Z", "changed_by": "op", "reason": "r", **extra}


def audit_lines(path: Path):
    log = ps.audit_log_path(path)
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Family map
# ---------------------------------------------------------------------------

REQUIRED_MAPPINGS = {
    "deepseek": ["deepseek", "deepseek-harness", "deepseek_harness", "deepseek_gate", "deepseek-v4-pro",
                 "litellm:deepseek", "litellm:deepseek/deepseek-v4-pro", "litellm:openrouter/deepseek/deepseek-chat"],
    "glm": ["glm", "glm-harness", "glm_gate", "glm-5.2", "zai", "z-ai/glm-5.2", "litellm:zai",
            "litellm:openrouter/z-ai/glm-5.3"],
    "kimi": ["kimi", "kimi_cli", "kimi_gate", "kimi-k3", "moonshot", "moonshot/kimi-k2.6",
             "litellm:moonshot", "litellm:openrouter/moonshotai/kimi-k2"],
    "gemini": ["gemini", "gemini_review", "gemini-2.5-pro", "litellm:openrouter/google/gemini-2.5-pro"],
    "local": ["ollama", "ollama:gemma3:4b", "local-gemma", "local_gemma", "gemma-4b-local"],
    "openrouter": ["litellm:openrouter/meta-llama/llama-4", "litellm:openrouter/mistralai/x"],
    "claude": ["claude", "claude_headless", "anthropic", "opus", "opus-5-5", "sonnet", "haiku", "fable-5",
               "claude-opus-5-5"],
    "codex": ["codex", "codex_gate", "openai", "gpt-5.5"],
    "tooling": ["gh", "ci_gate", "wiring_gate", "claude_github_optional"],
    "unresolved": ["auto"],
}


@pytest.mark.parametrize("family,identifier",
                         [(f, i) for f, ids in REQUIRED_MAPPINGS.items() for i in ids])
def test_family_of_required_mappings(family, identifier):
    assert ps.family_of(identifier) == family
    assert ps.family_of(f"  {identifier.upper()} ") == family


@pytest.mark.parametrize("identifier", ["mistral", "", "litellm:mistral/large", "llama-4", None, 3])
def test_family_of_unknown_raises(identifier):
    with pytest.raises(ps.UnknownProviderError):
        ps.family_of(identifier)


@pytest.mark.parametrize("url,model,family", [
    ("https://api.deepseek.com/anthropic", None, "deepseek"),
    ("https://openrouter.ai/api/v1", None, "openrouter"),
    ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat", "deepseek"),
    ("https://openrouter.ai/api/v1", "z-ai/glm-5.2", "glm"),
    ("https://openrouter.ai/api/v1", "meta-llama/llama-4", "openrouter"),
    ("https://api.moonshot.ai/anthropic", None, "kimi"),
    ("https://api.moonshot.cn/v1", None, "kimi"),
    ("https://api.z.ai/api/anthropic", None, "glm"),
    ("https://open.bigmodel.cn/api", None, "glm"),
    ("https://us-central1-aiplatform.googleapis.com/v1", None, "gemini"),
    ("https://generativelanguage.googleapis.com/v1beta", None, "gemini"),
    ("http://localhost:11434", None, "local"),
    ("http://127.0.0.1:4141", None, "glm"),
    ("localhost:4141", None, "glm"),
    ("https://api.anthropic.com", None, "claude"),
])
def test_family_of_host(url, model, family):
    assert ps.family_of_host(url, model=model) == family


def test_family_of_host_unknown_raises():
    with pytest.raises(ps.UnknownProviderError):
        ps.family_of_host("https://llm.example.invalid/v1")


@pytest.mark.parametrize("url,family", [
    ("http://localhost:11434", "local"),
    ("http://127.0.0.1:11434/api/generate", "local"),
    ("http://[::1]:11434", "local"),
    ("LOCALHOST:11434", "local"),
    ("http://localhost:4141/v1/messages", "glm"),
    ("127.0.0.1:4141", "glm"),
    ("http://[::1]:4141", "glm"),
])
def test_family_of_host_maps_the_ports_on_loopback(url, family):
    assert ps.family_of_host(url) == family


NON_LOOPBACK_ON_A_MAPPED_PORT = [
    "http://some-remote-host:11434",
    "https://10.0.0.5:11434/api/generate",
    "gpu-box.lan:11434",
    "http://other:4141",
    "http://127.0.0.2:4141/v1",
    "http://[fe80::1]:4141",
]


@pytest.mark.parametrize("url", NON_LOOPBACK_ON_A_MAPPED_PORT)
def test_family_of_host_refuses_a_mapped_port_on_another_host(url):
    with pytest.raises(ps.UnknownProviderError):
        ps.family_of_host(url)


@pytest.mark.parametrize("url", NON_LOOPBACK_ON_A_MAPPED_PORT)
def test_a_host_decision_for_a_non_loopback_port_is_refused(url, switch_file):
    decision = ps.decide_host(url, consumer="harness", project_id="p-x")
    assert (decision.allowed, decision.source, decision.family) == (
        False, ps.SOURCE_UNKNOWN_IDENTIFIER, ps.UNKNOWN_FAMILY)
    assert "not in the provider_switch host map" in decision.error
    with pytest.raises(ps.ProviderDisabledError) as exc:
        ps.require_host(url, consumer="harness", project_id="p-x")
    assert exc.value.decision.source == ps.SOURCE_UNKNOWN_IDENTIFIER


def test_a_host_decision_follows_the_family_switch(switch_file):
    write_doc(switch_file, {"version": 1, "global": {"glm": entry(False, reason="proxy weg")}})
    refused = ps.decide_host("http://127.0.0.1:4141/v1", consumer="harness", project_id="p-x")
    assert (refused.allowed, refused.family, refused.source, refused.reason) == (False, "glm", ps.SOURCE_GLOBAL, "proxy weg")
    assert refused.identifier == "127.0.0.1:4141"
    allowed = ps.require_host("https://openrouter.ai/api/v1", model="meta-llama/llama-4", consumer="h", project_id="p-x")
    assert (allowed.allowed, allowed.family, allowed.identifier) == (True, "openrouter", "openrouter.ai")
    assert ps.decide_host("https://api.anthropic.com", consumer="h", project_id="p-x").source == ps.SOURCE_NOT_SWITCHABLE


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("family", FAMILIES)
def test_precedence_per_family(family, switch_file, no_project, monkeypatch):
    d = ps.decide(family, consumer="t", project_id="proj-a")
    assert (d.allowed, d.source, d.scope) == (True, ps.SOURCE_DEFAULT, "proj-a")

    write_doc(switch_file, {"version": 1, "global": {family: entry(False, reason="global off")}})
    d = ps.decide(family, consumer="t", project_id="proj-a")
    assert (d.allowed, d.source, d.reason) == (False, ps.SOURCE_GLOBAL, "global off")

    write_doc(switch_file, {"version": 1, "global": {family: entry(False)},
                            "projects": {"proj-a": {family: entry(True, reason="project on")}}})
    d = ps.decide(family, consumer="t", project_id="proj-a")
    assert (d.allowed, d.source, d.scope, d.reason) == (True, ps.SOURCE_PROJECT, "proj-a", "project on")
    other = ps.decide(family, consumer="t", project_id="proj-b")
    assert (other.allowed, other.source) == (False, ps.SOURCE_GLOBAL)

    write_doc(switch_file, {"version": 1, "global": {family: entry(True)},
                            "projects": {"proj-a": {family: entry(False)}}})
    assert ps.decide(family, consumer="t", project_id="proj-a").allowed is False
    assert ps.decide(family, consumer="t", project_id="proj-b").allowed is True

    monkeypatch.setenv(f"VNX_OVERRIDE_PROVIDER_{family.upper()}", "off")
    write_doc(switch_file, {"version": 1, "projects": {"proj-a": {family: entry(True)}}})
    d = ps.decide(family, consumer="t", project_id="proj-a")
    assert (d.allowed, d.source) == (False, ps.SOURCE_OVERRIDE)


@pytest.mark.parametrize("token", ["off", "0", "false", "no", " OFF ", "No", "FALSE"])
def test_override_off_tokens_win(token, monkeypatch, switch_file, caplog):
    write_doc(switch_file, {"version": 1, "global": {"deepseek": entry(True)}})
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_DEEPSEEK", token)
    with caplog.at_level(logging.WARNING, logger="provider_switch"):
        d = ps.decide("deepseek-harness", consumer="tagger", project_id="p-x")
    assert (d.allowed, d.source, d.family) == (False, ps.SOURCE_OVERRIDE, "deepseek")
    warning = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning and "tagger" in warning[0].getMessage() and f"pid {os.getpid()}" in warning[0].getMessage()


@pytest.mark.parametrize("token", ["on", "1", "true", "yes", " YES ", "On"])
def test_override_on_tokens_are_ignored_with_a_warning(token, monkeypatch, switch_file, caplog):
    write_doc(switch_file, {"version": 1, "global": {"glm": entry(False, reason="operator off")}})
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_GLM", token)
    with caplog.at_level(logging.WARNING, logger="provider_switch"):
        d = ps.decide("glm-harness", consumer="panel", project_id="p-x")
    assert (d.allowed, d.source, d.reason) == (False, ps.SOURCE_GLOBAL, "operator off")
    message = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "ignored" in message and "vnx providers on glm" in message and "panel" in message


@pytest.mark.parametrize("token", ["of", "", "maybe", "offf", "2"])
def test_override_invalid_token_refuses(token, monkeypatch, switch_file):
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_KIMI", token)
    d = ps.decide("kimi", consumer="t", project_id="p-x")
    assert (d.allowed, d.source) == (False, ps.SOURCE_OVERRIDE_INVALID)
    assert "VNX_OVERRIDE_PROVIDER_KIMI" in d.error


def test_plain_environment_variable_is_not_a_source(monkeypatch, switch_file):
    monkeypatch.setenv("VNX_PROVIDER_DEEPSEEK", "off")
    monkeypatch.setenv("DEEPSEEK_ENABLED", "0")
    assert ps.decide("deepseek", consumer="t", project_id="p-x").source == ps.SOURCE_DEFAULT


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

@pytest.fixture
def project_tree(tmp_path):
    root = tmp_path / "resolved-project"
    (root / ".vnx-data" / "state").mkdir(parents=True)
    (root / ".vnx-project-id").write_text("proj-resolved\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("env_pid", [None, "proj-env"])
def test_project_resolution_order(env_pid, monkeypatch, project_tree, switch_file):
    import vnx_paths
    write_doc(switch_file, {"version": 1, "global": {"kimi": entry(True)}, "projects": {
        "proj-explicit": {"kimi": entry(False, reason="explicit")},
        "proj-env": {"kimi": entry(False, reason="env")},
        "proj-resolved": {"kimi": entry(False, reason="resolver")},
    }})
    if env_pid:
        monkeypatch.setenv("VNX_PROJECT_ID", env_pid)
    else:
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(vnx_paths, "resolve_paths",
                        lambda: {"VNX_STATE_DIR": str(project_tree / ".vnx-data" / "state")})

    # 1. the explicit argument wins over everything
    d = ps.decide("kimi", consumer="t", project_id="proj-explicit")
    assert (d.scope, d.reason) == ("proj-explicit", "explicit")
    # 2. VNX_PROJECT_ID, else 3. the state-dir resolver of the process
    d = ps.decide("kimi", consumer="t")
    expected = ("proj-env", "env") if env_pid else ("proj-resolved", "resolver")
    assert (d.scope, d.reason) == expected


def test_no_project_uses_the_global_section_only(monkeypatch, switch_file, no_project):
    write_doc(switch_file, {"version": 1, "global": {"gemini": entry(True, reason="global")},
                            "projects": {"some-proj": {"gemini": entry(False)}}})
    d = ps.decide("gemini", consumer="t")
    assert (d.allowed, d.scope, d.source, d.reason) == (True, "global", ps.SOURCE_GLOBAL, "global")


def test_resolver_failure_falls_back_to_global(monkeypatch, switch_file, caplog):
    import vnx_paths
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    def broken():
        raise RuntimeError("data-dir guard refused")

    monkeypatch.setattr(vnx_paths, "resolve_paths", broken)
    with caplog.at_level(logging.WARNING, logger="provider_switch"):
        d = ps.decide("gemini", consumer="t")
    assert d.scope == "global" and d.allowed
    assert "data-dir guard refused" in caplog.text


# ---------------------------------------------------------------------------
# Fail-closed sources and non-switchable classes
# ---------------------------------------------------------------------------

def test_missing_file_means_everything_on(switch_file):
    assert not switch_file.exists()
    for family in FAMILIES:
        d = ps.decide(family, consumer="t", project_id="p-x")
        assert (d.allowed, d.source) == (True, ps.SOURCE_DEFAULT)


UNREADABLE_DOCS = {
    "invalid json": "{not json",
    "empty file": "",
    "top level list": [1, 2],
    "missing version": {"global": {}},
    "string version": {"version": "1"},
    "bool version": {"version": True},
    "float version": {"version": 1.0},
    "higher major": {"version": 2, "global": {}},
    "version zero": {"version": 0},
    "global not object": {"version": 1, "global": []},
    "projects not object": {"version": 1, "projects": ["a"]},
    "project section not object": {"version": 1, "projects": {"p-x": "off"}},
    "enabled not bool": {"version": 1, "global": {"deepseek": {"enabled": "false"}}},
    "enabled missing": {"version": 1, "global": {"kimi": {"reason": "x"}}},
    "known family not object": {"version": 1, "projects": {"p-x": {"glm": False}}},
}


@pytest.mark.parametrize("name", sorted(UNREADABLE_DOCS))
def test_unreadable_file_refuses_every_switchable_family(name, switch_file):
    write_doc(switch_file, UNREADABLE_DOCS[name])
    state = ps.load_state()
    assert not state.readable and state.error
    for family in FAMILIES:
        d = ps.decide(family, consumer="t", project_id="p-x")
        assert (d.allowed, d.source) == (False, ps.SOURCE_UNREADABLE)
        assert d.error == state.error
    for identifier in ("claude", "codex", "gh", "auto", "sonnet", "gpt-5.5"):
        assert ps.decide(identifier, consumer="t").allowed


def test_unreadable_when_the_path_is_a_directory(switch_file):
    switch_file.mkdir()
    d = ps.decide("deepseek", consumer="t", project_id="p-x")
    assert (d.allowed, d.source) == (False, ps.SOURCE_UNREADABLE)


def test_unknown_identifier_is_refused_and_logged(switch_file, caplog):
    with caplog.at_level(logging.ERROR, logger="provider_switch"):
        d = ps.decide("mistral-large", consumer="router", project_id="p-x")
    assert (d.allowed, d.source, d.family) == (False, ps.SOURCE_UNKNOWN_IDENTIFIER, ps.UNKNOWN_FAMILY)
    assert "mistral-large" in caplog.text and "router" in caplog.text


@pytest.mark.parametrize("identifier", [None, 42, ""])
def test_decide_never_raises(identifier, switch_file):
    d = ps.decide(identifier, consumer="t", project_id="p-x")
    assert d.allowed is False and d.source == ps.SOURCE_UNKNOWN_IDENTIFIER


@pytest.mark.parametrize("identifier", ["claude", "sonnet", "opus-5-5", "codex", "gpt-5.5", "gh", "ci_gate", "auto"])
def test_non_switchable_classes_are_always_allowed(identifier, switch_file, monkeypatch):
    write_doc(switch_file, {"version": 1, "global": {"claude": {"enabled": False}, "codex": {"enabled": False}}})
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_CLAUDE", "off")
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_CODEX", "off")
    d = ps.decide(identifier, consumer="t", project_id="p-x")
    assert (d.allowed, d.source) == (True, ps.SOURCE_NOT_SWITCHABLE)
    write_doc(switch_file, "{broken")
    assert ps.decide(identifier, consumer="t", project_id="p-x").allowed


def test_require_raises_with_the_decision(switch_file):
    write_doc(switch_file, {"version": 1, "global": {"deepseek": entry(False, reason="tegoed sparen")}})
    with pytest.raises(ps.ProviderDisabledError) as exc:
        ps.require("deepseek_gate", consumer="review", project_id="p-x")
    decision = exc.value.decision
    assert (decision.allowed, decision.family, decision.identifier) == (False, "deepseek", "deepseek_gate")
    detail = decision.detail()
    assert "\n" not in detail and ps.REASON_SWITCHED_OFF in detail and "tegoed sparen" in detail
    assert str(exc.value) == detail
    assert ps.require("kimi", consumer="review", project_id="p-x").allowed


# ---------------------------------------------------------------------------
# Version policy
# ---------------------------------------------------------------------------

def test_newer_cli_file_with_extra_family_and_fields_is_read(switch_file, caplog, monkeypatch):
    monkeypatch.setattr(ps, "_LOGGED_IGNORED_FAMILIES", set())
    write_doc(switch_file, {
        "version": 1, "written_by": "vnx 9.9",
        "global": {"mistral": {"enabled": False, "tier": "x"}, "deepseek": entry(False, colour="red")},
        "projects": {"p-x": {"mistral": {"enabled": "not even bool"}, "glm": entry(False)}},
    })
    with caplog.at_level(logging.INFO, logger="provider_switch"):
        assert ps.load_state().readable
        assert ps.decide("deepseek", consumer="t", project_id="p-x").allowed is False
        assert ps.decide("glm", consumer="t", project_id="p-x").allowed is False
        assert ps.decide("kimi", consumer="t", project_id="p-x").allowed is True
        ps.load_state()
    logged = [r for r in caplog.records if "unknown family 'mistral'" in r.getMessage()]
    assert len(logged) == 1 and logged[0].levelno == logging.INFO


def test_higher_major_is_unreadable(switch_file):
    write_doc(switch_file, {"version": 2, "global": {"deepseek": entry(True)}})
    d = ps.decide("deepseek", consumer="t", project_id="p-x")
    assert (d.allowed, d.source) == (False, ps.SOURCE_UNREADABLE)
    assert "newer than this reader" in d.error


def test_a_write_preserves_unknown_families_and_fields(switch_file, writable):
    original = {
        "version": 1, "written_by": "vnx 9.9",
        "global": {"mistral": {"enabled": False, "tier": "x"}, "deepseek": entry(True, colour="red")},
        "projects": {"p-x": {"mistral": {"enabled": True}}},
    }
    write_doc(switch_file, original)
    ps.set_switch("deepseek", False, scope="global", actor="op", reason="sparen")
    ps.set_switch("glm", False, scope="p-x", actor="op", reason="")
    after = json.loads(switch_file.read_text(encoding="utf-8"))
    assert after["written_by"] == "vnx 9.9"
    assert after["global"]["mistral"] == {"enabled": False, "tier": "x"}
    assert after["projects"]["p-x"]["mistral"] == {"enabled": True}
    assert after["global"]["deepseek"]["colour"] == "red"
    assert after["global"]["deepseek"]["enabled"] is False
    assert after["projects"]["p-x"]["glm"]["enabled"] is False
    assert after["version"] == 1


# ---------------------------------------------------------------------------
# Identifier inventory (Risks: "Unknown identifiers")
# ---------------------------------------------------------------------------

def _parse(rel: str) -> ast.Module:
    return ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))


def _assigned(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return node.value
    raise AssertionError(f"{name} not found")


def _strings(node: ast.AST) -> Set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _dispatch_spec_providers() -> Set[str]:
    tree = _parse("scripts/lib/dispatch_spec.py")
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Provider")
    return {n.value.value for n in cls.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)}


def _gate_providers() -> Set[str]:
    value = _assigned(_parse("scripts/lib/gate_recorder.py"), "GATE_PROVIDERS")
    names = {k.value for k in value.keys}
    binaries = {v.elts[1].value for v in value.values}
    return names | binaries


def _classifier_keys() -> Set[str]:
    tree = _parse("scripts/lib/classifier_providers/base.py")
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_provider")
    keys = {c.comparators[0].value for c in ast.walk(func)
            if isinstance(c, ast.Compare) and isinstance(c.comparators[0], ast.Constant)}
    return keys | {"haiku"}


def _plan_gate_identifiers() -> Set[str]:
    config = yaml.safe_load((REPO_ROOT / "configs" / "plan_gate_panel.yaml").read_text(encoding="utf-8"))
    found = {s["provider"] for s in config["seats"]} | {s["model_arg"] for s in config["seats"]}
    found.discard("registry-default")  # codex seat: resolved from openai.default_model at build time
    found |= {config["tiebreaker"]["provider"], config["tiebreaker"]["model"]}
    default_panel = _assigned(_parse("scripts/lib/plan_gate_panel.py"), "_DEFAULT_PANEL_SEATS")
    for seat in default_panel.elts:
        values = {k.value: v.value for k, v in zip(seat.keys, seat.values) if isinstance(v, ast.Constant)}
        found |= {values["provider"], values.get("model_arg", values["provider"])}
    return found


def _deliberation_roster() -> Set[str]:
    return _strings(_assigned(_parse("scripts/lib/deliberation_panel.py"), "_DEFAULT_ROSTER_ENTRIES"))


def _lane_checks() -> Set[str]:
    value = _assigned(_parse("scripts/lib/providers/smart_router/availability.py"), "_LANE_CHECKS")
    return {k.value for k in value.keys}


def _wave7_identifiers() -> Set[str]:
    registry = yaml.safe_load((REPO_ROOT / "scripts/lib/providers/wave7_models.yaml").read_text(encoding="utf-8"))
    found: Set[str] = set()
    for section, body in registry["providers"].items():
        found.add(section)
        if body.get("dispatch_enum"):
            found.add(body["dispatch_enum"])
        for model_id, model in (body.get("models") or {}).items():
            found.add(model_id)
            if isinstance(model, dict):
                if model.get("litellm_name"):
                    found.add(f"litellm:{model['litellm_name']}")
                if model.get("cli_model_arg"):
                    found.add(model["cli_model_arg"])
    for route in registry["routing"]["tier_map"].values():
        found |= {route["provider"], route["model"]}
        for fallback in route.get("fallback", []) or []:
            found |= {fallback["provider"], fallback["model"]}
    return found


@pytest.mark.parametrize("source,reader,minimum", [
    ("dispatch_spec.Provider", _dispatch_spec_providers, 11),
    ("gate_recorder.GATE_PROVIDERS", _gate_providers, 10),
    ("classifier_providers.get_provider", _classifier_keys, 5),
    ("plan-gate seats + tiebreaker", _plan_gate_identifiers, 10),
    ("deliberation roster", _deliberation_roster, 9),
    ("availability._LANE_CHECKS", _lane_checks, 5),
    ("wave7_models.yaml", _wave7_identifiers, 40),
])
def test_every_routed_identifier_maps_to_a_family(source, reader, minimum):
    identifiers = reader()
    assert len(identifiers) >= minimum, f"{source}: read only {sorted(identifiers)}"
    unmapped = []
    for identifier in sorted(identifiers):
        try:
            ps.family_of(identifier)
        except ps.UnknownProviderError:
            unmapped.append(identifier)
    assert unmapped == [], f"{source} routes on identifiers the switch does not know"


def test_inventory_readers_parse_instead_of_importing():
    """The readers parse source and yaml: none imports a module that could start a process."""
    import inspect
    readers = (_dispatch_spec_providers, _gate_providers, _classifier_keys, _plan_gate_identifiers,
               _deliberation_roster, _lane_checks, _wave7_identifiers)
    for reader in readers:
        tree = ast.parse(textwrap.dedent(inspect.getsource(reader)))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert imports == [], reader.__name__


# ---------------------------------------------------------------------------
# Writing: audit, concurrency, refusals
# ---------------------------------------------------------------------------

def test_audit_line_content(switch_file, writable):
    first = ps.set_switch("deepseek", False, scope="global", actor="vincent", reason="tegoed sparen")
    second = ps.set_switch("deepseek", True, scope="global", actor="vincent", reason="weer aan")
    lines = audit_lines(switch_file)
    assert len(lines) == 2
    line = lines[0]
    assert set(line) >= {"id", "ts", "actor", "cwd", "ppid", "parent_cmd", "scope", "family", "old", "new", "reason",
                         "op", "argv"}
    assert isinstance(line["id"], str) and line["id"] and line["id"] != lines[1]["id"]
    assert line["op"] == "set" and line["actor"] == "vincent" and line["scope"] == "global"
    assert line["family"] == "deepseek" and line["old"] is None and line["reason"] == "tegoed sparen"
    assert line["new"]["enabled"] is False and line["new"]["changed_by"] == "vincent"
    assert line["cwd"] == str(writable) and line["ppid"] == os.getppid() and line["pid"] == os.getpid()
    assert isinstance(line["parent_cmd"], str) and line["parent_cmd"]
    assert line["ts"].endswith("Z")
    assert lines[1]["old"] == first.new and lines[1]["new"] == second.new


def test_clear_project_override(switch_file, writable, no_project):
    ps.set_switch("kimi", False, scope="global", actor="op", reason="")
    ps.set_switch("kimi", True, scope="proj-a", actor="op", reason="project on")
    assert ps.decide("kimi", consumer="t", project_id="proj-a").allowed
    result = ps.clear_project_override("kimi", "proj-a", actor="op", reason="back to global")
    assert result.changed and result.old["enabled"] is True
    d = ps.decide("kimi", consumer="t", project_id="proj-a")
    assert (d.allowed, d.source) == (False, ps.SOURCE_GLOBAL)
    assert "proj-a" not in json.loads(switch_file.read_text(encoding="utf-8"))["projects"]
    assert audit_lines(switch_file)[-1]["op"] == "clear"
    again = ps.clear_project_override("kimi", "proj-a", actor="op", reason="")
    assert not again.changed and len(audit_lines(switch_file)) == 3


@pytest.mark.parametrize("family", ["claude", "codex", "tooling", "unresolved", "mistral", ""])
def test_set_switch_refuses_non_switchable(family, writable):
    with pytest.raises(ps.NotSwitchableError):
        ps.set_switch(family, False, scope="global", actor="op", reason="")


@pytest.mark.parametrize("scope", ["Bad_Project", "x", "", "-lead", "a" * 40])
def test_set_switch_refuses_invalid_project(scope, writable, switch_file):
    with pytest.raises(ValueError):
        ps.set_switch("glm", False, scope=scope, actor="op", reason="")
    assert not switch_file.exists()


def test_project_id_global_is_reserved_for_the_global_scope():
    with pytest.raises(ValueError):
        ps.check_project_id("global")
    assert ps.check_project_id("global-tools") == "global-tools"


def test_set_switch_refuses_an_unreadable_file(switch_file, writable):
    write_doc(switch_file, "{broken")
    with pytest.raises(ps.SwitchFileUnreadable):
        ps.set_switch("glm", False, scope="global", actor="op", reason="")
    assert switch_file.read_text(encoding="utf-8") == "{broken"
    assert not ps.audit_log_path(switch_file).exists()


_WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    import provider_switch as ps
    scope, rounds = sys.argv[2], int(sys.argv[3])
    for i in range(rounds):
        for family in ps.SWITCHABLE_FAMILIES:
            ps.set_switch(family, i % 2 == 0, scope=scope, actor=scope, reason=f"round {i}")
""")


def test_atomic_write_under_a_concurrent_writer(switch_file, writable):
    env = {k: v for k, v in os.environ.items() if k not in ps.DISPATCH_MARKER_ENV}
    rounds = 15
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(REPO_ROOT / "scripts" / "lib"), scope, str(rounds)],
                         cwd=str(writable), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for scope in ("global", "proj-b")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    document = json.loads(switch_file.read_text(encoding="utf-8"))
    last_enabled = (rounds - 1) % 2 == 0
    for family in FAMILIES:
        assert document["global"][family]["enabled"] is last_enabled
        assert document["global"][family]["reason"] == f"round {rounds - 1}"
        assert document["projects"]["proj-b"][family]["enabled"] is last_enabled
    lines = audit_lines(switch_file)
    assert len(lines) == 2 * rounds * len(FAMILIES)
    assert sorted(p.name for p in switch_file.parent.iterdir()) == sorted(
        [switch_file.name, ps.lock_file_path(switch_file).name, ps.audit_log_path(switch_file).name]
    )
    for scope in ("global", "proj-b"):
        own = [line for line in lines if line["scope"] == scope]
        assert len(own) == rounds * len(FAMILIES)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def test_repair_rebuilds_from_the_audit_log(switch_file, writable):
    ps.set_switch("deepseek", False, scope="global", actor="op", reason="sparen")
    ps.set_switch("glm", False, scope="proj-a", actor="op", reason="")
    ps.set_switch("kimi", True, scope="proj-a", actor="op", reason="")
    ps.clear_project_override("kimi", "proj-a", actor="op", reason="")
    before = json.loads(switch_file.read_text(encoding="utf-8"))
    write_doc(switch_file, "{truncated by a crash")

    result = ps.repair(actor="op")
    assert result.quarantined_to.name.startswith("providers.json.corrupt-")
    assert result.quarantined_to.parent == switch_file.parent
    assert result.quarantined_to.read_text(encoding="utf-8") == "{truncated by a crash"
    rebuilt = json.loads(switch_file.read_text(encoding="utf-8"))
    assert rebuilt["global"] == before["global"] and rebuilt["projects"] == before["projects"]
    assert ps.decide("deepseek", consumer="t", project_id="proj-a").allowed is False
    repair_line = audit_lines(switch_file)[-1]
    assert repair_line["op"] == "repair" and repair_line["old"]["quarantined_to"] == result.quarantined_to.name
    assert repair_line["new"] == rebuilt

    with pytest.raises(ps.RepairNotNeeded):
        ps.repair(actor="op")

    ps.set_switch("gemini", False, scope="global", actor="op", reason="")
    write_doc(switch_file, {"version": 1, "global": {"deepseek": {"enabled": "no"}}})
    ps.repair(actor="op")
    replayed = json.loads(switch_file.read_text(encoding="utf-8"))
    assert replayed["global"]["gemini"]["enabled"] is False and replayed["global"]["deepseek"]["enabled"] is False


def test_repair_refuses_without_an_audit_log_unless_empty(switch_file, writable):
    write_doc(switch_file, "{broken")
    with pytest.raises(ps.AuditLogUnreadable):
        ps.repair(actor="op")
    assert switch_file.read_text(encoding="utf-8") == "{broken"
    result = ps.repair(actor="op", empty=True)
    assert json.loads(switch_file.read_text(encoding="utf-8")) == {"version": 1, "global": {}, "projects": {}}
    assert result.quarantined_to.exists()
    assert all(ps.decide(f, consumer="t", project_id="p-x").allowed for f in FAMILIES)
    assert audit_lines(switch_file)[-1]["op"] == "repair"


def test_repair_refuses_a_malformed_audit_line(switch_file, writable):
    ps.set_switch("deepseek", False, scope="global", actor="op", reason="")
    with ps.audit_log_path(switch_file).open("a", encoding="utf-8") as fh:
        fh.write('{"op": "set", "scope": "glob\n')
    write_doc(switch_file, "{broken")
    with pytest.raises(ps.AuditLogUnreadable, match="line 2"):
        ps.repair(actor="op")


def test_repair_refuses_a_missing_file(switch_file, writable):
    with pytest.raises(ps.RepairNotNeeded, match="missing"):
        ps.repair(actor="op")


def append_records(path: Path, *records) -> None:
    with ps.audit_log_path(path).open("a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(record) + "\n" for record in records)


def test_replay_accepts_a_log_written_before_ids(switch_file):
    append_records(
        switch_file,
        {"ts": "2026-10-12T08:00:00Z", "op": "set", "scope": "global", "family": "deepseek", "old": None,
         "new": entry(False), "reason": "r", "actor": "op"},
        {"op": "set", "scope": "proj-a", "family": "glm", "old": None, "new": entry(True)},
        {"op": "set", "scope": "proj-a", "family": "kimi", "old": None, "new": entry(False)},
        {"op": "clear", "scope": "proj-a", "family": "glm", "old": entry(True), "new": None},
    )
    expected = {"version": 1, "global": {"deepseek": entry(False)}, "projects": {"proj-a": {"kimi": entry(False)}}}
    assert ps.replay_audit(ps.audit_log_path(switch_file)) == expected

    # new records with ids, and an abort of one of them, replay on top of the old ones
    append_records(
        switch_file,
        {"id": "a1", "op": "set", "scope": "global", "family": "gemini", "old": None, "new": entry(False)},
        {"id": "a2", "op": "abort", "ref": "a1", "error": "OSError: disk full"},
    )
    assert ps.replay_audit(ps.audit_log_path(switch_file)) == expected


ABORT_REFS = {
    "an id no record has": ({"id": "s1", "op": "set", "scope": "global", "family": "glm", "new": entry(False)},
                            {"id": "x1", "op": "abort", "ref": "missing", "error": "e"}),
    "a later record": ({"id": "x1", "op": "abort", "ref": "s1", "error": "e"},
                       {"id": "s1", "op": "set", "scope": "global", "family": "glm", "new": entry(False)}),
    "another abort": ({"id": "s1", "op": "set", "scope": "global", "family": "glm", "new": entry(False)},
                      {"id": "x1", "op": "abort", "ref": "s1", "error": "e"},
                      {"id": "x2", "op": "abort", "ref": "x1", "error": "e"}),
    "no ref": ({"id": "s1", "op": "set", "scope": "global", "family": "glm", "new": entry(False)},
               {"id": "x1", "op": "abort", "error": "e"}),
    "a record without id": ({"op": "set", "scope": "global", "family": "glm", "new": entry(False)},
                            {"id": "x1", "op": "abort", "ref": None, "error": "e"}),
}


@pytest.mark.parametrize("name", sorted(ABORT_REFS))
def test_an_abort_with_an_unknown_ref_makes_the_log_unreadable(name, switch_file, writable):
    append_records(switch_file, *ABORT_REFS[name])
    write_doc(switch_file, "{broken")
    with pytest.raises(ps.AuditLogUnreadable, match="unrecognised record"):
        ps.repair(actor="op")
    assert switch_file.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("second_id", ["s1", 7])
def test_a_repeated_or_non_string_id_makes_the_log_unreadable(second_id, switch_file):
    append_records(switch_file,
                   {"id": "s1", "op": "set", "scope": "global", "family": "glm", "new": entry(False)},
                   {"id": second_id, "op": "set", "scope": "global", "family": "kimi", "new": entry(False)})
    with pytest.raises(ps.AuditLogUnreadable, match="line 2: id"):
        ps.replay_audit(ps.audit_log_path(switch_file))


# ---------------------------------------------------------------------------
# Write protection
# ---------------------------------------------------------------------------

def _git(*args, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def linked_worktree(tmp_path) -> dict:
    main = tmp_path / "tmp-repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    linked = tmp_path / "tmp-repo-linked"
    _git("worktree", "add", "-q", str(linked), cwd=main)
    (linked / "sub").mkdir()
    return {"main": main, "linked": linked}


def test_write_refused_in_a_linked_worktree(linked_worktree, writable, monkeypatch, switch_file):
    assert not str(Path(ps.__file__).resolve()).startswith(str(linked_worktree["linked"]))
    monkeypatch.chdir(linked_worktree["linked"] / "sub")
    with pytest.raises(ps.SwitchWriteRefused) as exc:
        ps.set_switch("glm", False, scope="global", actor="op", reason="")
    assert any("linked git worktree" in r and str(linked_worktree["linked"].resolve()) in r for r in exc.value.reasons)
    assert not switch_file.exists()
    monkeypatch.chdir(linked_worktree["main"])
    assert ps.set_switch("glm", False, scope="global", actor="op", reason="").changed


@pytest.mark.parametrize("marker", ps.DISPATCH_MARKER_ENV)
def test_write_refused_with_a_dispatch_marker(marker, writable, monkeypatch, switch_file):
    monkeypatch.setenv(marker, "20261012-some-dispatch")
    for call in (lambda: ps.set_switch("glm", False, scope="global", actor="op", reason=""),
                 lambda: ps.clear_project_override("glm", "proj-a", actor="op", reason=""),
                 lambda: ps.repair(actor="op", empty=True)):
        with pytest.raises(ps.SwitchWriteRefused) as exc:
            call()
        assert any(marker in r for r in exc.value.reasons)
    assert not switch_file.exists()


_ONE_PENDING = {"B": ("scripts/lib/example.py:launch",)}


def test_the_write_verbs_are_live_now_nothing_is_pending(writable, switch_file):
    """Provider-schakelaar B: every launch function calls require, so a caller outside any
    worktree and without a dispatch marker may write."""
    assert ps.pending_launch_functions() == {}
    assert ps.write_refusals() == []
    assert ps.set_switch("deepseek", False, scope="global", actor="op", reason="sparen").changed
    assert ps.load_state(switch_file).global_switches["deepseek"].enabled is False


def test_write_refused_while_launch_functions_are_pending(monkeypatch, tmp_path, switch_file):
    """The pending mechanism stays until F removes it: a pending entry still refuses."""
    for marker in ps.DISPATCH_MARKER_ENV:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ps, "pending_launch_functions", lambda: _ONE_PENDING)
    with pytest.raises(ps.SwitchWriteRefused) as exc:
        ps.set_switch("deepseek", False, scope="global", actor="op", reason="")
    reasons = " ".join(exc.value.reasons)
    assert "not enforced yet" in reasons and "B: 1" in reasons
    assert not switch_file.exists()


def test_no_environment_variable_bypasses_the_pending_refusal(monkeypatch, tmp_path):
    for marker in ps.DISPATCH_MARKER_ENV:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ps, "pending_launch_functions", lambda: _ONE_PENDING)
    for name in ("VNX_PROVIDER_SWITCH_FORCE", "VNX_OVERRIDE_PROVIDER_SWITCH", "VNX_PROVIDERS_ENFORCED"):
        monkeypatch.setenv(name, "1")
    assert any("not enforced yet" in r for r in ps.write_refusals())


def test_linked_worktree_detection_by_git_marker(tmp_path):
    main = tmp_path / "main-checkout"
    (main / ".git").mkdir(parents=True)
    (main / "deeper").mkdir()
    assert ps.linked_worktree_root(main / "deeper") is None
    submodule = tmp_path / "submodule"
    submodule.mkdir()
    (submodule / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
    assert ps.linked_worktree_root(submodule) is None
    linked = tmp_path / "linked"
    (linked / "deeper").mkdir(parents=True)
    (linked / ".git").write_text("gitdir: /x/repo/.git/worktrees/linked\n", encoding="utf-8")
    assert ps.linked_worktree_root(linked / "deeper") == linked
    relative = tmp_path / "relative"
    relative.mkdir()
    (relative / ".git").write_text("gitdir: ../repo/.git/worktrees/relative\n", encoding="utf-8")
    assert ps.linked_worktree_root(relative) == relative


def test_an_unreadable_git_marker_counts_as_linked(tmp_path):
    odd = tmp_path / "odd"
    odd.mkdir()
    marker = odd / ".git"
    marker.write_text("gitdir: somewhere\n", encoding="utf-8")
    marker.chmod(0)
    try:
        assert ps.linked_worktree_root(odd) == odd
    finally:
        marker.chmod(0o644)


# ---------------------------------------------------------------------------
# Test isolation (conftest)
# ---------------------------------------------------------------------------

def test_conftest_isolates_switch_audit_lock_and_quarantine(tmp_path):
    import conftest
    real_vnx = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".vnx"
    path = ps.switch_file_path()
    paths = (path, ps.audit_log_path(path), ps.lock_file_path(path), ps.quarantine_dir(path))
    for p in paths:
        assert tmp_path in p.parents, p
        assert p != real_vnx and real_vnx not in p.parents, p
    floor = Path(conftest._PROVIDER_SWITCH_ISOLATION_FILE)
    assert Path(conftest._CONFTEST_ISOLATION_TMP) in floor.parents
    assert real_vnx not in floor.parents
    assert not any(k.startswith("VNX_OVERRIDE_PROVIDER_") for k in os.environ)
