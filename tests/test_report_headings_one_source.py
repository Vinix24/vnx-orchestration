"""test_report_headings_one_source.py — OI-1850.

A worker was told two different things about its report. The validator
(``report_body_contract._REQUIRED_SECTIONS``) wanted Summary / Changes /
Verification / Open Items. The fabric prompt (``prompts/base_worker.md``, Layer 1)
asked for "What changed", commands, test totals and "Known limitations". Workers
followed the prompt, and 175 receipts landed as ``contract_invalid`` (probe
D-86244666: ``## What changed / ## Commands run / ## Tests / ## Answer /
## Known limitations / ## Open Items``).

The directive that states the real headings was only appended by
``dispatch_prepare.prepare``. The headless lane, the provider lanes and the
terminal-pinned subprocess lane on its default flag never went through it.

Measured chain per lane (main 69972b50), and what these tests pin:

  headless envelope     envelope_prepare._prepare -> skill_context._inject_skill_context
  provider lanes        provider_dispatch._enrich_instruction -> _inject_skill_context
  subprocess lane       delivery._assemble_instruction -> _inject_skill_context  (flag 0)
                                                        -> dispatch_prepare.prepare (flag 1)
  claude_adapter        stream_events -> _inject_skill_context
  reviewer role         L1 skipped in PromptAssembler.assemble; the door seam still runs

``_inject_skill_context`` is the seam every one of them shares, so the directive
is appended there. Gate reviewers call ``PromptAssembler.assemble`` directly and
answer with a verdict, not a report: they stay directive-free (pinned below).

Nothing here starts a worker: every test builds a prompt string.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import dispatch_envelope
import envelope_prepare
import prompt_assembler
import provider_dispatch
import report_body_contract as rbc
import skill_context
from subprocess_dispatch_internals.delivery import _assemble_instruction

DIRECTIVE_SENTINEL = "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->"
DISPATCH_ID = "20260925-oi1850-lane-probe"
INSTRUCTION = "Fix the parser in scripts/lib/foo.py so it accepts empty input."
BASE_WORKER = REPO_ROOT / "scripts" / "lib" / "prompts" / "base_worker.md"


@pytest.fixture(autouse=True)
def _isolated_prompt_assembly(monkeypatch):
    """Deterministic prompt assembly: no repo map, no central intelligence DB,
    no operator flags leaking in from the shell."""
    for var in (
        "VNX_REPORT_CONTRACT_DIRECTIVE",
        "VNX_SHARED_PREPARE",
        "VNX_WORKER_RULES_FOOTER",
        "VNX_BENCH_EQUAL_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VNX_NO_REPO_MAP", "1")
    monkeypatch.setattr(
        "subprocess_dispatch._build_intelligence_section", lambda *a, **k: ""
    )


# ---------------------------------------------------------------------------
# Lane builders — each returns the final prompt the worker on that lane reads
# ---------------------------------------------------------------------------


def _headless_prompt(tmp_path, role="backend-developer"):
    spec = dispatch_envelope.EnvelopeSpec(
        dispatch_id=DISPATCH_ID,
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction=INSTRUCTION,
        role=role,
        pr_id=None,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return envelope_prepare._prepare(spec)


def _provider_args(role):
    return SimpleNamespace(
        provider="litellm:deepseek",
        dispatch_id=DISPATCH_ID,
        terminal_id="T1",
        instruction=INSTRUCTION,
        model="deepseek-v4-pro",
        pr_id=None,
        dispatch_paths="",
        role=role,
        no_auto_commit=True,
        max_retries=1,
        gate="",
        deadline_seconds=900,
    )


def _provider_prompt(tmp_path, role="backend-developer"):
    with (
        patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path),
        patch.object(provider_dispatch, "_resolve_data_dir", return_value=tmp_path),
    ):
        return provider_dispatch._enrich_instruction(_provider_args(role))


def _subprocess_prompt(role="backend-developer"):
    return _assemble_instruction(
        "T1", INSTRUCTION, role, DISPATCH_ID, "sonnet", None,
    )


def _seam_prompt(role, metadata=None):
    return skill_context._inject_skill_context(
        "T1", INSTRUCTION, role, {"dispatch_id": DISPATCH_ID, **(metadata or {})},
    )


LANES = {
    "headless envelope, prompts/roles role": lambda tmp: _headless_prompt(tmp),
    "headless envelope, agents/ role (legacy path)": lambda tmp: _headless_prompt(
        tmp, role="quality-engineer"
    ),
    "provider lanes, prompts/roles role": lambda tmp: _provider_prompt(tmp),
    "provider lanes, agents/ role (legacy path)": lambda tmp: _provider_prompt(
        tmp, role="quality-engineer"
    ),
    "subprocess lane, VNX_SHARED_PREPARE=0": lambda tmp: _subprocess_prompt(),
    "claude_adapter.stream_events seam": lambda tmp: _seam_prompt(
        "backend-developer", {"model": "sonnet"}
    ),
    "reviewer role through the door": lambda tmp: _seam_prompt("reviewer"),
}


def _assert_one_directive(prompt: str) -> str:
    """The directive is in the prompt exactly once and lists every contract
    heading. The headings come from ``_REQUIRED_SECTIONS``, not a copy of it."""
    found = prompt.count(DIRECTIVE_SENTINEL)
    assert found == 1, f"expected exactly one report directive in the prompt, found {found}"
    assert prompt.count("## Report Body Contract") == 1
    block = prompt[prompt.index(DIRECTIVE_SENTINEL):]
    for heading in rbc._REQUIRED_SECTIONS:
        assert f"`{heading}`" in block, f"{heading} missing from the directive"
    return block


@pytest.mark.parametrize("lane", list(LANES))
def test_every_lane_prompt_carries_the_directive_exactly_once(lane, tmp_path):
    prompt = LANES[lane](tmp_path)

    block = _assert_one_directive(prompt)

    assert DISPATCH_ID in block


def test_subprocess_lane_shared_prepare_places_the_directive_last_and_once(monkeypatch):
    """The real flow, not a patched injector: prepare() opts the seam out and
    appends the directive itself, after the scope guard and the rules footer."""
    monkeypatch.setenv("VNX_SHARED_PREPARE", "1")
    from dispatch_prepare import prepare

    prompt = prepare(
        terminal_id="T1",
        instruction=INSTRUCTION,
        role="backend-developer",
        dispatch_id=DISPATCH_ID,
        dispatch_paths=["scripts/lib/foo.py"],
        pr_id="PR-1",
        model="sonnet",
    )

    block = _assert_one_directive(prompt)
    assert "`## PR`" in block
    assert prompt.index("<!-- VNX-WORKER-RULES-FOOTER -->") < prompt.index(DIRECTIVE_SENTINEL)
    assert prompt.index("## Scope Guard") < prompt.index(DIRECTIVE_SENTINEL)
    assert "## Scope Guard" not in block
    assert "<!-- VNX-WORKER-RULES-FOOTER -->" not in block


def test_headless_prompt_names_the_model_and_provider_it_runs_on(tmp_path):
    block = _assert_one_directive(_headless_prompt(tmp_path))

    assert f"`**Dispatch-ID**: {DISPATCH_ID}`" in block
    assert "`**Model**: sonnet`" in block
    assert "`**Provider**: claude`" in block


def test_directive_asks_for_the_identity_block():
    """The receipt converter refuses a dispatch report without a real Model
    (``_validate_model_present``), so the directive has to ask for it."""
    directive = rbc.build_directive(DISPATCH_ID)

    assert "**Dispatch-ID**:" in directive
    assert "**Model**:" in directive
    assert "**Provider**:" in directive


def test_directive_follows_the_role_text(tmp_path):
    """When a role text and the directive disagree the directive wins by
    standing after it."""
    prompt = _headless_prompt(tmp_path)

    _assert_one_directive(prompt)
    assert "# Role: Backend Developer" in prompt
    assert prompt.index("# Role: Backend Developer") < prompt.index(DIRECTIVE_SENTINEL)


def test_directive_switch_off_is_honoured_on_the_headless_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_REPORT_CONTRACT_DIRECTIVE", "0")

    assert DIRECTIVE_SENTINEL not in _headless_prompt(tmp_path)


def test_seam_without_a_dispatch_id_adds_no_directive():
    """A report contract belongs to one dispatch; ad-hoc callers with no id get
    the role context unchanged."""
    prompt = skill_context._inject_skill_context("T1", INSTRUCTION, "backend-developer")

    assert DIRECTIVE_SENTINEL not in prompt


# ---------------------------------------------------------------------------
# The injector fails: the fallback prompt still carries the directive
# ---------------------------------------------------------------------------


def test_headless_fallback_prompt_still_carries_the_directive(tmp_path):
    with patch.object(
        skill_context, "_inject_skill_context", side_effect=RuntimeError("role source broke")
    ):
        prompt = _headless_prompt(tmp_path)

    assert "You are operating as a **backend-developer** worker." in prompt
    _assert_one_directive(prompt)


def test_provider_fallback_prompt_still_carries_the_directive(tmp_path):
    with patch.object(
        skill_context, "_inject_skill_context", side_effect=RuntimeError("role source broke")
    ):
        prompt = _provider_prompt(tmp_path)

    assert "You are operating as a **backend-developer** worker." in prompt
    _assert_one_directive(prompt)


# ---------------------------------------------------------------------------
# Gate reviewers answer with a verdict, not a report
# ---------------------------------------------------------------------------


def test_gate_reviewer_prompt_stays_directive_free():
    """gate_runner builds reviewer prompts with PromptAssembler.assemble directly.
    Their contract is a verdict; a report directive there would ask for two
    outputs. The directive is appended at the door seam, not in the assembler."""
    assembled = prompt_assembler.PromptAssembler().assemble(
        dispatch_metadata={"role": "reviewer", "dispatch_id": DISPATCH_ID},
        instruction="Review the PR diff in the untrusted-data block.",
    )

    assert DIRECTIVE_SENTINEL not in assembled.to_pipe_input()


# ---------------------------------------------------------------------------
# base_worker.md names no heading list of its own
# ---------------------------------------------------------------------------


def _contract_headings() -> set[str]:
    """Every heading the validator accepts, read from its own constants."""
    accepted = set(rbc._REQUIRED_SECTIONS) | {"## PR"}
    for aliases in rbc._SECTION_ALIASES.values():
        accepted.update(aliases)
    return accepted


def test_base_worker_names_no_heading_the_validator_does_not_accept():
    references = re.findall(r"`(## [^`]+)`", BASE_WORKER.read_text())

    stray = [ref for ref in references if ref not in _contract_headings()]

    assert stray == [], f"base_worker.md names report headings outside the contract: {stray}"


def test_base_worker_report_discipline_points_at_the_directive_and_anchors_its_items():
    """Every item the discipline lists says which contract heading it goes
    under; a free-form list of report contents is how the two drifted apart."""
    text = BASE_WORKER.read_text()
    section = text.split("## Report Discipline", 1)[1].split("Do NOT:", 1)[0]
    items = [line for line in section.splitlines() if line.startswith("- ")]

    assert "Report Body Contract" in section, "must point at the directive"
    assert items, "the discipline lists what goes where"
    for item in items:
        assert any(f"`{heading}`" in item for heading in _contract_headings()), (
            f"item names no contract heading: {item!r}"
        )


# ---------------------------------------------------------------------------
# Validator agrees with what the directive tells the worker
# ---------------------------------------------------------------------------

PROBE_REPORT = """# Probe report

