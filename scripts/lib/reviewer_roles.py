"""reviewer_roles.py — single decision place for "may this role be enriched?" (D1, OI-1444).

Measured 2026-09-28: kimi_gate on PR #1956 received pattern ``intel_ap_3`` in its
review prompt. A reviewer fed patterns/antipatterns from past dispatches, a
repo-map, or a role's own CLAUDE.md is no longer independent — and any later
measurement of injection's effect on quality is contaminated by an injected
gate. review-gate (glm_gate.py/kimi_gate.py/gate_runner.py harness lane) and
plan-reviewer (scripts/lib/plan_gate_tiebreaker.py, plan_gate_panel._make_default_dispatcher
default) both review a self-contained diff/plan handed to them inline; nothing
from the intelligence DB or the repo tree may reach that prompt.

Every enrichment layer (repo-map, skill/role CLAUDE.md, intelligence/patterns)
across every dispatch lane (provider_dispatch.py, envelope_prepare.py,
skill_context.py, subprocess_dispatch.py's direct-CLI path) MUST ask
``enrichment_allowed()`` before adding anything to a reviewer's prompt — never
a per-lane ``if role == "review-gate"`` copy, or a new lane silently regains
the injection this module closes.

What enrichment_allowed()=False does NOT strip: the raw dispatch instruction
(already carrying the review instruction, the diff/plan, and the verdict
contract, built by the gate script itself) and the report-body-contract
directive (``report_body_contract.with_directive`` / ``_with_report_directive``)
every lane appends regardless of role — a reviewer still needs to know where
to write its report.
"""

from __future__ import annotations

REVIEWER_ROLES = frozenset({"review-gate", "plan-reviewer"})


def enrichment_allowed(role: "str | None") -> bool:
    """False when *role* must receive NO repo-map / skill-CLAUDE.md / intelligence.

    The raw instruction and the report-body-contract directive are unaffected —
    callers keep applying those unconditionally; this only gates the optional
    enrichment layers.
    """
    return (role or "") not in REVIEWER_ROLES
