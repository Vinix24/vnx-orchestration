#!/usr/bin/env python3
"""tests/test_oi2242_release_review_prompt.py: a release PR's review prompt gives the reviewer work in the tree.

Release PR #2151 (head 24412e26: VERSION, CHANGELOG.md and 25 deleted fragments) got
``codex_gate`` = ``unavailable`` / ``gate_execution_degenerate`` five times on 2026-10-21:
one message, zero investigative actions. The whole diff sat in the prompt and the prompt
asked for nothing beyond it, so codex read the version bump and stopped, and the release
notes' claims (which migration ships, that re-pinning to the previous version is safe)
went unchecked. The degenerate guard was right to refuse that run and stays as it is.

What changes is the prompt. A release PR (``changelog_fragments.is_release_pr``: it
changes VERSION and nothing outside VERSION, CHANGELOG.md, changelog.d/ and the legacy
list) gets a release-review task after the untrusted-diff block, in the prompt every
review gate reads. These tests build that prompt through the paths the gates take:
``GateRunner._build_codex_prompt`` (codex_gate, through ``PromptAssembler``),
``GateRunner._build_harness_lane_prompt`` (kimi_gate, deepseek_gate, glm_gate on the
governed lane) and the standalone ``glm_gate._build_prompt`` / ``kimi_gate._build_prompt``.
The task is matched as literal text, so a run on the old code fails on an assertion and
not on a missing symbol.
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
sys.path.insert(0, str(SCRIPTS_DIR / "release"))

import changelog_fragments as cf
import gate_lane_contract
import gate_prompt
import glm_gate
import kimi_gate
from gate_runner import GateRunner

TASK_HEADER = "RELEASE REVIEW TASK"

# What the task must make the reviewer do, as literal text. A rewrite that drops a
# step fails here instead of shipping a release task with nothing in it.
TASK_STEPS = (
    "Open VERSION and the new `## [X.Y.Z]` section of CHANGELOG.md in the checked-out tree",
    'Run `python3 scripts/ci/check_changelog_fragments.py --base "$(git merge-base origin/main HEAD)"`'
    " and quote its last line",
    "must exist under schemas/migrations/ and do what the intro says it does: read the migration",
    "`git show v<previous>:<path>`",
    "For every table this release's migrations touch",
    "must appear in one of the bullets folded under the new heading",
    'A claim you cannot confirm is an advisory finding (severity "warning")',
    'A claim the tree contradicts is a blocking finding (severity "error")',
)


def _file_diff(path: str, body: str, *, deleted: bool = False) -> str:
    if deleted:
        return (
            f"diff --git a/{path} b/{path}\n"
            "deleted file mode 100644\n"
            "index 1111111..0000000\n"
            f"--- a/{path}\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            f"{body}"
        )
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"{body}"
    )


VERSION_DIFF = _file_diff("VERSION", "-1.7.2\n+1.7.3\n")
CHANGELOG_DIFF = _file_diff(
    "CHANGELOG.md",
    " ## [Unreleased]\n"
    "+\n"
    "+## [1.7.3] - 2026-10-21\n"
    "+\n"
    "+This release carries migration 0035 (OI-2201); re-pinning a project to 1.7.2 is safe.\n"
    "+\n"
    "+- Cost ledger (K2a-1, #2141): one line per run with a mandatory source.\n",
)
FRAGMENT_DELETIONS = (
    _file_diff("changelog.d/20261020-kp-k2a1-grootboek.md", "-- Cost ledger (K2a-1).\n", deleted=True)
    + _file_diff("changelog.d/20261021-guard-cli-order.md", "-- T0 scripts (OI-2226).\n", deleted=True)
)
LEGACY_LIST_DELETION = _file_diff(cf.LEGACY_LIST_PATH, "-2001 " + "a" * 64 + "\n", deleted=True)

RELEASE_DIFF = VERSION_DIFF + CHANGELOG_DIFF + FRAGMENT_DELETIONS
VERSION_PLUS_SCRIPT_DIFF = VERSION_DIFF + _file_diff("scripts/gate_runner.py", "-x = 1\n+x = 2\n")
FRAGMENT_ONLY_DIFF = (
    "diff --git a/changelog.d/20261021-oi2242-release-review.md "
    "b/changelog.d/20261021-oi2242-release-review.md\n"
    "new file mode 100644\n"
    "index 0000000..3333333\n"
    "--- /dev/null\n"
    "+++ b/changelog.d/20261021-oi2242-release-review.md\n"
    "@@ -0,0 +1 @@\n"
    "+- Review gates (OI-2242): a release PR gets a review task.\n"
)


def _gh_diff(diff_text: str) -> mock.Mock:
    return mock.Mock(returncode=0, stdout=diff_text, stderr="")


def _payload() -> dict:
    return {"pr_number": 2151, "branch": "dispatch/20261021-release-v1-7-3", "risk_class": "medium"}


def _codex_prompt(diff_text: str) -> str:
    with mock.patch("gate_runner.subprocess.run", return_value=_gh_diff(diff_text)):
        return GateRunner._build_codex_prompt(_payload())


def _harness_lane_prompt(gate: str):
    def build(diff_text: str) -> str:
        with mock.patch("gate_runner.subprocess.run", return_value=_gh_diff(diff_text)):
            return GateRunner._build_harness_lane_prompt(gate, _payload())
    return build


def _script_prompt(module, gate: str):
    def build(diff_text: str) -> str:
        return module._build_prompt(diff_text, "2151", gate_lane_contract.max_diff_chars(gate))
    return build


# Every path a review gate's prompt is built on. codex_gate goes through
# PromptAssembler (reviewer role) like the ADR-010 prompt tests pin it.
GATE_PROMPT_BUILDERS = {
    "codex_gate": _codex_prompt,
    "kimi_gate": _harness_lane_prompt("kimi_gate"),
    "deepseek_gate": _harness_lane_prompt("deepseek_gate"),
    "glm_gate": _harness_lane_prompt("glm_gate"),
    "kimi_gate script": _script_prompt(kimi_gate, "kimi_gate"),
    "glm_gate script": _script_prompt(glm_gate, "glm_gate"),
}


# ---------------------------------------------------------------------------
# A release PR gets the task, in every gate's prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate", sorted(GATE_PROMPT_BUILDERS))
def test_release_pr_prompt_carries_the_release_review_task(gate: str) -> None:
    prompt = GATE_PROMPT_BUILDERS[gate](RELEASE_DIFF)
    assert prompt.count(TASK_HEADER) == 1, f"{gate}: release-review task appears {prompt.count(TASK_HEADER)} times"
    for step in TASK_STEPS:
        assert step in prompt, f"{gate}: release-review task is missing {step!r}"


@pytest.mark.parametrize("gate", sorted(GATE_PROMPT_BUILDERS))
def test_release_review_task_sits_after_the_untrusted_block(gate: str) -> None:
    prompt = GATE_PROMPT_BUILDERS[gate](RELEASE_DIFF)
    assert TASK_HEADER in prompt, f"{gate}: no release-review task"
    begin = prompt.index(gate_prompt.BEGIN_DIFF_MARKER)
    end = prompt.index(gate_prompt.END_DIFF_MARKER)
    task = prompt.index(TASK_HEADER)
    assert TASK_HEADER not in prompt[begin:end], f"{gate}: release-review task is inside the untrusted block"
    assert task > end, f"{gate}: release-review task precedes the untrusted block"


@pytest.mark.parametrize("gate", sorted(GATE_PROMPT_BUILDERS))
def test_gate_keeps_the_last_word_after_the_release_task(gate: str) -> None:
    """The restated instruction and verdict contract still close the prompt."""
    prompt = GATE_PROMPT_BUILDERS[gate](RELEASE_DIFF)
    assert TASK_HEADER in prompt, f"{gate}: no release-review task"
    tail = prompt[prompt.index(TASK_HEADER):]
    assert '"verdict": "pass|fail|blocked"' in tail, f"{gate}: verdict contract is not restated after the task"


def test_release_pr_that_also_deletes_the_legacy_list_gets_the_task() -> None:
    prompt = _codex_prompt(RELEASE_DIFF + LEGACY_LIST_DELETION)
    assert TASK_HEADER in prompt


def test_release_diff_longer_than_the_cap_still_gets_the_task() -> None:
    """Detection reads the whole diff, not the truncated rendering."""
    prompt = gate_prompt.build_review_prompt(
        gate_name="kimi_gate", pr="2151", diff_text=RELEASE_DIFF,
        verdict_contract=gate_lane_contract.VERDICT_CONTRACT, max_chars=40,
    )
    assert gate_prompt.TRUNCATION_NOTICE in prompt
    assert TASK_HEADER in prompt


# ---------------------------------------------------------------------------
# Never on a normal PR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate", sorted(GATE_PROMPT_BUILDERS))
def test_version_plus_one_script_gets_no_release_task(gate: str) -> None:
    assert TASK_HEADER not in GATE_PROMPT_BUILDERS[gate](VERSION_PLUS_SCRIPT_DIFF)


@pytest.mark.parametrize("gate", sorted(GATE_PROMPT_BUILDERS))
def test_fragment_only_pr_gets_no_release_task(gate: str) -> None:
    assert TASK_HEADER not in GATE_PROMPT_BUILDERS[gate](FRAGMENT_ONLY_DIFF)


def test_non_release_prompt_is_byte_identical_to_the_sandwich_without_a_task() -> None:
    """A normal PR's prompt does not change shape: no blank line, no stray text."""
    kwargs = dict(gate_name="glm_gate", pr="7", verdict_contract=gate_lane_contract.VERDICT_CONTRACT,
                  max_chars=50000)
    normal = gate_prompt.build_review_prompt(diff_text=VERSION_PLUS_SCRIPT_DIFF, **kwargs)
    scan = gate_prompt._scan_section(gate_prompt.scan_diff_for_instructions(VERSION_PLUS_SCRIPT_DIFF))
    lead = gate_prompt.default_review_instruction("glm_gate", "7")
    assert normal.endswith(f"{scan}\n{lead}\n\n{gate_lane_contract.VERDICT_CONTRACT}")


