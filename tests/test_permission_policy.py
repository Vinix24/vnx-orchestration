"""Tests for permission_policy.py: the boundary object, its digest and its refusals."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from permission_policy import (
    POLICY_VERSION,
    PermissionPolicy,
    PermissionPolicyRefused,
    check_profile_entry,
    compose_policy,
    load_isolation,
    load_role_profiles,
)
from worker_permissions import UnknownRoleError, resolve_worker_profile

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"allowed_tools": ["Write", "Read", "Read"], "denied_tools": ["WebFetch"],
          "file_write_scope": ["b/**", "a/**"], "mcp_servers": []}


def _compose(fields=None, isolation=None, **kwargs):
    kwargs.setdefault("is_fallback", False)
    return compose_policy(FIELDS if fields is None else fields, isolation, **kwargs)


def test_lists_are_sorted_and_deduplicated_so_equal_boundaries_compare_equal():
    a = _compose()
    b = _compose({**FIELDS, "allowed_tools": ["Read", "Write"],
                  "file_write_scope": ["a/**", "b/**"]})
    assert a.tools.allow == ("Read", "Write") and a.filesystem.write == ("a/**", "b/**")
    assert a == b and a.policy_digest == b.policy_digest


def test_sources_do_not_feed_the_digest_but_version_does():
    fallback = _compose(is_fallback=True)
    assert fallback.sources["role_profile"] == {"state": "fallback"}
    assert fallback.policy_digest == _compose().policy_digest
    bumped = PermissionPolicy(**{**fallback.__dict__, "version": POLICY_VERSION + 1})
    assert bumped.policy_digest != fallback.policy_digest


def test_network_follows_web_tools_and_mcp_deny_follows_servers():
    assert _compose().network == "deny"
    assert _compose({**FIELDS, "allowed_tools": ["WebFetch(domain:x)"],
                     "denied_tools": []}).network == "allow"
    assert _compose(mcp_namespace_deny="mcp__*").mcp.deny == ("mcp__*",)
    assert _compose({**FIELDS, "mcp_servers": ["s"]}, mcp_namespace_deny="mcp__*").mcp.deny == ()


def test_unknown_field_refuses_compose():
    with pytest.raises(PermissionPolicyRefused) as info:
        _compose({**FIELDS, "surprise": []})
    assert info.value.reason == "unknown-source-key"


def test_digest_failure_refuses_and_returns_no_digest():
    policy = _compose({**FIELDS, "file_write_scope": ["\ud800"]})
    with pytest.raises(PermissionPolicyRefused) as info:
        policy.policy_digest
    assert info.value.reason == "digest-failed"


@pytest.mark.parametrize("raw, reason", [
    ("text", "source-malformed"), ({"allowed_tools": "Read"}, "source-malformed"),
    ({"allowed_tools": [1]}, "source-malformed"), ({"nope": []}, "unknown-source-key"),
])
def test_check_profile_entry_refuses_malformed_or_unknown(raw, reason):
    with pytest.raises(PermissionPolicyRefused) as info:
        check_profile_entry("r", raw)
    assert info.value.reason == reason


@pytest.mark.parametrize("text, reason", [
    ("", "source-empty"), ("- a", "source-malformed"), ("profiles: [", "source-broken-yaml"),
    ("profiles: [a]", "source-malformed"),
])
def test_load_role_profiles_refuses_bad_sources(tmp_path, text, reason):
    path = tmp_path / "p.yaml"
    path.write_text(text)
    with pytest.raises(PermissionPolicyRefused) as info:
        load_role_profiles(path)
    assert info.value.reason == reason


def test_load_role_profiles_refuses_missing_and_unreadable(tmp_path):
    with pytest.raises(PermissionPolicyRefused) as missing:
        load_role_profiles(tmp_path / "absent.yaml")
    assert missing.value.reason == "source-missing"
    with pytest.raises(PermissionPolicyRefused) as unreadable:
        load_role_profiles(tmp_path)
    assert unreadable.value.reason == "source-unreadable"


def _agents(tmp_path, role, config):
    (tmp_path / role).mkdir()
    (tmp_path / role / "config.yaml").write_text(config)
    return tmp_path


def test_load_isolation_absent_empty_and_present(tmp_path):
    assert load_isolation("r", None) is None
    assert load_isolation("r", tmp_path) is None
    assert load_isolation("r", _agents(tmp_path, "r", "provider: x\n")) is None
    assert load_isolation("e", _agents(tmp_path, "e", "isolation: {}\n")) == {}
    assert load_isolation("p", _agents(tmp_path, "p", "isolation: {allowed_paths: [x]}\n")) == {
        "allowed_paths": ["x"]}


@pytest.mark.parametrize("config, reason", [
    ("isolation: []\n", "source-malformed"), ("isolation:\n", "source-malformed"),
    ("isolation: {extra: 1}\n", "unknown-source-key"),
    ("isolation: {allowed_paths: x}\n", "source-malformed"),
    ("isolation: {scope_type: [1]}\n", "source-malformed"), ("isolation: {a: [", "source-broken-yaml"),
])
def test_load_isolation_refuses_bad_blocks(tmp_path, config, reason):
    with pytest.raises(PermissionPolicyRefused) as info:
        load_isolation("r", _agents(tmp_path, "r", config))
    assert info.value.reason == reason


def test_load_isolation_refuses_a_role_name_that_walks_out_of_agents(tmp_path):
    with pytest.raises(PermissionPolicyRefused) as info:
        load_isolation("../x", tmp_path)
    assert info.value.reason == "role-name-invalid"


def test_unknown_role_error_is_a_policy_refusal():
    assert issubclass(UnknownRoleError, PermissionPolicyRefused)
    assert UnknownRoleError.reason == "unknown-role"


def test_every_repo_profile_composes_with_a_digest():
    profiles = yaml.safe_load((REPO_ROOT / ".vnx" / "worker_permissions.yaml").read_text())["profiles"]
    digests = {}
    for role in profiles:
        profile = resolve_worker_profile(role, agents_dir=REPO_ROOT / "agents")
        assert profile.policy_digest and not profile.is_fallback
        digests[role] = profile.policy_digest
    assert len(digests) == len(profiles)


@pytest.mark.parametrize("config", ["", "[]\n", "5\n", "just a string\n", "- a\n- b\n"])
def test_load_isolation_refuses_a_non_mapping_document(tmp_path, config):
    with pytest.raises(PermissionPolicyRefused) as info:
        load_isolation("r", _agents(tmp_path, "r", config))
    assert info.value.reason == "source-malformed"


def test_load_isolation_a_mapping_without_isolation_is_absent(tmp_path):
    assert load_isolation("r", _agents(tmp_path, "r", "{}\n")) is None


@pytest.mark.parametrize("bad_key", [1, True, None, 1.5])
def test_non_string_keys_refuse_with_source_malformed_in_profile_and_isolation(tmp_path, bad_key):
    # Mixed key types used to raise TypeError inside sorted() (codex r3 blocker).
    entry = {bad_key: [], "unexpected": []}
    with pytest.raises(PermissionPolicyRefused) as info:
        check_profile_entry("code-reviewer", entry)
    assert info.value.reason == "source-malformed"
    assert type(bad_key).__name__ in str(info.value)
    with pytest.raises(PermissionPolicyRefused) as info:
        load_isolation("r", _agents(tmp_path, "r", yaml.safe_dump({"isolation": entry})))
    assert info.value.reason == "source-malformed"
    with pytest.raises(PermissionPolicyRefused) as info:
        _compose({bad_key: [], "unexpected": []})
    assert info.value.reason == "source-malformed"


def test_non_string_key_message_names_the_type_not_the_value():
    with pytest.raises(PermissionPolicyRefused) as info:
        check_profile_entry("r", {"secret-value-9": [], 7: []})
    assert "int" in str(info.value) and "7" not in str(info.value)


def test_load_isolation_directory_config_refuses_not_absent(tmp_path):
    """OI-2650: a config.yaml that is a directory is not 'no isolation'."""
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "config.yaml").mkdir()
    with pytest.raises(PermissionPolicyRefused) as info:
        load_isolation("r", tmp_path)
    assert info.value.reason == "source-malformed"


def test_load_isolation_dead_symlink_refuses_not_absent(tmp_path):
    """OI-2650: a dead symlink exists as a path; it is a broken source, not an absent one."""
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "config.yaml").symlink_to(tmp_path / "nowhere.yaml")
    with pytest.raises(PermissionPolicyRefused) as info:
        load_isolation("r", tmp_path)
    assert info.value.reason == "source-malformed"


@pytest.mark.parametrize("bad", ["Read", 5, None, [1], ["ok", None]])
def test_compose_refuses_non_string_list_values(bad):
    """OI-2651 pt.2: a role field that is not a list of strings raises the named refusal."""
    with pytest.raises(PermissionPolicyRefused) as info:
        _compose({**FIELDS, "allowed_tools": bad})
    assert info.value.reason == "source-malformed"


def test_historical_profile_falls_back_on_any_refusal(monkeypatch):
    """OI-2651 pt.4: replaying history survives a refused source, not only an unknown role."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "analysis"))
    import worker_scope_enforcement_measure as measure

    def refuse(role):
        raise PermissionPolicyRefused("broken", "source-malformed")

    monkeypatch.setattr(measure, "resolve_worker_profile", refuse)
    assert measure._historical_profile("any").file_write_scope is not None
