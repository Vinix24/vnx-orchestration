"""tests/test_routing_policy.py — Unit tests for the Wave 7 cost-routing policy engine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Ensure scripts/lib is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from routing_policy import (
    RoutingDecision,
    _resolve_fallback_chain,
    _rule_matches,
    decide_lane,
    is_claude_headless_blocked,
    lane_to_claude_model,
    load_lane_safety,
    load_routing_policy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLICY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "providers" / "routing_policy.yaml"


def _env(**kwargs: str) -> dict:
    """Build a minimal env dict for testing."""
    return dict(kwargs)


# ---------------------------------------------------------------------------
# load_routing_policy
# ---------------------------------------------------------------------------


def test_missing_yaml_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_routing_policy(tmp_path / "nonexistent.yaml")


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "routing_policy.yaml"
    bad.write_text("version: 1\nrules: [invalid: yaml: : :\n")
    with pytest.raises(ValueError, match="malformed"):
        load_routing_policy(bad)


def test_wrong_version_raises(tmp_path: Path) -> None:
    bad = tmp_path / "routing_policy.yaml"
    bad.write_text("version: 2\ndefault_lane: x\n")
    with pytest.raises(ValueError, match="unsupported routing_policy version"):
        load_routing_policy(bad)


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "routing_policy.yaml"
    bad.write_text("- item1\n- item2\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_routing_policy(bad)


# ---------------------------------------------------------------------------
# decide_lane — happy path via production policy file
# ---------------------------------------------------------------------------


def test_default_lane_unmatched() -> None:
    """worker-provider-kimi-flip (2026-07-23): default_lane is now kimi."""
    decision = decide_lane("unknown-task-class", complexity="medium", env=_env())
    assert decision.lane == "kimi"
    assert decision.rule_name == "default"


def test_simple_cleanup_routes_to_kimi() -> None:
    """worker-provider-kimi-flip: simple-cleanup no longer carves out haiku — kimi
    covers low-complexity build-worker housekeeping too."""
    decision = decide_lane("lint-narrow", complexity="low", env=_env())
    assert decision.lane == "kimi"
    assert decision.rule_name == "simple-cleanup"


def test_doc_update_routes_to_kimi() -> None:
    decision = decide_lane("doc-update", complexity="low", env=_env())
    assert decision.lane == "kimi"


def test_refactor_medium_routes_to_kimi() -> None:
    decision = decide_lane("refactor", complexity="medium", env=_env())
    assert decision.lane == "kimi"
    assert decision.rule_name == "refactor-code-default"


def test_refactor_high_routes_to_kimi() -> None:
    decision = decide_lane("refactor", complexity="high", env=_env())
    assert decision.lane == "kimi"


def test_opt_in_deepseek_requires_flag() -> None:
    # Without opt-in flag: refactor + medium -> refactor-code-default (kimi)
    without_flag = decide_lane("refactor", complexity="medium", env=_env())
    assert without_flag.lane == "kimi"
    assert without_flag.rule_name == "refactor-code-default"

    # With opt-in flag: cost-optimized-code rule fires first because rules are ordered
    # BUT cost-optimized-code comes after refactor-code-default in the yaml.
    # The test verifies the flag-gating behavior: with flag, the cost-optimized rule wins.
    with_flag = decide_lane(
        "refactor", complexity="medium", env=_env(VNX_USE_CHEAP_LANE="1")
    )
    # cost-optimized-code rule is defined AFTER refactor-code-default in the yaml,
    # but it has the same task_class + complexity criteria PLUS an opt_in_flag guard.
    # First-match semantics: refactor-code-default fires first (no opt_in_flag guard).
    # Cost-optimized-code only fires when the earlier rule doesn't match — but it does.
    # So with the current yaml order, kimi still wins even with the flag.
    # This is intentional: operators must move cost-optimized-code above refactor-code-default
    # in the yaml to activate it for refactor tasks.
    assert with_flag.lane == "kimi"

    # Directly validate that _rule_matches respects opt_in_flag:
    cost_rule = {
        "name": "cost-optimized-code",
        "when": {
            "task_class": ["refactor"],
            "complexity": ["medium"],
            "opt_in_flag": "VNX_USE_CHEAP_LANE",
        },
        "lane": "litellm:deepseek:deepseek-v4-pro",
    }
    assert not _rule_matches(cost_rule, "refactor", "medium", _env())
    assert _rule_matches(cost_rule, "refactor", "medium", _env(VNX_USE_CHEAP_LANE="1"))


def test_review_routes_to_kimi() -> None:
    decision = decide_lane("code-review", complexity="medium", env=_env())
    # review-analysis must route to the kimi CLI lane, not litellm:moonshot
    # (kimi-via-cli-only constraint: litellm:moonshot is blocking).
    assert decision.lane == "kimi"
    assert decision.rule_name == "review-analysis"


def test_analysis_routes_to_kimi() -> None:
    decision = decide_lane("analysis", complexity="low", env=_env())
    assert decision.lane == "kimi"


def test_research_high_routes_to_opus() -> None:
    decision = decide_lane("research", complexity="high", env=_env())
    assert decision.lane == "claude/opus"
    assert decision.rule_name == "research-deep"


# ---------------------------------------------------------------------------
# Fallback chain resolution
# ---------------------------------------------------------------------------


def test_fallback_chain_resolution_deepseek() -> None:
    """worker-provider-kimi-flip (2026-07-23): kimi has NO fallback — a kimi-lane
    failure must fail loud, never silently re-route onto the Claude subscription."""
    decision = decide_lane("code-review", complexity="medium", env=_env())
    assert decision.lane == "kimi"
    assert decision.fallback_chain == []


def test_fallback_chain_wildcard_deepseek() -> None:
    """Wildcard pattern litellm:deepseek:* resolves codex as first fallback (was
    kimi; OI-940 kimi quota exhausted 2026-08-02)."""
    policy = load_routing_policy(_POLICY_PATH)
    fallback_map = policy.get("fallback_chain", {})
    chain = _resolve_fallback_chain("litellm:deepseek:deepseek-v4-pro", fallback_map)
    assert chain[0] == "codex"
    assert "claude/sonnet-5" in chain
    assert len(chain) >= 2


def test_fallback_chain_haiku() -> None:
    policy = load_routing_policy(_POLICY_PATH)
    fallback_map = policy.get("fallback_chain", {})
    chain = _resolve_fallback_chain("claude/haiku-4-5", fallback_map)
    assert chain == ["claude/sonnet-5"]


def test_fallback_chain_kimi_empty() -> None:
    """worker-provider-kimi-flip (2026-07-23): kimi has NO fallback chain — a routing
    miss on the kimi lane must fail loud, not silently re-route onto claude/sonnet-5."""
    policy = load_routing_policy(_POLICY_PATH)
    fallback_map = policy.get("fallback_chain", {})
    assert _resolve_fallback_chain("kimi", fallback_map) == []


def test_fallback_chain_sonnet_empty() -> None:
    """Sonnet and Opus have no fallback — they are the safety net."""
    policy = load_routing_policy(_POLICY_PATH)
    fallback_map = policy.get("fallback_chain", {})
    assert _resolve_fallback_chain("claude/sonnet-5", fallback_map) == []
    assert _resolve_fallback_chain("claude/opus", fallback_map) == []


# ---------------------------------------------------------------------------
# lane_to_claude_model
# ---------------------------------------------------------------------------


def test_lane_to_claude_model_mappings() -> None:
    assert lane_to_claude_model("claude/sonnet-4-6") == "sonnet"
    assert lane_to_claude_model("claude/haiku-4-5") == "haiku"
    assert lane_to_claude_model("claude/opus") == "opus"


def test_lane_to_claude_model_litellm_returns_none() -> None:
    assert lane_to_claude_model("litellm:deepseek:deepseek-v4-pro") is None
    assert lane_to_claude_model("litellm:moonshot:kimi-k2-0905-default") is None
    assert lane_to_claude_model("litellm:zai:glm-5.2") is None


# ---------------------------------------------------------------------------
# RoutingDecision dataclass
# ---------------------------------------------------------------------------


def test_routing_decision_has_required_fields() -> None:
    d = RoutingDecision(lane="claude/sonnet-4-6", rule_name="default", rationale="x")
    assert d.lane == "claude/sonnet-4-6"
    assert d.rule_name == "default"
    assert d.rationale == "x"
    assert d.fallback_chain == []


def test_routing_decision_returns_rationale() -> None:
    decision = decide_lane("lint-narrow", complexity="low", env=_env())
    assert decision.rationale  # non-empty rationale from yaml


# ---------------------------------------------------------------------------
# Cross-check: routing_policy lanes × provider_constraints (C2 unit test)
#
# Ensures that every non-Claude lane in routing_policy.yaml is NOT blocked by
# any provider_constraints.yaml entry when passed as a plain --provider arg.
# Catches the kimi-via-cli-only / litellm:moonshot collision structurally.
# ---------------------------------------------------------------------------


def _parse_lane_provider(lane: str) -> tuple:
    """Split a lane string into (provider, sub_provider, via) for constraint check.

    Examples:
      "kimi"                               -> ("kimi", None, None)
      "litellm:deepseek:deepseek-v4-pro"  -> ("litellm", "deepseek", None)
      "litellm:moonshot:kimi-k2-0905-..."  -> ("litellm", "moonshot", None)
      "claude/sonnet-4-6"                  -> ("claude", None, None)  # skip
    """
    if lane.startswith("litellm:"):
        parts = lane.split(":", 2)
        sub = parts[1] if len(parts) > 1 else None
        return "litellm", sub, None
    if "/" in lane:
        return lane.split("/", 1)[0], None, None
    return lane, None, None


def test_policy_lanes_pass_constraint_preflight() -> None:
    """Every non-Claude lane in routing_policy.yaml must not violate provider_constraints.

    This is the structural cross-check for the kimi-via-cli-only conflict
    (sweep H4, 2026-06-11). Any litellm:moonshot lane would trigger a blocking
    violation here; the corrected 'kimi' lane must pass cleanly.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
    from providers.constraint_enforcer import ConstraintEnforcer, ConstraintViolationError

    policy = load_routing_policy(_POLICY_PATH)
    enforcer = ConstraintEnforcer()

    # Collect all unique lanes from rules + fallback_chain values
    lanes: set = set()
    for rule in policy.get("rules", []):
        lanes.add(rule.get("lane", ""))
    for chain in policy.get("fallback_chain", {}).values():
        if isinstance(chain, list):
            lanes.update(chain)
    lanes.discard("")

    # Only check non-Claude lanes (Claude lane constraint violations are warn-only
    # and depend on terminal_id context, not on lane parsing alone)
    non_claude_lanes = {lane for lane in lanes if not lane.startswith("claude/")}

    violations_found: list[str] = []
    for lane in sorted(non_claude_lanes):
        provider, sub_provider, via = _parse_lane_provider(lane)
        try:
            enforcer.enforce(provider=provider, sub_provider=sub_provider, via=via)
        except ConstraintViolationError as exc:
            violations_found.append(f"lane={lane!r}: [{exc.code}] {exc.message}")

    assert not violations_found, (
        "routing_policy.yaml contains lanes that violate provider_constraints.yaml:\n"
        + "\n".join(f"  - {v}" for v in violations_found)
        + "\n\nFix: update the lane in routing_policy.yaml to a non-blocked provider."
    )