def test_header_line_inside_a_hunk_is_not_a_changed_path() -> None:
    """Text the author wrote starts with a hunk prefix, so it never names a path."""
    smuggled = _file_diff("CHANGELOG.md", "+diff --git a/scripts/evil.py b/scripts/evil.py\n")
    assert TASK_HEADER in _codex_prompt(VERSION_DIFF + smuggled)


def test_non_release_path_beyond_the_cap_still_counts() -> None:
    prompt = gate_prompt.build_review_prompt(
        gate_name="kimi_gate", pr="2151", diff_text=RELEASE_DIFF + _file_diff("scripts/x.py", "+y = 1\n"),
        verdict_contract=gate_lane_contract.VERDICT_CONTRACT, max_chars=len(RELEASE_DIFF),
    )
    assert TASK_HEADER not in prompt


def test_rename_out_of_a_code_path_is_no_release() -> None:
    rename = (
        "diff --git a/scripts/old.py b/changelog.d/old.md\n"
        "similarity index 100%\n"
        "rename from scripts/old.py\n"
        "rename to changelog.d/old.md\n"
    )
    assert TASK_HEADER not in _codex_prompt(VERSION_DIFF + rename)


def test_quoted_header_is_no_release() -> None:
    """git quotes a path with a space; the parser does not guess, so no task."""
    quoted = 'diff --git "a/changelog.d/a b.md" "b/changelog.d/a b.md"\nnew file mode 100644\n'
    assert TASK_HEADER not in _codex_prompt(VERSION_DIFF + quoted)


