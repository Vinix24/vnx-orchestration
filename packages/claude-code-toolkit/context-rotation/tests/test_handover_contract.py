"""The handover contract, and the skill that tells the model about it."""
from __future__ import annotations

import re
import textwrap

import pytest

import handover_contract as hc
import handover_injection as hi
from conftest import PKG, VALID_HANDOVER

SKILL = PKG / "skills" / "rotate" / "SKILL.md"


def test_the_sample_handover_is_valid():
    assert hc.problems(VALID_HANDOVER.encode(), 8000) == []


@pytest.mark.parametrize("first", ["1) Run it.", "  1. Run it.", "1.\tRun it."])
def test_numbering_variants_are_accepted(first):
    text = VALID_HANDOVER.replace("1. Run the parser tests and read the first failure.", first)
    assert hc.problems(text.encode(), 8000) == []


@pytest.mark.parametrize("change,problem", [
    (("## Running\n", "## Background\n"), '"## Running" section is missing'),
    (("## Next steps\n", "## Plan\n"), '"## Next steps" section is missing'),
    (("1. Run the parser", "Run the parser"), 'not a step numbered "1."'),
    (("1. Run the parser", "2. Run the parser"), 'not a step numbered "1."'),
    (("## Next steps\n", "## Next steps\n### Today\n"), 'not a step numbered "1."'),
])
def test_each_problem_is_named(change, problem):
    text = VALID_HANDOVER.replace(*change)
    found = hc.problems(text.encode(), 8000)
    assert len(found) == 1 and problem in found[0]


def test_an_empty_next_steps_section_is_a_problem():
    text = VALID_HANDOVER.split("## Next steps")[0] + "## Next steps\n"
    assert hc.problems(text.encode(), 8000) == ['the first line under "## Next steps" is not a step numbered "1."']


def test_length_is_counted_in_characters_not_bytes():
    text = VALID_HANDOVER + "é" * 100
    assert hc.problems(text.encode(), len(text)) == []
    assert hc.problems(text.encode(), len(text) - 1)


def test_unreadable_and_non_utf8():
    assert hc.problems(None, 8000) == ["the file cannot be read"]
    assert hc.problems(b"\xff\xfe\x00", 8000) == ["the file is not UTF-8 text"]


def test_injection_context_is_inline_up_to_9000_characters():
    small = hi.injection_context("x" * 100, "/a/archive.md")
    assert small["mode"] == "inline" and small["text"].endswith("x" * 100)
    big = hi.injection_context("x" * 9000, "/a/archive.md")
    assert big["mode"] == "pointer" and "Read this file first" in big["text"]
    assert len(big["text"]) < 1000


def test_the_skill_names_the_sections_the_contract_checks():
    text = SKILL.read_text(encoding="utf-8")
    front = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    assert "name: rotate" in front and "description:" in front
    for heading in ("## Goal", "## Current state", "## Running", "## Next steps", "## Files touched", "## Open risks"):
        assert heading in text
    assert "## write" in text and "## resume" in text
    assert "/context-rotation:rotate resume <number>" in text
    sample = textwrap.dedent(re.search(r"```\n([ \t]*# Handover\n.*?)\n[ \t]*```", text, re.S).group(1))
    assert hc.problems(sample.encode("utf-8"), 8000) == []