# ---------------------------------------------------------------------------
# load_lane_safety / is_claude_headless_blocked — OI-223 lane_safety loader
# ---------------------------------------------------------------------------


def test_load_lane_safety_reads_production_yaml() -> None:
    """lane_safety is actually loaded from the production routing_policy.yaml."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    assert "headless_block" in lane_safety


def test_force_headless_removed_from_lane_safety() -> None:
    """OI-1174: force_headless was dead config — never read by any runtime gating
    decision, only asserted-present by a test. Removed so a half-wired flag can't be
    mistaken for a live guard. This test pins the ABSENCE: headless is reached only
    via the explicit allow_headless=true DispatchSpec opt-in."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    assert "force_headless" not in lane_safety


def test_load_lane_safety_missing_block_returns_empty(tmp_path: Path) -> None:
    policy_file = tmp_path / "routing_policy.yaml"
    policy_file.write_text("version: 1\ndefault_lane: claude/sonnet-5\n")
    assert load_lane_safety(policy_file) == {}


def test_load_lane_safety_non_mapping_raises(tmp_path: Path) -> None:
    policy_file = tmp_path / "routing_policy.yaml"
    policy_file.write_text("version: 1\ndefault_lane: claude/sonnet-5\nlane_safety: [1, 2]\n")
    with pytest.raises(ValueError, match="lane_safety block must be a mapping"):
        load_lane_safety(policy_file)