def test_empty_diff_gets_no_release_task() -> None:
    prompt = gate_prompt.build_review_prompt(
        gate_name="glm_gate", pr="1", diff_text="", verdict_contract=gate_lane_contract.VERDICT_CONTRACT,
        max_chars=50000,
    )
    assert TASK_HEADER not in prompt


# ---------------------------------------------------------------------------
# The rule itself (changelog_fragments owns it)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("paths", [
    ["VERSION"],
    ["VERSION", "CHANGELOG.md"],
    ["VERSION", "CHANGELOG.md", "changelog.d/a.md", cf.LEGACY_LIST_PATH],
])
def test_is_release_pr_accepts_release_paths_with_a_version_change(paths) -> None:
    assert cf.is_release_pr(paths)


@pytest.mark.parametrize("paths", [
    [],
    ["CHANGELOG.md", "changelog.d/a.md"],
    ["changelog.d/a.md"],
    ["VERSION", "scripts/gate_runner.py"],
    ["VERSION", "CHANGELOG.md", "docs/VERSION"],
    ["VERSION", "changelog.dx/a.md"],
])
def test_is_release_pr_refuses_anything_else(paths) -> None:
    assert not cf.is_release_pr(paths)


def test_diff_changed_paths_reads_both_sides_of_every_header() -> None:
    rename = "diff --git a/changelog.d/a.md b/changelog.d/b.md\nrename from changelog.d/a.md\n"
    assert gate_prompt.diff_changed_paths(VERSION_DIFF + rename) == [
        "VERSION", "VERSION", "changelog.d/a.md", "changelog.d/b.md",
    ]


def test_diff_changed_paths_refuses_a_header_it_cannot_read() -> None:
    assert gate_prompt.diff_changed_paths('diff --git "a/x y" "b/x y"\n') is None
