"""Review stack: seat 1 is a subscription reviewer; seat 2 and the takeover chain may be metered.

Operator decision 2026-11-11: the Kimi subscription is cancelled. Seat 2 is deepseek_gate on
deepseek-v4-pro (API credit), the default chain codex_gate,deepseek_gate,claude_gate; no default
names kimi_gate, which stays registered for an explicit stack or chain. Seat 1 stays codex.

Two things are pinned here, each against the real code. Nothing starts a kimi,
codex, glm or deepseek process.

1. Guard: seat 1 of every default stack is a subscription gate and no default
   names kimi_gate. Classification comes from ONE place,
   ``gate_billing_table.GATE_BILLING`` (imported by ``gate_recorder``, which is
   where this file reads it).
2. The obligation the door declares follows the same order (``_primary_review_gate``).

The shared project-store setup is in ``tests/review_stack_support.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import build_project_store, isolate_config_registry

import config_registry as cr
from gate_recorder import (
    GATE_BILLING,
    GATE_BILLING_METERED,
    GATE_BILLING_NONE,
    GATE_BILLING_SUBSCRIPTION,
    GATE_PROVIDERS,
    UnknownGateProvider,
    gate_billing,
    metered_before_subscription,
)


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build_project_store(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# 1. Guard: an API-credit gate never stands before a subscription gate
# ---------------------------------------------------------------------------

def _split(value: str) -> list:
    return [item.strip() for item in value.split(",") if item.strip()]


class TestBillingClassification:
    def test_every_registered_gate_is_classified_and_nothing_else(self):
        assert set(GATE_BILLING) == set(GATE_PROVIDERS), (
            "GATE_BILLING and GATE_PROVIDERS drifted: classify a new gate by billing next to "
            f"its registry entry. unclassified={sorted(set(GATE_PROVIDERS) - set(GATE_BILLING))} "
            f"orphaned={sorted(set(GATE_BILLING) - set(GATE_PROVIDERS))}"
        )
        assert set(GATE_BILLING.values()) <= {
            GATE_BILLING_SUBSCRIPTION, GATE_BILLING_METERED, GATE_BILLING_NONE,
        }

    @pytest.mark.parametrize("gate,expected", [
        ("codex_gate", GATE_BILLING_SUBSCRIPTION),
        ("kimi_gate", GATE_BILLING_SUBSCRIPTION),
        ("glm_gate", GATE_BILLING_METERED),
        ("deepseek_gate", GATE_BILLING_METERED),
    ])
    def test_the_four_review_seats_carry_the_operator_classification(self, gate, expected):
        assert gate_billing(gate) == expected

    def test_an_unclassified_gate_is_refused_not_assumed_subscription(self):
        with pytest.raises(UnknownGateProvider) as exc:
            gate_billing("some_future_gate")
        assert "some_future_gate" in str(exc.value)

    def test_billing_vocabulary_is_the_dispatch_plan_vocabulary(self):
        """A gate and the lane that carries it describe one provider in one word."""
        import dispatch_plan
        source = Path(dispatch_plan.__file__).read_text(encoding="utf-8")
        assert f'"{GATE_BILLING_SUBSCRIPTION}"' in source
        assert f'"{GATE_BILLING_METERED}"' in source


class TestOrderPredicate:
    @pytest.mark.parametrize("order,expected", [
        (["glm_gate", "codex_gate"], ("glm_gate", "codex_gate")),
        (["deepseek_gate", "kimi_gate"], ("deepseek_gate", "kimi_gate")),
        (["codex_gate", "glm_gate", "kimi_gate"], ("glm_gate", "kimi_gate")),
        (["ci_gate", "glm_gate", "codex_gate"], ("glm_gate", "codex_gate")),
        (["codex_gate", "kimi_gate", "glm_gate", "deepseek_gate"], None),
        (["codex_gate", "kimi_gate"], None),
        (["glm_gate", "deepseek_gate"], None),
        (["glm_gate", "claude_github_optional"], None),
        (["ci_gate", "codex_gate"], None),
        ([], None),
    ])
    def test_the_predicate_names_the_first_violating_pair(self, order, expected):
        assert metered_before_subscription(order) == expected

    def test_the_predicate_refuses_an_unclassified_gate(self):
        with pytest.raises(UnknownGateProvider):
            metered_before_subscription(["codex_gate", "some_future_gate"])


def _defaults():
    import gate_profile
    import governance_profiles
    from gate_request_handler import _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN as handler_chain
    rows, reg = gate_profile.PROFILE_TABLE.values(), cr.CONFIG_REGISTRY
    stacks = [reg["VNX_DEFAULT_REVIEW_STACK"].default] + [row["review_stack"] for row in rows]
    others = [reg["VNX_REVIEW_GATE_TAKEOVER_CHAIN"].default, handler_chain] + [row["takeover_chain"] for row in rows]
    return [_split(v) for v in stacks], [_split(v) for v in others] + [
        p.required_gates for p in governance_profiles.DEFAULT_PROFILES.values()]


class TestRegistryDefaultsKeepTheOrder:
    def test_seat_1_of_every_default_stack_is_a_subscription_gate(self):
        for stack in _defaults()[0]:
            assert gate_billing(stack[0]) == GATE_BILLING_SUBSCRIPTION, f"{stack}: seat 1 (2026-11-11)"

    def test_no_default_stack_chain_or_profile_names_kimi_gate(self):
        stacks, others = _defaults()
        assert [g for g in stacks + others if "kimi_gate" in g] == [], "Kimi is cancelled (2026-11-11)"

    def test_takeover_chain_starts_with_the_subscription_seats_of_the_default_stack(self):
        from gate_request_handler import _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
        chain = _split(_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN)
        stack = _split(cr.CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default)
        assert chain[:len(stack)] == stack
        # R7 (2026-10-22): the one subscription reviewer outside the standing stack is
        # claude_gate, and it reads before every API-credit gate.
        assert [g for g in chain[len(stack):] if gate_billing(g) == GATE_BILLING_SUBSCRIPTION] == ["claude_gate"]

    def test_claude_gate_is_the_subscription_end_link_after_deepseek(self):
        from gate_request_handler import _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
        chain = _split(_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN)
        assert chain == ["codex_gate", "deepseek_gate", "claude_gate"]
        assert gate_billing("claude_gate") == GATE_BILLING_SUBSCRIPTION

    def test_an_empty_project_config_resolves_the_new_stack_and_chain(self, env):
        import review_gate_manager
        from gate_request_handler import review_gate_takeover_chain_raw
        assert review_gate_manager._build_default_review_stack()[:2] == ["codex_gate", "deepseek_gate"]
        assert review_gate_takeover_chain_raw() == ("codex_gate,deepseek_gate,claude_gate", "default")


# ---------------------------------------------------------------------------
# 2. The obligation the door declares follows the same order
# ---------------------------------------------------------------------------

class TestPrimaryReviewSeat:
    @staticmethod
    def _seat(monkeypatch, stack: str) -> str:
        import config_runtime
        import smart_router
        real_get = config_runtime.get
        monkeypatch.setattr(
            config_runtime, "get",
            lambda key, *a, **kw: stack if key == "VNX_DEFAULT_REVIEW_STACK" else real_get(key, *a, **kw),
        )
        return smart_router._primary_review_gate()

    @pytest.mark.parametrize("stack,expected", [
        ("codex_gate,kimi_gate", "codex_gate"),
        ("kimi_gate,codex_gate", "kimi_gate"),
        ("glm_gate,codex_gate", "codex_gate"),
        ("glm_gate,kimi_gate", "kimi_gate"),
        ("deepseek_gate,glm_gate,kimi_gate", "kimi_gate"),
        ("codex_gate,glm_gate", "codex_gate"),
        ("deepseek_gate,glm_gate", "deepseek_gate"),
        ("glm_gate,claude_github_optional", "glm_gate"),
        ("claude_github_optional,glm_gate", "glm_gate"),
        ("kimi_gate", "kimi_gate"),
    ])
    def test_a_subscription_gate_fills_the_seat_before_an_api_credit_gate(self, monkeypatch, stack, expected):
        assert self._seat(monkeypatch, stack) == expected

    def test_the_registry_default_still_declares_codex(self, monkeypatch, env):
        import smart_router
        assert smart_router._primary_review_gate() == "codex_gate"

    @pytest.mark.parametrize("gate,stack", [
        # glm loses the seat on billing anyway: the shape a lenient GATE_BILLING.get()
        # would have hidden, ranking the unclassified gate below codex and saying nothing.
        ("glm_gate", "codex_gate,glm_gate"),
        ("kimi_gate", "codex_gate,kimi_gate"),
        ("codex_gate", "codex_gate"),
    ])
    def test_a_gate_missing_from_the_billing_table_fails_loud_naming_it(self, monkeypatch, gate, stack):
        from dispatch_spec import ReviewGateConfigError

        assert gate in GATE_PROVIDERS, "the gate must be a registered one; only its billing class is missing"
        monkeypatch.delitem(GATE_BILLING, gate)

        with pytest.raises(ReviewGateConfigError, match=f"{gate} has no billing class"):
            self._seat(monkeypatch, stack)

    def test_an_unclassified_gate_outside_the_stack_does_not_block_the_seat(self, monkeypatch):
        monkeypatch.delitem(GATE_BILLING, "deepseek_gate")

        assert self._seat(monkeypatch, "codex_gate,kimi_gate") == "codex_gate"