def test_production_headless_block_is_open_by_default() -> None:
    """dispatch-20260811c-b: the block is OFF by default (lane_safety.headless_block.enabled:
    false) — claude -p runs on the subscription, not API credits, so it no longer needs the
    billing-protection block. No override needed to proceed."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    assert lane_safety["headless_block"]["enabled"] is False
    assert is_claude_headless_blocked(lane_safety, env={}) is False


def test_production_headless_block_override_env_still_wired() -> None:
    """The override_env key is retained even though the block is off, so a future revert
    (enabled: true) doesn't also need the escape hatch re-added — the switch stays reversible."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    assert lane_safety["headless_block"]["override_env"] == "VNX_OVERRIDE_CLAUDE_HEADLESS"


def test_production_headless_block_lifted_with_override() -> None:
    """Still not blocked with the override set — no regression from the open-by-default state."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    assert is_claude_headless_blocked(
        lane_safety, env={"VNX_OVERRIDE_CLAUDE_HEADLESS": "1"}
    ) is False


def test_headless_block_reenabled_still_honours_override() -> None:
    """Reversibility proof: if enabled is ever flipped back to true (the yaml-side revert),
    the retained override_env config still works exactly as it did before the open — without
    the flag it blocks, with the flag it doesn't. Uses the real block's override_env value
    with enabled forced True, so this exercises the actual configured escape hatch, not just
    the default env-var name."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    reenabled = {
        "headless_block": {
            **lane_safety["headless_block"],
            "enabled": True,
        }
    }
    override_env = reenabled["headless_block"]["override_env"]
    assert is_claude_headless_blocked(reenabled, env={}) is True
    assert is_claude_headless_blocked(reenabled, env={override_env: "1"}) is False


