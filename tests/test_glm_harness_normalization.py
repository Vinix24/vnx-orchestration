"""test_glm_harness_normalization.py — Flip item B: GLM always via the harness lane.

Covers: Provider enum membership, door lane-wiring (_sub_provider_for/_via_for_provider with
the DISTINCT via), the bridge alias normalization (litellm:zai/zai/glm -> glm-harness), the
panel default, and the glm-via-harness-only constraint (glm-harness clears, plain litellm:zai
blocked, benchmark override downgrades to warn). The door and provider_dispatch MUST agree on
the distinct via or one pre-flight would block the other.
"""
from __future__ import annotations

import dispatch_bridge
import plan_gate_panel
import provider_dispatch
from dispatch_spec import Provider
from dispatch_cli import _sub_provider_for, _via_for_provider
from providers.constraint_enforcer import check_constraints, _override_key


def test_enum_has_glm_harness():
    assert Provider("glm-harness") is Provider.GLM_HARNESS


def test_door_lane_wiring_glm_harness():
    assert _sub_provider_for("glm-harness") == "zai"
    assert _via_for_provider("glm-harness", "zai") == "claude_harness_openrouter"


def test_bridge_alias_normalizes_zai_to_harness():
    for raw in ("litellm:zai", "zai", "glm", "glm-harness"):
        assert dispatch_bridge._canonical_provider(raw) is Provider.GLM_HARNESS
    # unrelated providers are untouched
    assert dispatch_bridge._canonical_provider("litellm:deepseek") is Provider.LITELLM_DEEPSEEK
    assert dispatch_bridge._canonical_provider("claude") is Provider.CLAUDE


def test_panel_default_uses_glm_harness_not_plain_runner():
    glm = [m for m in plan_gate_panel.DEFAULT_PANEL if m["label"].startswith("glm")]
    assert glm and glm[0]["provider"] == "glm-harness"
    assert all(m["provider"] != "litellm:zai" for m in plan_gate_panel.DEFAULT_PANEL)


def test_door_and_provider_dispatch_agree_on_via():
    # both layers stamp the SAME distinct via for glm-harness, else one pre-flight blocks the other
    assert (
        _via_for_provider("glm-harness", "zai")
        == provider_dispatch._constraint_via_for_provider("glm-harness", "zai")
        == "claude_harness_openrouter"
    )


def _glm_via(provider, sub, via, env):
    v = check_constraints(
        provider=provider, sub_provider=sub, model="glm-5.2",
        terminal_id="T1", role="backend-developer", via=via,
        env=env, check_registry=False,
    )
    return [(x.severity, x.override_applied) for x in v if x.code == "glm-via-harness-only"]


def test_glm_harness_clears_constraint():
    assert _glm_via("glm-harness", "zai", "claude_harness_openrouter", {}) == []


def test_plain_litellm_zai_is_blocked():
    assert _glm_via("litellm:zai", "zai", "openrouter", {}) == [("blocking", False)]


def test_benchmark_override_downgrades_to_warn():
    key = _override_key("glm-via-harness-only")
    assert key == "VNX_OVERRIDE_GLM_VIA_HARNESS_ONLY"
    assert _glm_via("litellm:zai", "zai", "openrouter", {key: "1"}) == [("warn", True)]


# --- END-TO-END coverage: the door must not just NORMALIZE to glm-harness, it must be able to
# --- CLEAR its own registry check and EXECUTE it. (codex flip-PR F1/F2 — the gap that let the
# --- isolated-alias/constraint tests pass while the full door->registry->envelope path was broken.)

def test_glm_harness_registry_key_is_zai():
    # GLM models register under `zai` in wave7_models.yaml; the door's registry check must look there.
    from providers.constraint_enforcer import _registry_key_for
    assert _registry_key_for("glm-harness", "zai") == "zai"


def test_glm_harness_clears_door_registry_check():
    # The single-entry door calls check_constraints(check_registry=True). Before F1, glm-harness was
    # rejected with model-not-in-current-registry BEFORE execution.
    v = check_constraints(provider="glm-harness", sub_provider="zai", model="glm-5.2",
                          terminal_id="T1", role="backend-developer", via="claude_harness_openrouter",
                          check_registry=True, env={})
    assert not [x for x in v if "registry" in x.code and x.severity == "blocking"]


def test_envelope_adapter_can_execute_glm_harness():
    # Before F2 the provider envelope raised ValueError("unsupported provider") for GLM_HARNESS.
    import inspect
    import dispatch_envelope
    src = inspect.getsource(dispatch_envelope.ProviderAdapter.run)
    assert "Provider.GLM_HARNESS" in src and "spawn_glm_harness" in src
