"""Gate profile P3 (track gate-profiel-per-project): the round cap comes from the profile.

Precedence: ``VNX_OVERRIDE_REVIEW_ROUND_CAP`` > ``project_config`` row > profile > env > 3.
Unset and ``standard`` equal today (cap 3, the C6 lock); ``heavy`` 5; ``light`` 2; a
malformed cap fails loud; a malformed profile raises ``GateProfileConfigError``.
"""
from __future__ import annotations

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import config_registry
import review_rounds
from gate_profile import GateProfileConfigError


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def project_config(monkeypatch):
    rows = {}
    config_registry.set_db_resolver(lambda _pid, key: rows.get(key))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    return rows


def test_unset_is_todays_cap():
    assert review_rounds.round_cap() == 3


def test_standard_is_todays_cap(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "standard")
    assert review_rounds.round_cap() == review_rounds.ROUND_CAP_DEFAULT == 3


def test_standard_leaves_the_env_cap_alone(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "standard")
    monkeypatch.setenv("VNX_REVIEW_ROUND_CAP", "4")
    assert review_rounds.round_cap() == 4


@pytest.mark.parametrize("profile,cap", [("heavy", 5), ("light", 2)])
def test_profile_sets_the_cap(monkeypatch, profile, cap):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", profile)
    assert review_rounds.round_cap() == cap
    assert review_rounds.round_cap(project_id="proj-x") == cap


def test_profile_outranks_the_env_cap(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    monkeypatch.setenv("VNX_REVIEW_ROUND_CAP", "4")
    assert review_rounds.round_cap() == 5


def test_a_project_config_row_wins_over_the_profile(monkeypatch, project_config):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    project_config["VNX_REVIEW_ROUND_CAP"] = "4"
    assert review_rounds.round_cap() == 4
    assert review_rounds.round_cap(project_id="proj-x") == 4


def test_the_override_wins_over_the_profile(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    monkeypatch.setenv("VNX_OVERRIDE_REVIEW_ROUND_CAP", "7")
    assert review_rounds.round_cap() == 7


@pytest.mark.parametrize("bad", ["0", "-1", "x", "2.5", ""])
def test_a_malformed_row_cap_fails_loud_even_under_a_profile(monkeypatch, project_config, bad):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    project_config["VNX_REVIEW_ROUND_CAP"] = bad
    with pytest.raises(review_rounds.RoundCapError):
        review_rounds.round_cap()


@pytest.mark.parametrize("bad", ["0", "x", "-2"])
def test_a_malformed_env_cap_fails_loud_without_a_profile(monkeypatch, bad):
    monkeypatch.setenv("VNX_REVIEW_ROUND_CAP", bad)
    with pytest.raises(review_rounds.RoundCapError):
        review_rounds.round_cap()


@pytest.mark.parametrize("bad", ["zwaar", "Heavy", "high", "standard heavy"])
def test_a_malformed_profile_raises_and_never_falls_back(monkeypatch, bad):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", bad)
    with pytest.raises(GateProfileConfigError):
        review_rounds.round_cap()
    with pytest.raises(GateProfileConfigError):
        review_rounds.round_cap(project_id="proj-x")


def test_a_state_dir_row_wins_over_the_profile(monkeypatch, tmp_path):
    import sqlite3

    import config_store_db

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = sqlite3.connect(state_dir / "runtime_coordination.db")
    try:
        config_store_db.set_config(conn, "proj-sd", "VNX_REVIEW_ROUND_CAP", "4", actor="test")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    assert review_rounds.round_cap(project_id="proj-sd", state_dir=state_dir) == 4
    other = tmp_path / "other"
    other.mkdir()
    assert review_rounds.round_cap(project_id="proj-sd", state_dir=other) == 5


def test_a_malformed_profile_raises_with_a_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "zwaar")
    with pytest.raises(GateProfileConfigError):
        review_rounds.round_cap(project_id="proj-sd", state_dir=tmp_path)


def _store_with_profile(path, project, profile):
    import sqlite3

    import config_store_db

    path.mkdir()
    conn = sqlite3.connect(path / "runtime_coordination.db")
    try:
        config_store_db.set_config(conn, project, "VNX_GOVERNANCE_PROFILE", profile, actor="test", approval_id="ap-test")
        conn.commit()
    finally:
        conn.close()


def test_the_profile_is_read_from_the_named_store_not_the_process_store(monkeypatch, tmp_path):
    _store_with_profile(tmp_path / "b", "proj-b", "heavy")
    assert review_rounds.round_cap() == 3
    assert review_rounds.round_cap(project_id="proj-b", state_dir=tmp_path / "b") == 5

