"""Gate profile P1 (track gate-profiel-per-project): one fail-closed read function, and the
review stack reads it.

Pinned here: unset and ``standard`` give today's seats and ``standard``/``default``/``None`` in the
record (the C6 lock); ``heavy`` gives three seats; every malformed value raises with the raw
value and its source; an explicit stack wins over every profile; an operator-set stack wins over
the profile; the ``standard`` column of the table equals today's registry defaults.
"""
from __future__ import annotations

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import config_registry
import gate_profile
from gate_profile import GateProfileConfigError
from review_seats import resolve_review_seats

TODAY = ("codex_gate", "kimi_gate")


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def project_config(monkeypatch):
    """A fake per-project layer (step 2), so a value can come from ``project_config``."""
    rows = {}
    config_registry.set_db_resolver(lambda _pid, key: rows.get(key))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    return rows


class TestNotSet:
    def test_unset_gives_todays_seats_and_the_c6_lock_in_the_record(self):
        seats = resolve_review_seats()
        assert seats.seats == TODAY
        assert (seats.profile, seats.profile_source, seats.profile_error) == ("standard", "default", None)
        assert "profile" not in seats.to_record()

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_is_not_set(self, monkeypatch, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        resolution = gate_profile.resolve()
        assert (resolution.profile, resolution.source, resolution.raw) == ("standard", "default", None)

    def test_a_resolver_returning_none_is_not_set(self, project_config):
        assert gate_profile.resolve().profile == "standard"

    def test_standard_gives_todays_seats_and_contributes_nothing(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "standard")
        resolution = gate_profile.resolve()
        assert (resolution.profile, resolution.source) == ("standard", "env")
        assert all(resolution.value(field) is None for field in gate_profile.FIELDS)
        seats = resolve_review_seats()
        assert seats.seats == TODAY
        assert seats.profile_source == "env"

    def test_standard_leaves_the_operator_stack_alone(self, monkeypatch):
        monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "codex_gate")
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "standard")
        assert resolve_review_seats().seats == ("codex_gate",)


