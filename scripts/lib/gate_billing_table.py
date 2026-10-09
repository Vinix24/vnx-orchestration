"""How each review gate's provider is BILLED: the one billing table.

The operator decision of 2026-09-26: a review gate chooses a subscription
reviewer (codex, kimi) first and an API-credit reviewer (glm, deepseek) only
when both subscription seats are unavailable. The order of a review stack and
of the takeover chain is that decision written down, so "which side of the line
is this gate" needs exactly one answer, and this module is where it is kept.

It lives in its own module, apart from ``gate_recorder`` and its registry
``GATE_PROVIDERS``, because the dispatch door reads it. The door runs with only
``scripts/lib`` on ``sys.path`` (``bin/vnx dispatch`` sets
``PYTHONPATH=<vnx home>/scripts/lib``), and ``gate_recorder`` pulls in
``governance_receipts`` -> ``append_receipt``, which lives in ``scripts/``. Read
through ``gate_recorder``, the table was unreachable from the door, and every
spec that named no gate was refused as ``gate-config-unreadable`` (OI-2022).
This module therefore imports nothing outside the standard library.
``gate_recorder`` imports the table from here, so the two cannot hold different
copies; ``tests/test_review_stack_subscription_first.py`` pins that every gate
in ``GATE_PROVIDERS`` is classified here and nothing else is.

The vocabulary is the one ``dispatch_plan.ExecutionPlan.billing`` already uses
for a dispatch lane ("subscription" / "provider_metered"), so a gate and the
lane that carries it cannot describe one provider in two different words.
``none`` is a gate that drives ``gh`` and no model at all.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

GATE_BILLING_SUBSCRIPTION = "subscription"
GATE_BILLING_METERED = "provider_metered"
GATE_BILLING_NONE = "none"

GATE_BILLING: Dict[str, str] = {
    "codex_gate": GATE_BILLING_SUBSCRIPTION,   # codex CLI, ChatGPT subscription
    "kimi_gate": GATE_BILLING_SUBSCRIPTION,    # kimi CLI OAuth (kimi-via-cli-only)
    # gemini CLI on OAuth. VNX_GEMINI_ROUTING=vertex is an explicit opt-in that
    # bills the API; the default routing is oauth, so the default is a subscription.
    "gemini_review": GATE_BILLING_SUBSCRIPTION,
    "glm_gate": GATE_BILLING_METERED,          # OpenRouter credit (zai-via-openrouter-only)
    "deepseek_gate": GATE_BILLING_METERED,     # DeepSeek API credit
    "claude_github_optional": GATE_BILLING_NONE,
    "ci_gate": GATE_BILLING_NONE,
    "wiring_gate": GATE_BILLING_NONE,
}


class UnknownGateProvider(ValueError):
    """A gate key was asked for its billing class or its availability and is not
    registered (or its provider kind is unknown).

    Raised where a silent skip would do the most damage: an operator
    configures a review stack with a gate name
    (``VNX_OVERRIDE_DEFAULT_REVIEW_STACK=glm_gate,claude_github_optional``)
    and a typo books the seat as ``not_executable`` while every other gate
    runs: the request fails without ever naming the key that was wrong.
    The name is carried in the message, so the error is loud and actionable.

    Defined here because :func:`gate_billing` raises it; ``gate_recorder``
    raises the same class from its availability check, so one ``except``
    catches both.
    """


def gate_billing(gate: str) -> str:
    """Billing class of a registered gate.

    An unregistered gate raises :class:`UnknownGateProvider` rather than
    returning a guess: a gate nobody classified is a gate whose position in a
    stack nobody can judge, and "assume subscription" is the answer that would
    let an API-credit reviewer slip in unnoticed.
    """
    billing = GATE_BILLING.get(gate)
    if billing is None:
        raise UnknownGateProvider(
            f"{gate} has no billing class in gate_billing_table.GATE_BILLING; classify it "
            f"there when you register it in gate_recorder.GATE_PROVIDERS, before placing it "
            f"in a review stack"
        )
    return billing


def metered_before_subscription(gates: Iterable[str]) -> Optional[Tuple[str, str]]:
    """First ``(metered_gate, subscription_gate)`` pair where an API-credit gate
    stands EARLIER in ``gates`` than a subscription gate, else ``None``.

    Read as an ordered preference list (a review stack, a takeover chain): the
    first pair returned is the violation of "subscription reviewers first".
    Gates that drive no model (``none``) neither violate nor satisfy the order.
    """
    first_metered: Optional[str] = None
    for gate in gates:
        billing = gate_billing(gate)
        if billing == GATE_BILLING_METERED and first_metered is None:
            first_metered = gate
        elif billing == GATE_BILLING_SUBSCRIPTION and first_metered is not None:
            return first_metered, gate
    return None
