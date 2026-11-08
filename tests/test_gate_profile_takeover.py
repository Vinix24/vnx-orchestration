"""Gate profile P3 (track gate-profiel-per-project): the takeover chain comes from the profile.

Unset and ``standard`` give today's chain from the source ``default``; ``light`` gives three
names without ``deepseek_gate`` from the source ``profile``; an explicit chain (override or
``project_config`` row) wins; a malformed profile raises ``GateProfileConfigError``.
"""
from __future__ import annotations

import logging

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import config_registry
import gate_request_handler as grh
from gate_profile import GateProfileConfigError

TODAY = {"codex_gate": "kimi_gate", "kimi_gate": "claude_gate", "claude_gate": "deepseek_gate"}
LIGHT = {"codex_gate": "kimi_gate", "kimi_gate": "claude_gate"}


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def project_config(monkeypatch):
    rows = {}
    config_registry.set_db_resolver(lambda _pid, key: rows.get(key))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    return rows


def test_unset_is_todays_chain_from_default():
    assert grh._build_review_gate_takeover_chain() == TODAY
    assert grh.review_gate_takeover_chain_raw() == (grh._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN, "default")


def test_standard_is_todays_chain(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "standard")
    assert grh._build_review_gate_takeover_chain() == TODAY
    assert grh.review_gate_takeover_chain_raw()[1] == "default"


def test_standard_leaves_an_env_chain_alone(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "standard")
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate")
    assert grh.review_gate_takeover_chain_raw() == ("codex_gate,kimi_gate", "env")


def test_light_is_three_names_without_deepseek_from_the_profile(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    raw, source = grh.review_gate_takeover_chain_raw()
    assert (raw, source) == ("codex_gate,kimi_gate,claude_gate", "profile")
    assert "deepseek_gate" not in raw
    assert grh._build_review_gate_takeover_chain() == LIGHT


def test_heavy_chain_equals_today_but_names_the_profile(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    assert grh.review_gate_takeover_chain_raw() == (grh._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN, "profile")
    assert grh._build_review_gate_takeover_chain() == TODAY


def test_the_log_line_carries_the_source(monkeypatch, caplog):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    with caplog.at_level(logging.INFO, logger=grh.logger.name):
        grh._build_review_gate_takeover_chain()
    assert "bron=profile" in caplog.text


def test_a_project_config_row_wins_over_the_profile(monkeypatch, project_config):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    project_config["VNX_REVIEW_GATE_TAKEOVER_CHAIN"] = "codex_gate,deepseek_gate"
    assert grh.review_gate_takeover_chain_raw() == ("codex_gate,deepseek_gate", "project_config")
    assert grh._build_review_gate_takeover_chain() == {"codex_gate": "deepseek_gate"}


def test_the_override_wins_over_the_profile(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate")
    assert grh.review_gate_takeover_chain_raw() == ("codex_gate", "override")
    assert grh._build_review_gate_takeover_chain() == {}


def test_an_unknown_gate_in_an_explicit_chain_still_raises(monkeypatch):
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,nope_gate")
    with pytest.raises(grh.ReviewGateTakeoverConfigError):
        grh._build_review_gate_takeover_chain()


@pytest.mark.parametrize("bad", ["zwaar", "Light", "high", "light heavy"])
def test_a_malformed_profile_raises_in_both_readers(monkeypatch, bad):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", bad)
    with pytest.raises(GateProfileConfigError):
        grh.review_gate_takeover_chain_raw()
    with pytest.raises(GateProfileConfigError):
        grh._build_review_gate_takeover_chain()
    import review_rounds

    with pytest.raises(GateProfileConfigError):
        review_rounds.round_cap()


def test_providers_cli_reports_cap_and_chain_with_their_source(monkeypatch):
    import providers_cli

    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    review = providers_cli.review_settings(None)
    assert (review["round_cap"], review["round_cap_source"]) == (2, "profile")
    assert (review["takeover_chain"], review["takeover_chain_source"]) == (
        "codex_gate,kimi_gate,claude_gate", "profile")
    monkeypatch.delenv("VNX_GOVERNANCE_PROFILE")
    review = providers_cli.review_settings(None)
    assert (review["round_cap"], review["round_cap_source"], review["takeover_chain_source"]) == (3, "default", "default")


def test_providers_cli_names_a_malformed_profile_instead_of_hiding_it(monkeypatch):
    import providers_cli

    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "zwaar")
    assert "zwaar" in providers_cli.review_settings(None)["error"]


def test_review_settings_reports_the_chain_of_the_named_project(monkeypatch):
    import providers_cli

    rows = {("proj-b", "VNX_GOVERNANCE_PROFILE"): "light"}
    config_registry.set_db_resolver(lambda pid, key: rows.get((pid, key)))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    review = providers_cli.review_settings("proj-b")
    assert review["profile"] == "light"
    assert (review["takeover_chain"], review["takeover_chain_source"]) == (
        "codex_gate,kimi_gate,claude_gate", "profile")
    other = providers_cli.review_settings("proj-a")
    assert (other["takeover_chain"], other["takeover_chain_source"]) == (
        grh._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN, "default")


def test_the_raw_chain_resolves_an_operator_row_for_the_named_project(monkeypatch):
    rows = {("proj-b", "VNX_REVIEW_GATE_TAKEOVER_CHAIN"): "codex_gate,deepseek_gate"}
    config_registry.set_db_resolver(lambda pid, key: rows.get((pid, key)))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    assert grh.review_gate_takeover_chain_raw("proj-b") == ("codex_gate,deepseek_gate", "project_config")
    assert grh.review_gate_takeover_chain_raw("proj-a")[1] == "default"