## What changed

Edited scripts/lib/foo.py so the parser accepts empty input and returns an empty list.

## Commands run

python3 -m pytest tests/test_foo.py -q

## Tests

12 passed in 0.4s.

## Answer

The parser now returns [] for empty input.

## Known limitations

None.

## Open Items

None.
"""


def test_probe_d86244666_report_shape_fails_the_validator():
    result = rbc.validate_body(PROBE_REPORT)

    assert result.valid is False
    assert "## Summary" in result.missing
    assert "## Changes" in result.missing


@pytest.mark.parametrize("lane", ["headless envelope, prompts/roles role", "provider lanes, prompts/roles role"])
def test_a_report_that_follows_the_lane_directive_passes_the_validator(lane, tmp_path):
    block = _assert_one_directive(LANES[lane](tmp_path))
    headings = re.findall(r"^- `(## [^`]+)`$", block, re.MULTILINE)
    assert set(rbc._REQUIRED_SECTIONS) <= set(headings)

    report = "\n\n".join(
        f"{heading}\n\nThe dispatch delivered what it was asked to and nothing else, "
        "verified with the commands below."
        for heading in headings
    )

    assert rbc.validate_body(report).valid is True


# ---------------------------------------------------------------------------
# A role that lists other headings makes the door warn, and the directive wins
# ---------------------------------------------------------------------------

DIVERGENT_ROLE = """# Role: Odd Developer