class TestHeavyAndLight:
    def test_heavy_gives_three_seats_and_names_itself_in_the_record(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = "heavy"
        seats = resolve_review_seats()
        assert seats.seats == ("codex_gate", "kimi_gate", "claude_gate")
        assert (seats.profile, seats.profile_source, seats.profile_error) == ("heavy", "project_config", None)

    def test_heavy_from_the_environment_names_env_as_source(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", " heavy ")
        seats = resolve_review_seats()
        assert (seats.profile, seats.profile_source) == ("heavy", "env")

    def test_light_keeps_the_standard_stack_until_the_floor_lands(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
        assert resolve_review_seats().seats == TODAY

    def test_the_override_source_is_named(self, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_GOVERNANCE_PROFILE", "heavy")
        assert gate_profile.resolve().source == "override"

    def test_other_fields_are_provided_for_the_later_readers(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
        resolution = gate_profile.resolve()
        assert resolution.value("round_cap") == 5
        assert resolution.value("plan_gate_seats") == 2
        assert resolution.value("takeover_chain") == "codex_gate,kimi_gate,claude_gate,deepseek_gate"

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(KeyError):
            gate_profile.resolve().value("round_limit")


class TestFailClosed:
    @pytest.mark.parametrize("raw", ["zwaar", "Heavy", "high", "standard heavy", "LIGHT", "stndard", "0", "none"])
    def test_a_malformed_value_raises_with_raw_value_and_source(self, monkeypatch, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        with pytest.raises(GateProfileConfigError) as caught:
            gate_profile.resolve()
        assert caught.value.raw == raw
        assert caught.value.source == "env"
        assert repr(raw) in str(caught.value) and "env" in str(caught.value)

    def test_the_review_seats_refuse_instead_of_returning_the_default_stack(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "zwaar")
        with pytest.raises(GateProfileConfigError, match="zwaar"):
            resolve_review_seats()

    def test_the_source_is_project_config_when_the_row_is_malformed(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = "Heavy"
        with pytest.raises(GateProfileConfigError) as caught:
            resolve_review_seats()
        assert caught.value.source == "project_config"

    def test_a_non_text_value_is_refused(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = 3
        with pytest.raises(GateProfileConfigError, match="not text"):
            gate_profile.resolve()

    def test_a_bad_override_is_refused_even_when_the_env_is_valid(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
        monkeypatch.setenv("VNX_OVERRIDE_GOVERNANCE_PROFILE", "zwaar")
        with pytest.raises(GateProfileConfigError) as caught:
            gate_profile.resolve()
        assert caught.value.source == "override"

    def test_the_error_is_a_review_config_error_so_the_door_refuses_it(self):
        from dispatch_spec import ReviewGateConfigError

        assert issubclass(GateProfileConfigError, ReviewGateConfigError)


class TestPrecedence:
    @pytest.mark.parametrize("profile", ["light", "standard", "heavy"])
    def test_an_explicit_stack_wins_over_every_profile(self, monkeypatch, profile):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", profile)
        seats = resolve_review_seats(explicit_stack=["kimi_gate"])
        assert (seats.source, seats.seats) == ("explicit", ("kimi_gate",))
        assert seats.profile == profile

    def test_an_explicit_stack_records_an_invalid_profile_instead_of_standard(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "zwaar")
        seats = resolve_review_seats(explicit_stack=["kimi_gate"])
        assert seats.profile is None
        assert seats.profile_source == "env"
        assert "zwaar" in seats.profile_error

    def test_a_project_config_stack_wins_over_heavy(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = "heavy"
        project_config["VNX_DEFAULT_REVIEW_STACK"] = "codex_gate"
        assert resolve_review_seats().seats == ("codex_gate",)

    def test_an_override_stack_wins_over_heavy(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "kimi_gate")
        assert resolve_review_seats().seats == ("kimi_gate",)

    def test_heavy_wins_over_the_env_stack(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
        monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "codex_gate")
        assert len(resolve_review_seats().seats) == 3


class TestResolveWithSource:
    def test_sources_in_precedence_order(self, monkeypatch, project_config):
        key = "VNX_GOVERNANCE_PROFILE"
        assert config_registry.resolve_with_source(key) == ("standard", "default")
        monkeypatch.setenv(key, "light")
        assert config_registry.resolve_with_source(key) == ("light", "env")
        project_config[key] = "heavy"
        assert config_registry.resolve_with_source(key) == ("heavy", "project_config")
        monkeypatch.setenv("VNX_OVERRIDE_GOVERNANCE_PROFILE", "standard")
        assert config_registry.resolve_with_source(key) == ("standard", "override")

    def test_an_unknown_key_is_unset(self):
        assert config_registry.resolve_with_source("VNX_NO_SUCH_KEY") == (None, "unset")

    def test_get_and_resolve_with_source_agree(self, monkeypatch, project_config):
        project_config["VNX_DEFAULT_REVIEW_STACK"] = "kimi_gate"
        assert config_registry.get("VNX_DEFAULT_REVIEW_STACK") == "kimi_gate"
        assert config_registry.resolve_with_source("VNX_DEFAULT_REVIEW_STACK")[0] == "kimi_gate"


class TestTableMatchesToday:
    def test_the_standard_column_equals_the_registry_defaults(self):
        row = gate_profile.PROFILE_TABLE["standard"]
        reg = config_registry.CONFIG_REGISTRY
        assert row["review_stack"] == reg["VNX_DEFAULT_REVIEW_STACK"].default
        assert row["round_cap"] == int(reg["VNX_REVIEW_ROUND_CAP"].default)
        assert row["takeover_chain"] == reg["VNX_REVIEW_GATE_TAKEOVER_CHAIN"].default

    def test_the_standard_plan_gate_seats_equal_the_panel_config(self):
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "configs" / "plan_gate_panel.yaml").read_text()
        assert re.search(r"^default_seat: \w+\s*$", text, re.M)
        assert re.search(r"^escalation_seat: \w+\s*$", text, re.M)
        assert gate_profile.PROFILE_TABLE["standard"]["plan_gate_seats"] == 1

    def test_every_profile_has_every_field(self):
        for name in gate_profile.PROFILES:
            assert set(gate_profile.PROFILE_TABLE[name]) == set(gate_profile.FIELDS)

    def test_the_profile_key_is_registered_with_the_standard_default(self):
        entry = config_registry.CONFIG_REGISTRY["VNX_GOVERNANCE_PROFILE"]
        assert (entry.type, entry.default, entry.category) == ("enum", "standard", "gate")
