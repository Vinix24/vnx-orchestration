#!/usr/bin/env python3
"""Tests for config_registry — the operator config SSOT (P0 config control-plane foundation).

Dispatch-ID: 20260627-config-registry

Covers the flag inventory (defaults must MIRROR the current read-site fallbacks), the resolution
precedence chain (override > DB > env > default), and the behaviour-preserving guarantee: with no
override / no DB layer / no env, get() returns exactly the current-code default.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Drop any inherited VNX_* / overrides so tests resolve against the registry, and reset the DB layer.
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    cr.set_db_resolver(None)
    yield
    cr.set_db_resolver(None)


# ---------------------------------------------------------------------------
# Inventory: defaults mirror the current code (codex finding #2)
# ---------------------------------------------------------------------------

def test_gate_flags_default_off_matching_code():
    # The read-sites read these as `get("VNX_CI_GATE_REQUIRED", "0")` — off. Registry must match.
    assert cr.CONFIG_REGISTRY["VNX_CI_GATE_REQUIRED"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_WIRING_GATE_REQUIRED"].default == "0"


def test_feature_toggles_default_off_and_provider_deepseek():
    assert cr.CONFIG_REGISTRY["VNX_SCOUT_PREPASS"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_TAGGER_ENABLED"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_TAGGER_PROVIDER"].default == "deepseek"
    assert cr.CONFIG_REGISTRY["VNX_USE_CENTRAL_DB"].default == ""


def test_gate_and_autopilot_require_approval():
    assert cr.CONFIG_REGISTRY["VNX_CI_GATE_REQUIRED"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_ROADMAP_AUTOPILOT"].requires_approval is True
    # fail-safe intelligence toggles do not require approval
    assert cr.CONFIG_REGISTRY["VNX_SCOUT_PREPASS"].requires_approval is False


def test_federation_is_planned_and_not_writable():
    fed = cr.CONFIG_REGISTRY["VNX_USE_FEDERATION"]
    assert fed.planned is True
    assert fed.writable_from_ui is False


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------

def test_default_when_nothing_set():
    assert cr.get("VNX_SCOUT_PREPASS") == "0"  # = current-code behaviour
    assert cr.get_bool("VNX_SCOUT_PREPASS") is False


def test_env_overrides_default():
    import os
    os.environ["VNX_SCOUT_PREPASS"] = "1"
    try:
        assert cr.get("VNX_SCOUT_PREPASS") == "1"
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True
    finally:
        del os.environ["VNX_SCOUT_PREPASS"]


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "0")
    assert cr.get("VNX_SCOUT_PREPASS") == "0"  # the emergency brake wins


def test_db_layer_beats_env_but_loses_to_override(monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "0")
    cr.set_db_resolver(lambda pid, key: "1" if key == "VNX_SCOUT_PREPASS" else None)
    assert cr.get("VNX_SCOUT_PREPASS") == "1"          # DB beats env
    monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "0")
    assert cr.get("VNX_SCOUT_PREPASS") == "0"           # override beats DB


def test_db_resolver_error_falls_through(monkeypatch):
    def _boom(pid, key):
        raise RuntimeError("db down")
    cr.set_db_resolver(_boom)
    # DB error must not raise — falls through to the default.
    assert cr.get("VNX_SCOUT_PREPASS") == "0"


def test_unknown_key_returns_none():
    assert cr.get("VNX_NOT_A_REAL_FLAG") is None


# ---------------------------------------------------------------------------
# all_effective
# ---------------------------------------------------------------------------

def test_all_effective_marks_defaults_and_planned():
    rows = {r["key"]: r for r in cr.all_effective()}
    assert rows["VNX_SCOUT_PREPASS"]["is_default"] is True
    assert rows["VNX_USE_FEDERATION"]["planned"] is True
    assert rows["VNX_CI_GATE_REQUIRED"]["requires_approval"] is True


def test_all_effective_reflects_env(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    rows = {r["key"]: r for r in cr.all_effective()}
    assert rows["VNX_TAGGER_ENABLED"]["value"] == "1"
    assert rows["VNX_TAGGER_ENABLED"]["is_default"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
