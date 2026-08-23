"""review-gate role tests (dispatch-20260823-beta2-j).

Measured 23-08 on real captured prompts (``~/.vnx-data/vnx-dev/dispatches/pending/
<dispatch_id>/final_prompt.md``): every glm_gate/kimi_gate provider-lane dispatch
ran with ``role="plan-reviewer"`` (the default of ``_make_default_dispatcher``),
which carries ``agents/plan-reviewer/CLAUDE.md`` — a role written for a PLAN-GATE
PANEL SEAT that self-authors its report file:

    - Do NOT write to any path other than the mandated report file.

That instruction contradicts each gate's own ``_VERDICT_CONTRACT``, which asks
for an INLINE ```json``` fence in the response (the governed lane already
captures the response text as the report — the model does not need to write
anything). Four real runs (three glm, one kimi) picked the file-write reading
of that contradiction and wrote a second, never-parsed report file next to the
one each gate actually reads back, landing the run as
``unavailable``/``parse_error`` even though a real review had been produced.

Fix: glm_gate.py and kimi_gate.py now construct their dispatcher with an
explicit ``role="review-gate"`` (``agents/review-gate/CLAUDE.md``) instead of
the shared default ``"plan-reviewer"``. This is a role-level split, not a
dispatch_id string check — plan_gate_panel's own plan-reviewer panelists (which
DO need to self-author a report file for the panel to read back) are
untouched, since they never pass an explicit role override and keep hitting
the "plan-reviewer" default.

These tests exercise the REAL legacy CLAUDE.md resolution path
(``skill_context._legacy_claude_md_resolution``) against the real repo's
``agents/`` directory (no mocking of project root) — the same function
``provider_dispatch._enrich_instruction`` calls for a role with no
PromptAssembler prompt file, which both "plan-reviewer" and "review-gate" are.
"""
from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import skill_context

_CONTRADICTORY_INSTRUCTION = "Do NOT write to any path other than the mandated report file."

# The exact opening line of the historical dispatch prompt, verified present in
# ~/.vnx-data/vnx-dev/dispatches/pending/glm-gate-pr1672-1787474011/final_prompt.md
# (the real run whose response opened "De review is afgerond en het rapport
# staat op de mandated locatie" and wrote a second, never-parsed report file).
_PLAN_REVIEWER_OPENING = (
    "You are a plan-reviewer worker: an independent plan-beoordelaar seated by "
    "the VNX plan-gate panel"
)

_FAKE_INSTRUCTION = "Review this diff.\n\n```json\n{\"verdict\": \"pass|fail|blocked\"}\n```\n"


def test_agents_review_gate_claude_md_exists():
    path = VNX_ROOT / "agents" / "review-gate" / "CLAUDE.md"
    assert path.is_file(), (
        "agents/review-gate/CLAUDE.md must exist — this is the role glm_gate.py/"
        "kimi_gate.py now dispatch under, resolved by skill_context._has_legacy_role_source"
    )


def test_plan_reviewer_role_carries_the_contradictory_instruction():
    """Pin the historical bug: role="plan-reviewer" (the OLD default for glm_gate/
    kimi_gate) resolves agents/plan-reviewer/CLAUDE.md, which instructs the worker
    to write its OWN report file — the instruction that contradicted each gate's
    inline-fence verdict contract and caused the measured second-report-pit bug."""
    resolved = skill_context._legacy_claude_md_resolution(
        "plan-gate", _FAKE_INSTRUCTION, "plan-reviewer", "",
    )
    assert _PLAN_REVIEWER_OPENING in resolved
    assert _CONTRADICTORY_INSTRUCTION in resolved


def test_review_gate_role_does_not_carry_the_contradictory_instruction():
    """The NEW role glm_gate.py/kimi_gate.py now dispatch under must NOT contain
    any instruction telling the worker to write its own report file — that is
    exactly the instruction that caused the bug this dispatch fixes."""
    resolved = skill_context._legacy_claude_md_resolution(
        "plan-gate", _FAKE_INSTRUCTION, "review-gate", "",
    )
    assert _CONTRADICTORY_INSTRUCTION not in resolved
    assert "mandated report file" not in resolved
    assert "REPORT FILE (MANDATORY)" not in resolved


def test_review_gate_role_explicitly_forbids_writing_a_report_file():
    """Not just silent on the topic — explicit, so a model that has seen the
    plan-reviewer framing in a prior turn or via intelligence context does not
    default back to "of course I write a report file"."""
    resolved = skill_context._legacy_claude_md_resolution(
        "plan-gate", _FAKE_INSTRUCTION, "review-gate", "",
    )
    lowered = resolved.lower()
    assert "do not write" in lowered
    assert "captured automatically" in lowered or "response text is" in lowered


def test_review_gate_role_still_carries_the_dispatch_instruction():
    """The role swap must not drop the actual dispatch payload (the diff + verdict
    contract) — only the file-write instruction changes."""
    resolved = skill_context._legacy_claude_md_resolution(
        "plan-gate", _FAKE_INSTRUCTION, "review-gate", "",
    )
    assert _FAKE_INSTRUCTION in resolved


def test_plan_reviewer_role_is_unaffected_by_this_fix():
    """plan_gate_panel's own plan-review panelists (which DO need to self-author
    a report file so the panel can read it back) must keep the exact same
    agents/plan-reviewer/CLAUDE.md content — this fix is a role-level split, not
    a removal of the file-write instruction from the role that legitimately
    needs it."""
    resolved = skill_context._legacy_claude_md_resolution(
        "plan-gate", _FAKE_INSTRUCTION, "plan-reviewer", "",
    )
    assert "vnx-plan-verdict" in resolved
    assert _CONTRADICTORY_INSTRUCTION in resolved


def test_glm_gate_and_kimi_gate_dispatch_under_review_gate_role():
    """Direct proof the two gate scripts actually pass the new role — not just
    that the role file behaves correctly in isolation."""
    glm_gate_src = (SCRIPTS_DIR / "glm_gate.py").read_text(encoding="utf-8")
    kimi_gate_src = (SCRIPTS_DIR / "kimi_gate.py").read_text(encoding="utf-8")
    assert 'role="review-gate"' in glm_gate_src
    assert 'role="review-gate"' in kimi_gate_src


def test_review_roles_ssot_includes_review_gate():
    import phantom_guard
    assert "review-gate" in phantom_guard.REVIEW_ROLES


def test_dispatch_enricher_review_roles_includes_review_gate():
    import dispatch_enricher
    assert "review-gate" in dispatch_enricher._REVIEW_ROLES
