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


def test_evidence_bound_gate_default_advisory_and_requires_approval():
    entry = cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"]
    assert entry.default == "advisory"
    assert entry.type == "enum"
    assert entry.requires_approval is True


def test_feature_toggles_default_off_and_provider_deepseek():
    assert cr.CONFIG_REGISTRY["VNX_SCOUT_PREPASS"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_TAGGER_ENABLED"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_TAGGER_PROVIDER"].default == "deepseek"
    assert cr.CONFIG_REGISTRY["VNX_USE_CENTRAL_DB"].default == ""


def test_gate_and_autopilot_require_approval():
    assert cr.CONFIG_REGISTRY["VNX_CI_GATE_REQUIRED"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_WIRING_GATE_REQUIRED"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_ROADMAP_AUTOPILOT"].requires_approval is True
    # fail-safe intelligence toggles do not require approval
    assert cr.CONFIG_REGISTRY["VNX_SCOUT_PREPASS"].requires_approval is False


def test_federation_is_planned_and_not_writable():
    fed = cr.CONFIG_REGISTRY["VNX_USE_FEDERATION"]
    assert fed.planned is True
    assert fed.writable_from_ui is False


def test_central_db_is_env_only_routing():
    # VNX_USE_CENTRAL_DB is a process-start routing decision, surfaced read-only — never UI-writable
    # (live-toggling would split reads across DBs mid-process).
    assert cr.CONFIG_REGISTRY["VNX_USE_CENTRAL_DB"].writable_from_ui is False


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


# ---------------------------------------------------------------------------
# Subsystem cockpit metadata (framework-status-audit-and-cockpit PR-1)
# ---------------------------------------------------------------------------

def test_every_registry_entry_has_subsystem_and_status():
    for key, entry in cr.CONFIG_REGISTRY.items():
        assert entry.subsystem, f"{key} is missing a subsystem"
        assert entry.status in cr.ALLOWED_STATUSES, f"{key} has invalid status {entry.status!r}"


def test_all_effective_includes_subsystem_and_status():
    for row in cr.all_effective():
        assert row["subsystem"], f"{row['key']} all_effective() row is missing subsystem"
        assert row["status"] in cr.ALLOWED_STATUSES, (
            f"{row['key']} all_effective() row has invalid status {row['status']!r}"
        )


def test_config_registry_subsystems_disjoint_from_flag_backed_subsystems():
    flag_backed = {entry.subsystem for entry in cr.CONFIG_REGISTRY.values()}
    flag_less = set(cr.CONFIG_REGISTRY_SUBSYSTEMS)
    assert not (flag_backed & flag_less), "a subsystem name is represented as both flag-backed and flag-less"


def test_config_registry_subsystems_have_status_and_description():
    for name, meta in cr.CONFIG_REGISTRY_SUBSYSTEMS.items():
        assert meta.get("status") in cr.ALLOWED_STATUSES, f"{name} has invalid status"
        assert meta.get("description"), f"{name} is missing a description"


# ---------------------------------------------------------------------------
# PR-2: net-new subsystem flags (display metadata only)
# ---------------------------------------------------------------------------

PR2_NEW_FLAGS = (
    "VNX_GOVERNANCE_ENFORCED",
    "VNX_LEARNING_LOOP_ENABLED",
    "VNX_DREAM_SCHEDULER_ENABLED",
    "VNX_INJECTION_FEEDBACK_ENABLED",
    "VNX_PLAN_GATE_COMPLEX_ONLY",
    "VNX_HASH_CHAIN_REQUIRED",
    "VNX_ATTESTATION_REQUIRED",
    "VNX_MIGRATION_SYSTEM",
)


def test_pr2_new_flags_exist_default_off():
    for key in PR2_NEW_FLAGS:
        entry = cr.CONFIG_REGISTRY[key]
        if key == "VNX_MIGRATION_SYSTEM":
            assert entry.default == "manifest"
            assert entry.type == "enum"
        else:
            assert entry.default == "0", f"{key} must default off"
            assert entry.type == "bool"


def test_pr2_approval_flags_require_approval():
    for key in ("VNX_GOVERNANCE_ENFORCED", "VNX_HASH_CHAIN_REQUIRED", "VNX_ATTESTATION_REQUIRED"):
        assert cr.CONFIG_REGISTRY[key].requires_approval is True


def test_pr2_non_approval_flags_do_not_require_approval():
    for key in (
        "VNX_LEARNING_LOOP_ENABLED", "VNX_DREAM_SCHEDULER_ENABLED",
        "VNX_INJECTION_FEEDBACK_ENABLED", "VNX_PLAN_GATE_COMPLEX_ONLY",
    ):
        assert cr.CONFIG_REGISTRY[key].requires_approval is False


def test_pr2_migration_system_is_read_only():
    entry = cr.CONFIG_REGISTRY["VNX_MIGRATION_SYSTEM"]
    assert entry.writable_from_ui is False
    assert entry.default == "manifest"


def test_pr2_new_flags_have_correct_subsystem_and_status():
    expected = {
        "VNX_GOVERNANCE_ENFORCED": ("governance-enforcement-stack", "PARK"),
        "VNX_LEARNING_LOOP_ENABLED": ("intelligence-self-learning-loop", "ACTIVATE"),
        "VNX_DREAM_SCHEDULER_ENABLED": ("dream-consolidation", "ACTIVATE"),
        "VNX_INJECTION_FEEDBACK_ENABLED": ("injection-effectiveness-eval-loop", "ACTIVATE"),
        "VNX_PLAN_GATE_COMPLEX_ONLY": ("plan-gate-panel", "SCOPE"),
        "VNX_HASH_CHAIN_REQUIRED": ("receipt-hash-chain", "PARK"),
        "VNX_ATTESTATION_REQUIRED": ("signed-attestation", "PARK"),
        "VNX_MIGRATION_SYSTEM": ("migration-mechanisms", "PARK"),
    }
    for key, (subsystem, status) in expected.items():
        entry = cr.CONFIG_REGISTRY[key]
        assert entry.subsystem == subsystem, key
        assert entry.status == status, key


def test_pr2_does_not_duplicate_already_registered_flags():
    # VNX_EVIDENCE_BOUND_GATE and VNX_PLAN_GATE_ENFORCE predate PR-2 (PR-1 backfilled their
    # subsystem/status). PR-2 must not re-register them or alter their existing metadata.
    assert cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"].subsystem == "evidence-bound-gate"
    assert cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"].status == "PARK"
    assert cr.CONFIG_REGISTRY["VNX_PLAN_GATE_ENFORCE"].subsystem == "plan-gate-panel"
    assert cr.CONFIG_REGISTRY["VNX_PLAN_GATE_ENFORCE"].status == "SCOPE"
    for key in PR2_NEW_FLAGS:
        assert key not in ("VNX_EVIDENCE_BOUND_GATE", "VNX_PLAN_GATE_ENFORCE")


def test_pr2_registering_flags_does_not_change_effective_value_when_unset():
    # Metadata-only guarantee (§2.1): registering a flag must not change what get()/get_bool()
    # resolve to when nothing overrides it — no read-site behaviour change.
    for key in PR2_NEW_FLAGS:
        entry = cr.CONFIG_REGISTRY[key]
        assert cr.get(key) == entry.default


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
