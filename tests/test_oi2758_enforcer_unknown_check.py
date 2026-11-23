"""OI-2758: a configured check without a ``_check_<name>`` fails closed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import governance_enforcer
from governance_enforcer import CheckConfig, EnforcementResult, GovernanceEnforcer

UNKNOWN = "kimi_gate_required_retired"
OVERRIDE_ENV = f"VNX_OVERRIDE_{UNKNOWN.upper()}"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_KNOWN_PROBE", raising=False)
    monkeypatch.setattr(governance_enforcer, "_append_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(governance_enforcer, "_log_enforcement_audit", None)


def _enforcer(name: str, level: int) -> GovernanceEnforcer:
    enforcer = GovernanceEnforcer()
    enforcer._checks[name] = CheckConfig(name=name, level=level)
    return enforcer


def test_unknown_check_level_2_fails_and_names_check():
    result = _enforcer(UNKNOWN, 2).check(UNKNOWN, {})
    assert result.passed is False
    assert UNKNOWN in result.message
    assert "no implementation" in result.message
    assert result.level == 2


def test_unknown_check_level_1_fails():
    result = _enforcer(UNKNOWN, 1).check(UNKNOWN, {})
    assert result.passed is False
    assert UNKNOWN in result.message


def test_unknown_check_level_3_fails_and_override_rejected(monkeypatch):
    monkeypatch.setenv(OVERRIDE_ENV, "try")
    result = _enforcer(UNKNOWN, 3).check(UNKNOWN, {})
    assert result.passed is False
    assert result.overridden_by is None


def test_unknown_check_level_0_stays_disabled():
    result = _enforcer(UNKNOWN, 0).check(UNKNOWN, {})
    assert result.passed is True
    assert "disabled" in result.message


def _enforcer_with_known(level: int, passed: bool) -> GovernanceEnforcer:
    """An enforcer with one real ``_check_known_probe`` (no git, no pytest)."""
    enforcer = _enforcer("known_probe", level)

    def _check_known_probe(cfg, ctx):
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message="known probe", override_key="VNX_OVERRIDE_KNOWN_PROBE",
        )

    enforcer._check_known_probe = _check_known_probe
    return enforcer


def test_unknown_check_level_2_override_follows_real_failure_rule(monkeypatch):
    monkeypatch.setenv(OVERRIDE_ENV, "operator says so")
    monkeypatch.setenv("VNX_OVERRIDE_KNOWN_PROBE", "operator says so")
    unknown = _enforcer(UNKNOWN, 2).check(UNKNOWN, {})
    real = _enforcer_with_known(2, passed=False).check("known_probe", {})

    assert real.passed is True, "precondition: a real level-2 failure is overridable"
    assert real.overridden_by == "operator says so"
    assert unknown.passed is real.passed
    assert unknown.overridden_by == real.overridden_by
    assert unknown.message.startswith("[OVERRIDDEN]")
    assert UNKNOWN in unknown.message


def test_unknown_check_level_2_without_override_matches_real_failure():
    unknown = _enforcer(UNKNOWN, 2).check(UNKNOWN, {})
    real = _enforcer_with_known(2, passed=False).check("known_probe", {})
    assert unknown.passed is real.passed is False
    assert unknown.overridden_by is None and real.overridden_by is None


def test_known_check_unchanged():
    ok = _enforcer_with_known(2, passed=True).check("known_probe", {})
    assert ok.passed is True and ok.message == "known probe"
    bad = _enforcer_with_known(2, passed=False).check("known_probe", {})
    assert bad.passed is False and bad.message == "known probe"
