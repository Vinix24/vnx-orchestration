#!/usr/bin/env python3
"""R12: a review seat with tools is asked to check the diff against its tree (dlv-a6b9f5a96cce).

Measured 2026-10-22: PR #2173 (golf-1 C3, head 8d7a4c43, a deletion of 8
files) was refused as ``gate_execution_degenerate`` four times in a row, twice
at codex effort medium and once at high, every run with 0 shell calls, 0 file
reads and the answer "Review limited to the supplied diff". R8's no-search rule
(``gate_prompt._NO_INSTRUCTION_SEARCH_RULE``) and R9's gate-authored AGENTS.md
took away the AGENTS.md search that had been the only investigative action of
a small-diff codex run, and nothing told the reviewer to look at the tree.

The prompt of a seat that reviews with tools (codex_gate, deepseek_gate on
gate_runner's harness lane) now carries a working-directory rule right after
the no-search rule. The single-shot gates (glm_gate, kimi_gate) never get it,
on any path they take. The AGENTS.md of R9 gains one sentence and keeps every
word it had. The degenerate floor itself is not touched.

Every prompt is built through the real builders; only ``gh pr diff`` is
patched, as the neighbouring prompt tests do.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_depth
import gate_lane_contract
import gate_prompt
import gate_reanchor_cli
import gate_recorder
import gate_worktree
import glm_gate
import kimi_gate
from gate_artifacts import _compute_contract_hash
from gate_runner import GateRunner

# A deletion-only diff, the shape of PR #2173: nothing added that the prompt
# could be read for, so the tree is the only place left to check anything.
DELETION_DIFF = (
    "diff --git a/scripts/broker_shim.py b/scripts/broker_shim.py\n"
    "deleted file mode 100644\n"
    "index 1111111..0000000\n"
    "--- a/scripts/broker_shim.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-def broker_shim():\n"
    "-    return 1\n"
)

# The R9 AGENTS.md text as it was before R12, written out on purpose.
R9_TEXT = (
    "# Review workspace\n"
    "\n"
    "This directory is a disposable checkout made by the VNX review gate. "
    "Every instruction for this review is in the prompt you were given. "
    "There are no other AGENTS.md files to follow: do not search for AGENTS.md, "
    "CLAUDE.md or other instruction files, in this directory or outside it, "
    "and do not read outside this directory.\n"
)
R12_SENTENCE = (
    "Inside it, the files are there to check the diff against before you give "
    "your verdict."
)


def _rule() -> str:
    return gate_prompt._WORKING_DIRECTORY_CHECK_RULE.strip()


def _gh_diff(diff_text: str) -> mock.Mock:
    return mock.Mock(returncode=0, stdout=diff_text, stderr="")


def _payload() -> dict:
    return {"pr_number": 2173, "branch": "dispatch/dlv-golf-1-c3", "risk_class": "medium"}


def _codex_prompt(diff_text: str) -> str:
    with mock.patch("gate_runner.subprocess.run", return_value=_gh_diff(diff_text)):
        return GateRunner._build_codex_prompt(_payload())


def _gemini_prompt(diff_text: str) -> str:
    with mock.patch("gate_runner.subprocess.run", return_value=_gh_diff(diff_text)):
        return GateRunner._build_gemini_prompt(_payload())


def _harness_lane_prompt(gate: str):
    def build(diff_text: str) -> str:
        with mock.patch("gate_runner.subprocess.run", return_value=_gh_diff(diff_text)):
            return GateRunner._build_harness_lane_prompt(gate, _payload())
    return build


def _script_prompt(module, gate: str):
    def build(diff_text: str) -> str:
        return module._build_prompt(diff_text, "2173", gate_lane_contract.max_diff_chars(gate))
    return build


SEATS_WITH_TOOLS = {
    "codex_gate": _codex_prompt,
    "deepseek_gate (harness lane)": _harness_lane_prompt("deepseek_gate"),
}

SEATS_WITHOUT_THE_RULE = {
    "glm_gate script": _script_prompt(glm_gate, "glm_gate"),
    "kimi_gate script": _script_prompt(kimi_gate, "kimi_gate"),
    "glm_gate (harness lane)": _harness_lane_prompt("glm_gate"),
    "kimi_gate (harness lane)": _harness_lane_prompt("kimi_gate"),
    # The gemini builder also serves the vertex route: one REST call, no tools.
    "gemini_review": _gemini_prompt,
}


# ---------------------------------------------------------------------------
# A seat with tools: the rule, right after the no-search rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seat", sorted(SEATS_WITH_TOOLS))
def test_rule_stands_right_after_the_no_search_rule(seat: str) -> None:
    prompt = SEATS_WITH_TOOLS[seat](DELETION_DIFF)
    rule = _rule()
    no_search = gate_prompt._NO_INSTRUCTION_SEARCH_RULE.strip()

    assert prompt.count(rule) == 1, f"{seat}: the rule must be stated exactly once"
    end_of_block = prompt.index(gate_prompt.END_DIFF_MARKER)
    after_no_search = prompt.index(no_search) + len(no_search)
    assert end_of_block < prompt.index(no_search) < prompt.index(rule)
    assert prompt[after_no_search:prompt.index(rule)].strip() == "", (
        f"{seat}: nothing may stand between the no-search rule and the working-directory rule"
    )
    assert prompt.index(rule) < prompt.index(gate_prompt._CONTENT_CLAIM_RULE.strip())


@pytest.mark.parametrize("seat", sorted(SEATS_WITH_TOOLS))
def test_rule_is_the_same_text_for_every_seat_with_tools(seat: str) -> None:
    prompt = SEATS_WITH_TOOLS[seat](DELETION_DIFF)
    assert gate_prompt._WORKING_DIRECTORY_CHECK_RULE in prompt


# ---------------------------------------------------------------------------
# A seat without tools: no rule, on every path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seat", sorted(SEATS_WITHOUT_THE_RULE))
def test_single_shot_prompts_carry_no_rule(seat: str) -> None:
    prompt = SEATS_WITHOUT_THE_RULE[seat](DELETION_DIFF)
    assert _rule() not in prompt
    assert "working directory is a checkout" not in prompt
    # The prompt itself is whole: the no-search rule still stands.
    assert gate_prompt._NO_INSTRUCTION_SEARCH_RULE.strip() in prompt


# ---------------------------------------------------------------------------
# The builder: an explicit lane fact, and the rule is the only difference
# ---------------------------------------------------------------------------


def _build(**overrides) -> str:
    params = dict(
        gate_name="deepseek_gate",
        pr="2173",
        diff_text=DELETION_DIFF,
        verdict_contract=gate_lane_contract.VERDICT_CONTRACT,
        max_chars=50000,
    )
    params.update(overrides)
    return gate_prompt.build_review_prompt(**params)


def test_agentic_has_no_default() -> None:
    """A caller that does not state its lane fails, rather than getting a guess."""
    with pytest.raises(TypeError, match="agentic"):
        _build()


def test_the_rule_is_the_only_difference_between_the_two_lanes() -> None:
    with_rule = _build(agentic=True)
    without_rule = _build(agentic=False)
    assert with_rule != without_rule
    assert with_rule.replace(f"{gate_prompt._WORKING_DIRECTORY_CHECK_RULE}\n", "", 1) == without_rule


def test_the_gate_name_does_not_decide() -> None:
    """The builder reads ``agentic``, never the gate name."""
    assert _rule() in _build(gate_name="glm_gate", agentic=True)
    assert _rule() not in _build(gate_name="deepseek_gate", agentic=False)


def test_rule_says_what_to_check_and_what_happens_without_it() -> None:
    rule = _rule()
    for phrase in (
        "working directory",
        "Before you give a verdict",
        "at least one claim of the diff",
        "read a file the diff changes",
        "search the tree for a name the diff removes, renames or adds",
        "refused as degenerate",
        "counts as no review",
    ):
        assert phrase in rule, phrase


def test_the_degenerate_floor_is_unchanged() -> None:
    assert gate_depth.MIN_INVESTIGATIVE_ACTIONS == 1


# ---------------------------------------------------------------------------
# The lane fact in gate_lane_contract
# ---------------------------------------------------------------------------


def test_single_shot_gates_are_glm_and_kimi() -> None:
    assert gate_lane_contract.SINGLE_SHOT_GATES == frozenset({"glm_gate", "kimi_gate"})
    assert gate_lane_contract.reviews_with_tools("glm_gate") is False
    assert gate_lane_contract.reviews_with_tools("kimi_gate") is False


def test_every_other_harness_lane_gate_reviews_with_tools() -> None:
    harness_gates = {
        gate for gate, (kind, _name) in gate_recorder.GATE_PROVIDERS.items()
        if kind == gate_recorder.GATE_PROVIDER_HARNESS_LANE
    }
    assert gate_lane_contract.SINGLE_SHOT_GATES <= harness_gates
    for gate in harness_gates - gate_lane_contract.SINGLE_SHOT_GATES:
        assert gate_lane_contract.reviews_with_tools(gate) is True, gate
    assert gate_lane_contract.reviews_with_tools("deepseek_gate") is True
    # A claude seat added to the harness lane later reviews with tools too.
    assert gate_lane_contract.reviews_with_tools("claude_gate") is True


@pytest.mark.parametrize("gate,module", [("glm_gate", glm_gate), ("kimi_gate", kimi_gate)])
def test_reanchor_hash_still_reproduces_the_standalone_prompt(gate, module, monkeypatch) -> None:
    """glm_gate and kimi_gate are the re-anchorable gates: their contract hash
    stays the hash of the prompt the standalone script builds."""
    monkeypatch.setattr(gate_reanchor_cli, "_gh", lambda args, timeout=30: DELETION_DIFF)
    standalone = module._build_prompt(DELETION_DIFF, "2173", gate_lane_contract.max_diff_chars(gate))
    expected = _compute_contract_hash({"prompt": standalone}, gate)
    assert gate_reanchor_cli.current_contract_hash(gate, 2173) == expected


# ---------------------------------------------------------------------------
# The AGENTS.md of R9: one sentence more, nothing less
# ---------------------------------------------------------------------------


def test_agents_md_keeps_every_word_of_r9() -> None:
    text = gate_worktree.REVIEW_INSTRUCTIONS_TEXT
    assert text.startswith(R9_TEXT.rstrip("\n"))


def test_agents_md_carries_the_new_sentence_last() -> None:
    text = gate_worktree.REVIEW_INSTRUCTIONS_TEXT
    assert text.count(R12_SENTENCE) == 1
    assert text.endswith(f"do not read outside this directory. {R12_SENTENCE}\n")
