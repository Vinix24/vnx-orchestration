"""Review stack: subscription reviewers first (codex, kimi), API credit only as fallback.

Operator decision 2026-09-26: a review gate ALWAYS chooses codex or kimi first,
because both run on a subscription (codex CLI, kimi CLI OAuth). glm (OpenRouter
credit) and deepseek (API credit) read a PR only when both are unavailable.

Two things are pinned here, each against the real code. Nothing starts a kimi,
codex, glm or deepseek process.

1. Guard: no API-credit gate stands before a subscription gate in the registry
   default stack or in the takeover chain. Classification comes from ONE place,
   ``gate_recorder.GATE_BILLING``.
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


class TestRegistryDefaultsKeepTheOrder:
    def test_default_review_stack_has_no_api_credit_gate_before_a_subscription_gate(self):
        stack = _split(cr.CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default)
        violation = metered_before_subscription(stack)
        assert violation is None, (
            f"VNX_DEFAULT_REVIEW_STACK={stack} puts API-credit gate {violation[0]} before "
            f"subscription gate {violation[1]}: codex and kimi read first, glm and deepseek "
            "only as a fallback (operator decision 2026-09-26)"
        )

    def test_default_review_stack_is_codex_then_kimi(self):
        assert _split(cr.CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default) == ["codex_gate", "kimi_gate"]

    def test_default_review_stack_has_no_standing_api_credit_seat(self):
        stack = _split(cr.CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default)
        assert [g for g in stack if gate_billing(g) == GATE_BILLING_METERED] == [], (
            "an API-credit gate is a takeover fallback, never a standing seat on every PR"
        )

    def test_takeover_chain_has_no_api_credit_gate_before_a_subscription_gate(self):
        from gate_request_handler import _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
        for source, value in (
            ("registry", cr.CONFIG_REGISTRY["VNX_REVIEW_GATE_TAKEOVER_CHAIN"].default),
            ("gate_request_handler", _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN),
        ):
            chain = _split(value)
            violation = metered_before_subscription(chain)
            assert violation is None, (
                f"{source} takeover chain {chain} puts API-credit gate {violation[0]} before "
                f"subscription gate {violation[1]}"
            )

    def test_takeover_chain_starts_with_the_subscription_seats_of_the_default_stack(self):
        from gate_request_handler import _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
        chain = _split(_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN)
        stack = _split(cr.CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default)
        assert chain[:len(stack)] == stack
        assert [g for g in chain[len(stack):] if gate_billing(g) == GATE_BILLING_SUBSCRIPTION] == []

    def test_the_resolved_default_stack_is_the_registry_default(self, env):
        import review_gate_manager
        assert review_gate_manager._build_default_review_stack()[:2] == ["codex_gate", "kimi_gate"]

    def test_the_guard_would_fail_on_the_previous_default(self):
        """RED-on-main proof for the guard itself: the default this dispatch replaced
        (``codex_gate,glm_gate``) is fine by order, but the chain with glm ahead of
        kimi is exactly what the predicate must reject."""
        assert metered_before_subscription(_split("codex_gate,glm_gate,kimi_gate,deepseek_gate")) == (
            "glm_gate", "kimi_gate",
        )


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
