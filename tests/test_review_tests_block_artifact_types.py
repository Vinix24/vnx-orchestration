#!/usr/bin/env python3
"""tests/test_review_tests_block_artifact_types.py — OI-2785 (golf schoon F1).

The review prompt's tests block told codex to run pytest on every PR. A
TypeScript test (``tests/scripts/gate_report.test.ts``) gave pytest exit 4 and
codex answered ``blocked`` without a fault in the code. The block now picks the
command from the changed test files in the diff:

  - ``.py`` test files: the pytest command with those files listed
  - any other or unknown type: no command, one line per type telling the
    reviewer to read those tests
  - a missing command is never a blocking requirement, said in so many words
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_runner  # noqa: E402
from gate_runner import GateRunner  # noqa: E402

INTERP = "/opt/homebrew/opt/python@3.12/bin/python3.12"
NEVER_BLOCKING = ("A missing test command is never a blocking requirement: "
                  "do not return `blocked` because a test could not be run.")


def _hunk(path: str, deleted: bool = False) -> str:
    mode = "deleted file mode 100644\n" if deleted else ""
    return f"diff --git a/{path} b/{path}\n{mode}@@ -1 +1 @@\n+x = 1\n"


def _no_command(ext: str) -> str:
    return (f"- `{ext}` test files: there is no test command for this type on this host; "
            "read those tests instead of running them.")


def _pytest(files: str) -> str:
    return f"Run the PR's tests with `{INTERP} -m pytest {files} -q -p no:cacheprovider`"


class TestCommandPerArtifactType:
    def test_python_only_diff_lists_the_python_test_files(self):
        diff = _hunk("scripts/a.py") + _hunk("tests/test_a.py") + _hunk("tests/lib/test_b.py")
        block = gate_runner._review_tests_block(INTERP, diff)
        assert _pytest("tests/test_a.py tests/lib/test_b.py") in block
        assert "no test command for this type" not in block
        assert NEVER_BLOCKING in block

    def test_typescript_only_diff_gets_no_pytest_command(self):
        diff = _hunk("scripts/gate_report.ts") + _hunk("tests/scripts/gate_report.test.ts")
        block = gate_runner._review_tests_block(INTERP, diff)
        assert "-m pytest" not in block
        assert _no_command(".ts") in block
        assert NEVER_BLOCKING in block

    def test_mixed_diff_gets_pytest_for_the_python_part_only(self):
        diff = _hunk("tests/test_a.py") + _hunk("tests/scripts/gate_report.test.ts")
        block = gate_runner._review_tests_block(INTERP, diff)
        assert _pytest("tests/test_a.py") in block
        assert "gate_report.test.ts -q" not in block
        assert _no_command(".ts") in block
        assert NEVER_BLOCKING in block

    def test_unknown_type_gets_a_read_line(self):
        diff = _hunk("spec/models/user_spec.rb") + _hunk("tests/test_smoke")
        block = gate_runner._review_tests_block(INTERP, diff)
        assert "-m pytest" not in block
        assert _no_command(".rb") in block
        assert _no_command("(no extension)") in block
        assert NEVER_BLOCKING in block

    def test_a_fixture_under_tests_is_not_a_test_file(self):
        diff = _hunk("scripts/a.py") + _hunk("tests/fixtures/data.json")
        block = gate_runner._review_tests_block(INTERP, diff)
        assert _pytest("<files>") in block
        assert "no test command for this type" not in block

    def test_diff_without_test_files_keeps_the_generic_line(self):
        block = gate_runner._review_tests_block(INTERP, _hunk("scripts/a.py"))
        assert _pytest("<files>") in block
        assert "no test command for this type" not in block
        assert NEVER_BLOCKING in block

    def test_deleted_test_file_is_not_handed_to_pytest(self):
        diff = _hunk("tests/test_gone.py", deleted=True) + _hunk("tests/test_kept.py")
        block = gate_runner._review_tests_block(INTERP, diff)
        assert _pytest("tests/test_kept.py") in block
        assert "test_gone.py" not in block

    def test_no_interpreter_keeps_todays_text_for_the_python_part(self):
        diff = _hunk("tests/test_a.py") + _hunk("tests/scripts/gate_report.test.ts")
        block = gate_runner._review_tests_block(None, diff)
        assert "No interpreter with pytest was found on this host" in block
        assert "must say that the tests did not run" in block
        assert "-m pytest" not in block
        assert _no_command(".ts") in block
        assert NEVER_BLOCKING in block

    def test_no_interpreter_and_only_typescript_says_nothing_about_python(self):
        block = gate_runner._review_tests_block(None, _hunk("tests/scripts/gate_report.test.ts"))
        assert "No interpreter with pytest" not in block
        assert _no_command(".ts") in block


class TestTheBuiltPromptUsesTheDiff:
    def test_codex_prompt_for_a_typescript_pr_has_no_pytest_demand(self, monkeypatch):
        monkeypatch.setattr(gate_runner, "resolve_review_interpreter_with_rule", lambda **kw: (INTERP, 3))
        diff = _hunk("tests/scripts/gate_report.test.ts")
        gh_ok = mock.Mock(returncode=0, stdout=diff, stderr="")
        with mock.patch("gate_runner.subprocess.run", return_value=gh_ok):
            prompt = GateRunner._build_codex_prompt(
                {"pr_number": 4242, "branch": "feat/ts", "risk_class": "medium"})
        assert "-m pytest" not in prompt
        assert _no_command(".ts") in prompt
        assert NEVER_BLOCKING in prompt


def _command_line(block: str) -> str:
    lines = [ln for ln in block.splitlines() if ln.startswith("Run the PR's tests with `")]
    assert len(lines) == 1, block
    return lines[0]


class TestUntrustedPathsNeverReachTheCommand:
    """Round 2 on #2865: a PR-controlled test path is data, never shell (deepseek blocker)."""

    def _assert_safe(self, command: str):
        for bad in ("$(", "`" + "id", ";", "&", "|"):
            assert bad not in command.split("`", 1)[1].rsplit("`", 1)[0], command

    def test_command_substitution_path_is_refused(self):
        block = gate_runner._review_tests_block(INTERP, _hunk("tests/test_$(id).py"))
        command = _command_line(block)
        self._assert_safe(command)
        assert _pytest("<files>") in block
        assert "left out of the command" in block
        assert '"tests/test_$(id).py"' in block

    def test_backtick_path_is_refused(self):
        block = gate_runner._review_tests_block(INTERP, _hunk("tests/x`id`.py"))
        self._assert_safe(_command_line(block))
        assert "left out of the command" in block

    def test_semicolon_path_is_refused(self):
        diff = "diff --git a/tests/test_a;rm b/tests/test_a;rm\n@@ -1 +1 @@\n+x\n" + _hunk("tests/test_a;rm.py")
        block = gate_runner._review_tests_block(INTERP, diff)
        self._assert_safe(_command_line(block))
        assert "left out of the command" in block

    def test_path_with_spaces_never_reaches_the_command(self):
        diff = "diff --git a/tests/test_a;rm -rf x.py b/tests/test_a;rm -rf x.py\n@@ -1 +1 @@\n+x\n"
        block = gate_runner._review_tests_block(INTERP, diff)
        command = _command_line(block)
        assert ";" not in command and "rm -rf" not in command

    def test_leading_dash_path_is_refused_as_a_pytest_option(self):
        block = gate_runner._review_tests_block(INTERP, _hunk("-pevil_test.py"))
        assert "-pevil" not in _command_line(block)
        assert "left out of the command" in block

    def test_mixed_diff_keeps_the_safe_path_quoted(self):
        diff = _hunk("tests/test_ok.py") + _hunk("tests/test_$(id).py")
        block = gate_runner._review_tests_block(INTERP, diff)
        command = _command_line(block)
        self._assert_safe(command)
        assert _pytest("tests/test_ok.py") in block
        assert "test_$(id)" not in command
        assert '"tests/test_$(id).py"' in block

    def test_safe_paths_go_through_shlex_quote(self, monkeypatch):
        seen = []
        real = gate_runner.shlex.quote
        monkeypatch.setattr(gate_runner.shlex, "quote", lambda s: seen.append(s) or real(s))
        gate_runner._review_tests_block(INTERP, _hunk("tests/test_a.py") + _hunk("tests/test_b.py"))
        assert seen == ["tests/test_a.py", "tests/test_b.py"]

    def test_no_interpreter_still_names_the_refused_path_as_text(self):
        block = gate_runner._review_tests_block(None, _hunk("tests/test_$(id).py"))
        assert "-m pytest" not in block
        assert "left out of the command" in block


