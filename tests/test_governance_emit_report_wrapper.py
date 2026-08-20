"""tests/test_governance_emit_report_wrapper.py — the generic wrapper report is machine-readable.

D1 of track `report-wrapper-machine-readable`. These tests assert the invariant that
holds AFTER the fix (D2), not the defect before it, so they are RED on `main @2c352354`
and turn green when D2 lands. Source is untouched by this file on purpose: the fix is a
separate dispatch, so a test written here cannot be written toward its own solution.

The defect being closed: when a worker writes no report of its own,
``governance_emit.emit_unified_report()`` synthesizes a wrapper that pastes the FULL
dispatch instruction under ``## Instruction``. A plan-gate instruction carries the
``vnx-plan-verdict`` contract template, so the wrapper hands the plan-gate parser a fence
that is unparseable by construction (``"verdict": "pass" | "revise" | "block"``). The seat
then abstains on "verdict block is not valid JSON" while its real judgement sits in prose
under ``## Response``. Measured on the 20-08 panel run: the kimi seat report carried
exactly one verdict fence, and it was the echoed template.

Removing the echo is only safe together with the second half of D2. Today the wrapper
carries NO model field at all — it writes ``- Provider: {provider}`` as a BULLET, and
``scripts/report_parser.py:360-372`` recovers model/provider exclusively from the BOLD
form. For a wrapper report without frontmatter, the door-stamped ``**Model**`` inside the
echoed instruction is therefore the only model source, and ``_validate_model_present``
(``scripts/lib/append_receipt_internals/validation.py:361``) refuses fail-closed without
one. Measured 20-08: 171 of 3078 wrapper reports carry no frontmatter and would lose
their model identity to a bare echo removal. Hence the wrapper writes its own bold
identity block.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for _p in (str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import plan_gate_panel as pgp  # noqa: E402
from governance_emit import emit_unified_report  # noqa: E402


# A plan-gate instruction as the seats actually receive it: prose plus the verbatim
# verdict-contract template. The union value is not valid JSON by construction — that is
# what makes the echoed copy indistinguishable from a garbled real verdict.
_INSTRUCTION_SENTINEL = "SENTINEL-D1-INSTRUCTION-BODY-MUST-NOT-BE-ECHOED"
_PLAN_GATE_INSTRUCTION = (
    "You are one seat on a plan-gate panel.\n"
    f"{_INSTRUCTION_SENTINEL}\n\n"
    "Emit your verdict in exactly this form:\n\n"
    "```vnx-plan-verdict\n"
    "{\n"
    '  "verdict": "pass" | "revise" | "block",\n'
    '  "blocking_findings": [],\n'
    '  "rationale": "<one paragraph>"\n'
    "}\n"
    "```\n\n"
    "END PLAN\n"
)

# The measured failure mode: the model answered in prose and never emitted a fence of
# its own. Nothing here may be mistaken for a verdict block.
_PROSE_RESPONSE = (
    "I judge this plan needs revision. The census is not closed and the deliverable "
    "sizing rests on an untested assumption. I have no machine-readable block to offer.\n"
)


@pytest.fixture()
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def _wrapper_kwargs(data_dir, **overrides):
    kwargs = dict(
        dispatch_id="20260820-d1-wrapper-echo",
        terminal_id="T2",
        provider="litellm:deepseek",
        instruction=_PLAN_GATE_INSTRUCTION,
        response_text=_PROSE_RESPONSE,
        findings=[],
        duration_seconds=12.5,
        data_dir=data_dir,
    )
    kwargs.update(overrides)
    return kwargs


def test_wrapper_report_does_not_echo_the_instruction(tmp_data):
    """The wrapper report does not carry the instruction text it was built from.

    The instruction survives on disk in the dispatch bundle, findable by dispatch-id, so
    dropping it from the report loses no content. What it stops losing is the parser:
    every marker the instruction carries stops being indistinguishable from worker output.
    """
    path = emit_unified_report(**_wrapper_kwargs(tmp_data))
    content = path.read_text(encoding="utf-8")

    assert _INSTRUCTION_SENTINEL not in content, (
        "wrapper report echoes the dispatch instruction back into the report body; "
        "the instruction belongs in the bundle, not in the artifact the parser reads"
    )
    assert "```vnx-plan-verdict" not in content, (
        "wrapper report carries a vnx-plan-verdict fence that came from the INSTRUCTION, "
        "not from the model — this is the fence that hijacks the plan-gate parser"
    )


def test_wrapper_report_yields_no_verdict_block_found(tmp_data):
    """A prose-only answer abstains on the true reason, not on a fabricated one.

    "no verdict block found" and "verdict block is not valid JSON" describe two different
    worlds: the model emitted nothing machine-readable, versus the model emitted something
    unreadable. Today the echoed template makes the first case wear the second case's
    label, which is how a real kimi verdict was lost in round 1 of this very plan.
    """
    path = emit_unified_report(**_wrapper_kwargs(tmp_data))
    result = pgp.parse_verdict(path.read_text(encoding="utf-8"))

    assert result["rationale"] == "no verdict block found", (
        "abstention reason misreports why there is no verdict: the model emitted no fence "
        f"at all, but the parser reported {result['rationale']!r}"
    )


def test_wrapper_report_carries_its_own_bold_model_field(tmp_data):
    """The wrapper states its own model identity in the form the parser reads.

    This is what makes the echo removal safe. ``report_parser`` recovers model/provider
    only from the BOLD form, and the receipt converter refuses fail-closed without a
    model, so the wrapper must supply the identity itself rather than inherit it from a
    door-stamped field inside echoed input.
    """
    path = emit_unified_report(**_wrapper_kwargs(tmp_data, model="deepseek-v4-pro"))
    content = path.read_text(encoding="utf-8")

    assert "**Model**: deepseek-v4-pro" in content, (
        "wrapper report carries no bold **Model** field; without it a wrapper report "
        "without frontmatter has no recoverable model identity and its receipt is refused"
    )
