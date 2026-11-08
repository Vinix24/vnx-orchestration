"""Gate profile P4 (track gate-profiel-per-project): the plan-gate seats come from the profile.

Pinned here: ``light`` and ``standard`` on a security path still give two seats (the path floor),
``heavy`` on a docs path gives two seats from the start with quorum 2, an unset key gives today's
composition, a malformed profile refuses, and ``gov_trace`` names the profile and its source.
"""
from __future__ import annotations

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import config_registry
import plan_gate_panel
import planning_cli
from gate_profile import GateProfileConfigError

DOCS = "docs/operations/dispatch-rules.md"
SECURITY = "scripts/lib/security/token_guard.py"


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def project_config(monkeypatch):
    rows = {}
    config_registry.set_db_resolver(lambda _pid, key: rows.get(key))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    return rows


def _weight(path):
    return planning_cli._derive_panel_weight(
        panel_seats=None, dispatch_paths=path, task_class=None, irreversible=False, project_id="p1",
    )


def _guard_precondition():
    assert plan_gate_panel.second_seat_from_start([SECURITY]), "SECURITY must be a floor path"
    assert not plan_gate_panel.second_seat_from_start([DOCS]), "DOCS must not be a floor path"


class TestCompositionToday:
    def test_unset_docs_path_is_one_seat_with_escalation(self):
        _guard_precondition()
        weight = _weight(DOCS)
        assert (weight["seats"], weight["quorum"]) == (1, 1)
        assert weight["escalation"] is not None
        assert "profile=" not in weight["gov_trace"]

    def test_unset_security_path_is_two_seats(self):
        weight = _weight(SECURITY)
        assert (weight["seats"], weight["quorum"], weight["escalation"]) == (2, 2, None)

    @pytest.mark.parametrize("profile", ["light", "standard"])
    def test_light_and_standard_keep_the_security_floor(self, project_config, profile):
        project_config["VNX_GOVERNANCE_PROFILE"] = profile
        weight = _weight(SECURITY)
        assert (weight["seats"], weight["quorum"]) == (2, 2)
        assert f"profile={profile} (project_config)" in weight["gov_trace"]

    @pytest.mark.parametrize("profile", ["light", "standard"])
    def test_light_and_standard_docs_path_stay_one_seat(self, project_config, profile):
        project_config["VNX_GOVERNANCE_PROFILE"] = profile
        weight = _weight(DOCS)
        assert (weight["seats"], weight["quorum"]) == (1, 1)


class TestHeavy:
    def test_heavy_docs_path_gives_two_seats_from_the_start(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = "heavy"
        weight = _weight(DOCS)
        assert (weight["seats"], weight["quorum"], weight["escalation"]) == (2, 2, None)
        assert "profile=heavy (project_config)" in weight["gov_trace"]
        assert "both seats from the start (gate profile)" in weight["gov_trace"]

    def test_heavy_security_path_keeps_the_path_reason(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = "heavy"
        weight = _weight(SECURITY)
        assert (weight["seats"], weight["quorum"]) == (2, 2)
        assert "gate profile" not in weight["gov_trace"]

    def test_heavy_via_environment_names_its_source(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
        trace = _weight(DOCS)["gov_trace"]
        assert "profile=heavy (" in trace and "profile=heavy (default)" not in trace

    def test_an_operator_seat_override_wins_and_still_names_the_profile(self, project_config):
        project_config["VNX_GOVERNANCE_PROFILE"] = "heavy"
        weight = planning_cli._derive_panel_weight(
            panel_seats="codex", dispatch_paths=DOCS, task_class=None, irreversible=False,
            project_id="p1",
        )
        assert (weight["seats"], weight["chosen_by"]) == (1, "operator")
        assert "profile=heavy (project_config)" in weight["gov_trace"]


class TestRefusals:
    @pytest.mark.parametrize("raw", ["zwaar", "Heavy", "high", "standard heavy", 3])
    def test_a_malformed_profile_refuses(self, project_config, raw):
        project_config["VNX_GOVERNANCE_PROFILE"] = raw
        with pytest.raises(GateProfileConfigError):
            _weight(DOCS)

    @pytest.mark.parametrize("bad", [0, -1, "2", 1.5, True])
    def test_compose_panel_refuses_a_malformed_seat_count(self, bad):
        with pytest.raises(ValueError, match="profile_seats"):
            plan_gate_panel.compose_panel(profile_seats=bad)

    @pytest.mark.parametrize("bad", [0, "2", 2.0])
    def test_a_malformed_seat_count_refuses_even_on_a_floor_path(self, bad):
        with pytest.raises(ValueError, match="profile_seats"):
            plan_gate_panel.compose_panel(dispatch_paths=[SECURITY], profile_seats=bad)


class TestComposePanel:
    def test_profile_seats_two_gives_both_seats_and_the_label(self):
        composition = plan_gate_panel.compose_panel(profile_seats=2, profile="heavy (env)")
        assert (len(composition.seats), composition.quorum, composition.escalation) == (2, 2, None)
        assert composition.profile == "heavy (env)"
        assert composition.second_seat is None

    def test_profile_seats_one_never_lowers_a_floor(self):
        composition = plan_gate_panel.compose_panel(dispatch_paths=[SECURITY], profile_seats=1)
        assert (len(composition.seats), composition.quorum) == (2, 2)

    def test_none_is_todays_composition(self):
        composition = plan_gate_panel.compose_panel(dispatch_paths=[DOCS])
        assert (len(composition.seats), composition.quorum, composition.profile) == (1, 1, None)