class TestPythonPathInTheCommand:
    """D1: VNX_REVIEW_PYTHONPATH entries go first in PYTHONPATH as absolute worktree paths."""

    def test_pythonpath_entries_are_absolute_inside_the_worktree_and_first(self):
        wt = Path("/gate/wt")
        block = gate_runner._review_tests_block(
            INTERP, _hunk("tests/test_a.py"), rule=2, worktree=wt, pythonpath=["src", "lib/x"])
        assert (f"`env -u PYTHONSAFEPATH PYTHONPATH=/gate/wt/src:/gate/wt/lib/x {INTERP} -m pytest tests/test_a.py "
                "-q -p no:cacheprovider`") in block
        assert "chosen by rule 2" in block

    def test_the_worktree_token_stands_in_until_the_spawn(self):
        token = gate_runner._request_worktree_token({})
        block = gate_runner._review_tests_block(INTERP, _hunk("tests/test_a.py"), pythonpath=["src"], worktree_token=token)
        assert f"PYTHONPATH={token}/src " in block

    def test_a_refused_entry_is_text_not_command(self):
        block = gate_runner._review_tests_block(
            INTERP, _hunk("tests/test_a.py"), worktree=Path("/gate/wt"), pythonpath=["src;id"])
        assert "PYTHONPATH=" not in block.split("PYTHONPATH entry left out")[0]
        assert '"src;id"' in block