def test_headless_block_wrong_override_value_still_open() -> None:
    """A garbage override value doesn't matter while the block is off — still not blocked."""
    lane_safety = load_lane_safety(_POLICY_PATH)
    assert is_claude_headless_blocked(
        lane_safety, env={"VNX_OVERRIDE_CLAUDE_HEADLESS": "yes"}
    ) is False


def test_headless_blocked_missing_block_fails_closed() -> None:
    """No `headless_block` entry at all -> still blocked (fail-closed default)."""
    assert is_claude_headless_blocked({}, env={}) is True
    assert is_claude_headless_blocked({}, env={"VNX_OVERRIDE_CLAUDE_HEADLESS": "1"}) is False


def test_headless_blocked_disabled_via_yaml() -> None:
    lane_safety = {"headless_block": {"enabled": False}}
    assert is_claude_headless_blocked(lane_safety, env={}) is False


def test_headless_blocked_custom_override_env_name() -> None:
    lane_safety = {"headless_block": {"enabled": True, "override_env": "MY_CUSTOM_FLAG"}}
    assert is_claude_headless_blocked(lane_safety, env={}) is True
    assert is_claude_headless_blocked(lane_safety, env={"MY_CUSTOM_FLAG": "1"}) is False
    # The default env var name is NOT the override when a custom name is configured.
    assert is_claude_headless_blocked(
        lane_safety, env={"VNX_OVERRIDE_CLAUDE_HEADLESS": "1"}
    ) is True


def test_headless_blocked_defaults_env_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """env=None falls back to os.environ (real dispatcher call pattern)."""
    lane_safety = {"headless_block": {"enabled": True}}
    monkeypatch.delenv("VNX_OVERRIDE_CLAUDE_HEADLESS", raising=False)
    assert is_claude_headless_blocked(lane_safety) is True
    monkeypatch.setenv("VNX_OVERRIDE_CLAUDE_HEADLESS", "1")
    assert is_claude_headless_blocked(lane_safety) is False


