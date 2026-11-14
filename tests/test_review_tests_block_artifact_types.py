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
        monkeypatch.setattr(gate_runner, "resolve_review_interpreter", lambda: INTERP)
        diff = _hunk("tests/scripts/gate_report.test.ts")
        gh_ok = mock.Mock(returncode=0, stdout=diff, stderr="")
        with mock.patch("gate_runner.subprocess.run", return_value=gh_ok):
            prompt = GateRunner._build_codex_prompt(
                {"pr_number": 4242, "branch": "feat/ts", "risk_class": "medium"})
        assert "-m pytest" not in prompt
        assert _no_command(".ts") in prompt
        assert NEVER_BLOCKING in prompt