You are an odd developer.

## Reporting

Your completion report has these headings: `## What changed` / `## Commands run` / `## Tests` / `## Answer` / `## Known limitations` / `## Open Items`
"""

CONTRACT_ROLE = """# Role: Odd Developer

You are an odd developer.

## Reporting

Your completion report has these headings: `## Summary` / `## Changes` / `## Verification` / `## Open Items`
"""


def _divergence_warnings(caplog):
    return [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "report headings" in record.getMessage()
    ]


def _assembler_role_prompt(tmp_path, monkeypatch, role_text):
    roles = tmp_path / "prompts" / "roles"
    roles.mkdir(parents=True)
    (roles / "odd-role.md").write_text(role_text)
    (tmp_path / "prompts" / "base_worker.md").write_text(BASE_WORKER.read_text())
    monkeypatch.setattr(prompt_assembler, "_PROMPTS_DIR", tmp_path / "prompts")
    return roles / "odd-role.md", _seam_prompt("odd-role")


def test_assembler_role_with_other_headings_warns_with_path_and_headings(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING):
        role_path, prompt = _assembler_role_prompt(tmp_path, monkeypatch, DIVERGENT_ROLE)

    warnings = _divergence_warnings(caplog)
    assert len(warnings) == 1, "one warning for one role file"
    message = warnings[0].getMessage()
    assert str(role_path) in message
    for heading in ("## What changed", "## Commands run", "## Answer", "## Known limitations"):
        assert heading in message
    assert "## Summary" in message and "## Changes" in message  # what it never names
    # Not blocked, and the directive stands after the role text.
    block = _assert_one_directive(prompt)
    assert "`## Summary`" in block
    assert prompt.index("You are an odd developer.") < prompt.index(DIRECTIVE_SENTINEL)


def test_legacy_agents_role_with_other_headings_warns_with_path_and_headings(
    tmp_path, caplog
):
    import subprocess_dispatch

    agent_md = tmp_path / "agents" / "odd-role" / "CLAUDE.md"
    agent_md.parent.mkdir(parents=True)
    agent_md.write_text(DIVERGENT_ROLE)
    fake_file = tmp_path / "scripts" / "lib" / "subprocess_dispatch.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.touch()

    with (
        patch.object(subprocess_dispatch, "__file__", str(fake_file)),
        caplog.at_level(logging.WARNING),
    ):
        prompt = _seam_prompt("odd-role")

    warnings = _divergence_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(agent_md) in message
    assert "## What changed" in message and "## Known limitations" in message
    _assert_one_directive(prompt)
    assert prompt.index("You are an odd developer.") < prompt.index(DIRECTIVE_SENTINEL)


def test_role_that_lists_the_contract_headings_does_not_warn(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        _assembler_role_prompt(tmp_path, monkeypatch, CONTRACT_ROLE)

    assert _divergence_warnings(caplog) == []


@pytest.mark.parametrize("role", ["backend-developer", "test-engineer", "reviewer", "quality-engineer"])
def test_shipped_roles_do_not_trip_the_warning(role, caplog):
    with caplog.at_level(logging.WARNING):
        _seam_prompt(role)

    assert _divergence_warnings(caplog) == []
