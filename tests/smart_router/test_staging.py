"""Tests for smart_router.staging — per-tier rollout + deterministic canary.

Covers (dispatch 20260814s-a):
- the canary bucket is deterministic per dispatch and stays in [0, 100)
- a tier that is not enabled declines; an enabled tier at canary 0 declines;
  canary 100 routes everything; a middle value splits deterministically
- the config layer resolves through config_runtime (operator-config), defaults
  all OFF, and an unparseable canary fails closed to 0
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router import staging as st
from providers.smart_router.cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO


@pytest.fixture(autouse=True)
def _reset_config_state(monkeypatch):
    """Neutralise config-registry global state so operator-config DB wiring from
    any other test cannot leak into staging's env/default resolution here."""
    import config_registry as cr
    import config_runtime as crt

    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    crt._wired_for.clear()
    for key in list(st.TIER_FLAGS.values()) + [st.CANARY_PCT_KEY]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{key[len('VNX_'):]}", raising=False)
    yield


# ---------------------------------------------------------------------------
# Deterministic canary bucket
# ---------------------------------------------------------------------------

def test_canary_bucket_is_deterministic_and_in_range():
    key = "20260814s-a-auto-staging-canary:implement a feature"
    b1 = st.canary_bucket(key)
    b2 = st.canary_bucket(key)
    assert b1 == b2, "same dispatch must fall in the same bucket on re-evaluation"
    assert 0 <= b1 < 100


def test_canary_bucket_spreads_over_the_range():
    buckets = {st.canary_bucket(f"dispatch-{i}") for i in range(200)}
    assert len(buckets) > 50, "the hash must spread dispatches across buckets"


def test_dispatch_group_key_prefers_dispatch_id():
    key_a = st.dispatch_group_key("d-1", "T1", "same instruction")
    key_b = st.dispatch_group_key("d-2", "T1", "same instruction")
    assert key_a != key_b, "distinct dispatch ids must produce distinct group keys"


def test_dispatch_group_key_falls_back_to_content():
    key_a = st.dispatch_group_key(None, "T1", "add a helper", ["src/foo.py"])
    key_b = st.dispatch_group_key(None, "T1", "add a helper", ["src/foo.py"])
    key_c = st.dispatch_group_key(None, "T1", "add a DIFFERENT helper", ["src/foo.py"])
    assert key_a == key_b, "same content must produce the same key"
    assert key_a != key_c, "different instruction must produce a different key"


# ---------------------------------------------------------------------------
# staging_verdict
# ---------------------------------------------------------------------------

def _cfg(tiers=(), pct=0):
    return st.StagingConfig(enabled_tiers=frozenset(tiers), canary_pct=pct)


def test_verdict_tier_disabled_declines():
    verdict = st.staging_verdict(TIER_ZERO, "some-key", _cfg(tiers=()))
    assert verdict == f"{st.DECLINE_TIER_DISABLED}:{TIER_ZERO}"


def test_verdict_enabled_tier_canary_100_routes():
    assert st.staging_verdict(TIER_ZERO, "k", _cfg(tiers=(TIER_ZERO,), pct=100)) is None


def test_verdict_enabled_tier_canary_0_routes_nothing():
    verdict = st.staging_verdict(TIER_ZERO, "k", _cfg(tiers=(TIER_ZERO,), pct=0))
    assert verdict == f"{st.DECLINE_CANARY_CONTROL}:{TIER_ZERO}"


def test_verdict_middle_canary_splits_deterministically():
    config = _cfg(tiers=(TIER_ZERO,), pct=50)
    routed = [k for k in (f"dispatch-{i}" for i in range(200))
              if st.staging_verdict(TIER_ZERO, k, config) is None]
    # Not a flaky assertion on the exact fraction — just that a middle value
    # routes some and declines some, and a repeat is stable.
    assert 0 < len(routed) < 200
    first = st.staging_verdict(TIER_ZERO, "dispatch-42", config)
    second = st.staging_verdict(TIER_ZERO, "dispatch-42", config)
    assert first == second


# ---------------------------------------------------------------------------
# Config resolution + parsing
# ---------------------------------------------------------------------------

def test_load_staging_config_defaults_off():
    config = st.load_staging_config()
    assert config.enabled_tiers == frozenset()
    assert config.canary_pct == 0


def test_load_staging_config_honors_env(monkeypatch):
    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_ZERO", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_HIGH", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "25")
    config = st.load_staging_config()
    assert config.enabled_tiers == frozenset({TIER_ZERO, TIER_HIGH})
    assert config.canary_pct == 25


@pytest.mark.parametrize("raw,expected", [
    (None, 0),
    ("", 0),
    ("not-a-number", 0),
    ("-5", 0),
    ("0", 0),
    ("5", 5),
    ("100", 100),
    ("150", 100),
])
def test_parse_pct_fails_closed(raw, expected):
    assert st._parse_pct(raw) == expected


def test_registry_flags_registered_default_off_with_subsystem():
    import config_registry as cr

    for tier in (TIER_ZERO, TIER_LOW, TIER_MID, TIER_HIGH):
        key = st.TIER_FLAGS[tier]
        entry = cr.CONFIG_REGISTRY[key]
        assert entry.default == "0", f"{key} must default off (rollout starts from zero)"
        assert entry.subsystem == "smart-router-staging"
        assert entry.status == "LIVE"
    canary = cr.CONFIG_REGISTRY[st.CANARY_PCT_KEY]
    assert canary.default == "0"
    assert canary.subsystem == "smart-router-staging"
