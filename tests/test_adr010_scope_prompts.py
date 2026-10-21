#!/usr/bin/env python3
"""tests/test_adr010_scope_prompts.py: the role prompts summarise ADR-010 as ADR-010 says it.

ADR-010 is about the Claude path: Claude is reached only through the ``claude`` CLI
subprocess, and LiteLLM is permitted for non-Claude providers (the ADR's Accepted list).
The reviewer and security-engineer prompts said that all LLM delivery happens
"exclusively" via a CLI subprocess. On 2026-10-21 codex blocked a lane that calls only
DeepSeek over HTTP with its own key on that reading, after another codex run had passed
the same question (dispatch 20261021-adr010-rolprompt). These tests pin the narrowed
wording so a later rewrite cannot fall back to the LLM-wide variant without a red test.

The prompts are read through ``PromptAssembler.assemble``, the path the gates take, so
the test sees what the reviewer sees and not only what is on disk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# scripts/lib is on sys.path through tests/conftest.py
from prompt_assembler import PromptAssembler

VNX_ROOT = Path(__file__).resolve().parent.parent
ADR010_REL = "docs/governance/decisions/ADR-010-subprocess-adapter-claude-routing.md"
ROLES_DIR = VNX_ROOT / "scripts" / "lib" / "prompts" / "roles"

# Roles whose prompt carries an ADR-010 entry of its own.
ROLES_WITH_ADR010_ENTRY = ("reviewer", "security-engineer")

# A sentence that restricts delivery to a CLI subprocess.
_RESTRICTS = re.compile(r"(?i)\bexclusively\b|\bonly\b|-only\b|\bmust go (via|through)\b")
_CODE_SPAN = re.compile(r"`[^`]*`")
_LLM_WIDE = re.compile(r"(?i)subprocess-only LLM|\bLLM delivery\b|\bLLM calls must\b")


def adr010_entry_lines(text: str) -> list[str]:
    """Return the source lines of the ADR-010 entry: the line that opens it, up to the next
    list item, bold heading, section heading or blank line. Raise when the prompt has no
    such entry or more than one."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s*(- )?\*\*ADR-010\b", line)]
    if len(starts) != 1:
        raise ValueError(f"expected exactly one ADR-010 entry in role prompt text, found {len(starts)}")
    entry = [lines[starts[0]]]
    for line in lines[starts[0] + 1:]:
        if not line.strip() or line.startswith(("- ", "#", "**")):
            break
        entry.append(line)
    return entry


def adr010_entry(text: str) -> str:
    """The ADR-010 entry with whitespace collapsed (line wrapping is layout, not contract)."""
    return re.sub(r"\s+", " ", " ".join(adr010_entry_lines(text))).strip()


def unscoped_restrictions(entry: str) -> list[str]:
    """Sentences that restrict delivery to a CLI subprocess without naming Claude.

    Code spans are blanked first: ``subprocess.Popen(["claude", ...])`` names the binary,
    not the provider, and is exactly how the LLM-wide summary slipped past a reader."""
    without_code = _CODE_SPAN.sub("CODE", entry)
    sentences = [s for s in re.split(r"(?<=[.!?:])\s+", without_code) if s.strip()]
    return [s for s in sentences if "subprocess" in s.lower() and _RESTRICTS.search(s) and "Claude" not in s]


def _assembled_context(role: str) -> str:
    return PromptAssembler().assemble({"role": role}, "review this diff").context


@pytest.mark.parametrize("role", ROLES_WITH_ADR010_ENTRY)
def test_no_exclusive_subprocess_claim_without_naming_claude(role: str) -> None:
    offending = unscoped_restrictions(adr010_entry(_assembled_context(role)))
    assert offending == [], f"{role}: ADR-010 entry restricts delivery without naming Claude: {offending}"


@pytest.mark.parametrize("role", ROLES_WITH_ADR010_ENTRY)
def test_non_claude_provider_with_own_key_is_outside_adr010(role: str) -> None:
    entry = adr010_entry(_assembled_context(role))
    assert (
        "A non-Claude provider called over HTTP or LiteLLM with its own key is outside ADR-010"
        " and not a finding under it" in entry
    ), f"{role}: ADR-010 entry does not place a non-Claude provider with its own key outside it"


@pytest.mark.parametrize("role", ROLES_WITH_ADR010_ENTRY)
def test_error_list_matches_what_adr010_rejects(role: str) -> None:
    entry = adr010_entry(_assembled_context(role))
    assert "`severity: error`" in entry
    for item in (
        "Anthropic SDK import",
        "direct HTTP to `api.anthropic.com` or a REST shim around it",
        "LiteLLM for Claude",
        "Anthropic API key or OAuth token in code or config",
    ):
        assert item in entry, f"{role}: ADR-010 entry is missing {item!r}"


@pytest.mark.parametrize("role", ROLES_WITH_ADR010_ENTRY)
def test_entry_cites_the_adr_line_that_permits_non_claude_providers(role: str) -> None:
    entry = adr010_entry(_assembled_context(role))
    match = re.search(re.escape(ADR010_REL) + r":(\d+)", entry)
    assert match, f"{role}: ADR-010 entry does not cite {ADR010_REL}:<line>"
    adr_lines = (VNX_ROOT / ADR010_REL).read_text(encoding="utf-8").splitlines()
    cited = adr_lines[int(match.group(1)) - 1]
    assert "non-Claude providers" in cited and "The Claude path stays subprocess." in cited, (
        f"{role}: cited line {match.group(1)} does not permit non-Claude providers: {cited!r}"
    )


@pytest.mark.parametrize("role", ROLES_WITH_ADR010_ENTRY)
def test_entry_stays_two_to_four_lines(role: str) -> None:
    lines = adr010_entry_lines(_assembled_context(role))
    body = lines if lines[0].lstrip().startswith("- ") else lines[1:]
    assert 2 <= len(body) <= 4, f"{role}: ADR-010 entry body has {len(body)} lines, expected 2 to 4"


def test_no_role_prompt_carries_an_llm_wide_adr010_summary() -> None:
    offenders = [
        path.name
        for path in sorted(ROLES_DIR.glob("*.md"))
        if _LLM_WIDE.search(re.sub(r"\s+", " ", path.read_text(encoding="utf-8")))
    ]
    assert offenders == [], f"role prompts carry an LLM-wide ADR-010 summary: {offenders}"


def test_unscoped_restrictions_catches_the_old_wording() -> None:
    """The check must fire on the summary these prompts carried before the fix."""
    old = (
        "- **ADR-010 — Subprocess-only LLM delivery**: LLM delivery happens exclusively via\n"
        '  `subprocess.Popen(["claude", ...])` or equivalent CLI subprocess. No SDK, no\n'
        "  direct HTTP to api.anthropic.com, no embedded API key. Flag violations as\n"
        "  `severity: error`.\n"
    )
    assert unscoped_restrictions(adr010_entry(old)) == [
        "- **ADR-010 — Subprocess-only LLM delivery**:",
        "LLM delivery happens exclusively via CODE or equivalent CLI subprocess.",
    ]


def test_adr010_entry_raises_when_the_role_has_no_entry() -> None:
    with pytest.raises(ValueError, match="exactly one ADR-010 entry"):
        adr010_entry("# Role: Something\n\n- **ADR-003 — No SDK imports**: text\n")


def test_adr010_entry_raises_on_a_duplicate_entry() -> None:
    text = "- **ADR-010 — one**: a\n  b\n- **ADR-010 — two**: c\n  d\n"
    with pytest.raises(ValueError, match="found 2"):
        adr010_entry(text)
