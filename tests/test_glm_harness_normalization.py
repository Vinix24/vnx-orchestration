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