# ---------------------------------------------------------------------------
# The dormant fallback axis (OI-1347 / OI-1499)
#
# routing_policy.yaml's `fallback_chain` is DECLARATIVE. It is parsed into
# RoutingDecision.fallback_chain and then read by nobody in production: the only
# readers anywhere are tests. The one caller of decide_lane
# (subprocess_dispatch._maybe_route_cheap_lane) sits behind
# VNX_ROUTING_POLICY_ENABLED, which is unset, and explicitly refuses to consult
# fallback_chain for non-Claude lanes.
#
# The live fallback axis is elsewhere: wave7_models.yaml routing.tier_map, walked
# by smart_router.tier_routing.resolve_tier_route.
#
# That split is the hazard. A fix aimed at "the fallback chain" naturally lands on
# this file, because this is the one that says fallback_chain in it — and then has
# no effect, while the axis that actually routes is untouched. These tests pin the
# dormancy so the next such fix trips a red test instead of shipping a no-op.
# ---------------------------------------------------------------------------

import re as _re

_SRC_ROOTS = ("scripts", "vnx_cli", "bin")
_REPO_ROOT = _POLICY_PATH.parents[3]
# Where the field is legitimately defined/populated rather than consumed.
_FALLBACK_CHAIN_OWNERS = {"scripts/lib/routing_policy.py"}


def _production_fallback_chain_readers() -> list[str]:
    """Source locations that READ a resolved `.fallback_chain`, outside its owner.

    Assignments (`fallback_chain=`) and the dataclass field declaration are
    population, not consumption, and are excluded.
    """
    hits: list[str] = []
    pattern = _re.compile(r"\.fallback_chain\b")
    for root in _SRC_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _FALLBACK_CHAIN_OWNERS:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if pattern.search(line) and "fallback_chain=" not in line:
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_fallback_chain_has_no_production_reader():
    """If this goes red you are wiring the DORMANT fallback axis.

    Before adding a reader, reconcile it with the live axis
    (wave7_models.yaml routing.tier_map, walked by resolve_tier_route) — the two
    describe fallback differently and have never been reconciled. Wiring this one
    without that reconciliation gives the fleet two disagreeing fallback engines.
    """
    readers = _production_fallback_chain_readers()
    assert readers == [], (
        "routing_policy.yaml's fallback_chain gained a production reader:\n  "
        + "\n  ".join(readers)
        + "\n\nThe live fallback axis is wave7_models.yaml routing.tier_map "
        "(smart_router.tier_routing.resolve_tier_route). Reconcile the two before "
        "wiring this one, then update this test with the decision."
    )


def test_kimi_has_an_empty_chain_by_design():
    """`kimi: []` is a deliberate fail-loud choice (worker-provider-kimi-flip,
    2026-07-23), NOT "kimi was removed as a destination". A kimi-lane failure on a
    build worker must fail loud rather than silently re-route onto the Claude
    subscription. Pinned so the intent is not re-read as an accidental gap."""
    policy = load_routing_policy(_POLICY_PATH)
    assert policy["fallback_chain"]["kimi"] == []


def test_no_lane_falls_back_to_kimi():
    """Pins the post-OI-940 state: codex replaced kimi as the vangnet for the
    litellm chains. The block's own comment claimed the opposite for weeks."""
    policy = load_routing_policy(_POLICY_PATH)
    with_kimi = {
        lane: chain
        for lane, chain in policy["fallback_chain"].items()
        if any("kimi" in str(step) for step in chain)
    }
    assert with_kimi == {}, (
        f"a lane now falls back to kimi: {with_kimi}. If that is intended, update "
        "the comment above fallback_chain in routing_policy.yaml in the same commit "
        "— a stale comment there is what OI-1347 tripped over."
    )


def test_no_moonshot_lane_is_configured():
    """kimi-via-cli-only blocks litellm:moonshot routing, and no moonshot key is
    configured. The block's comment claimed such entries were "kept for reference"
    long after they were gone."""
    policy = load_routing_policy(_POLICY_PATH)
    moonshot = [lane for lane in policy["fallback_chain"] if "moonshot" in lane]
    assert moonshot == []
