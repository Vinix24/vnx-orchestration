"""Regression tests for OI-1573: cmd_doctor structural extraction.

Verifies that the two helper functions (_doctor_check_settings,
_doctor_check_worktree) exist in doctor.sh and that cmd_doctor calls them.
Also guards the size constraint: cmd_doctor must stay ≤ 190 lines.

These are static analysis tests — they run grep/line-count on the bash source,
which is valid here because the objective is structural (not behavioural)
correctness and the bash code is a legacy fallback that cannot be unit-tested
in isolation without a full VNX environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
DOCTOR_SH = REPO_ROOT / "scripts" / "commands" / "doctor.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source_text() -> str:
    assert DOCTOR_SH.exists(), f"doctor.sh not found: {DOCTOR_SH}"
    return DOCTOR_SH.read_text()


def _cmd_doctor_body(text: str) -> list[str]:
    """Extract lines belonging to the cmd_doctor() body (between { and closing }).

    The opening { is on the same line as cmd_doctor() in this file, so we
    seed depth from that line rather than starting at 0.
    """
    lines = text.splitlines()
    in_func = False
    depth = 0
    body: list[str] = []
    for line in lines:
        if not in_func:
            if line.startswith("cmd_doctor()"):
                in_func = True
                # Count braces on the declaration line itself (handles "cmd_doctor() {")
                depth = line.count("{") - line.count("}")
            continue
        body.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and body:
            break
    return body


# ---------------------------------------------------------------------------
# Test 1: helper functions are defined at module level
# ---------------------------------------------------------------------------

class TestHelperFunctionsDefined:
    def test_doctor_check_settings_defined(self):
        text = _source_text()
        assert "_doctor_check_settings()" in text, (
            "_doctor_check_settings() function not found in doctor.sh. "
            "OI-1573 extraction may have been reverted."
        )

    def test_doctor_check_worktree_defined(self):
        text = _source_text()
        assert "_doctor_check_worktree()" in text, (
            "_doctor_check_worktree() function not found in doctor.sh. "
            "OI-1573 extraction may have been reverted."
        )

    def test_helpers_defined_before_cmd_doctor(self):
        """Both helpers must be declared before cmd_doctor uses them."""
        text = _source_text()
        pos_settings = text.find("_doctor_check_settings()")
        pos_worktree = text.find("_doctor_check_worktree()")
        pos_cmd = text.find("cmd_doctor()")
        assert pos_settings < pos_cmd, (
            "_doctor_check_settings() must be defined before cmd_doctor()"
        )
        assert pos_worktree < pos_cmd, (
            "_doctor_check_worktree() must be defined before cmd_doctor()"
        )


# ---------------------------------------------------------------------------
# Test 2: cmd_doctor delegates to the helpers
# ---------------------------------------------------------------------------

class TestCmdDoctorDelegates:
    def test_cmd_doctor_calls_check_settings(self):
        text = _source_text()
        body = "\n".join(_cmd_doctor_body(text))
        assert "_doctor_check_settings" in body, (
            "cmd_doctor must call _doctor_check_settings. "
            "The delegation introduced by OI-1573 is missing."
        )

    def test_cmd_doctor_calls_check_worktree(self):
        text = _source_text()
        body = "\n".join(_cmd_doctor_body(text))
        assert "_doctor_check_worktree" in body, (
            "cmd_doctor must call _doctor_check_worktree. "
            "The delegation introduced by OI-1573 is missing."
        )

    def test_cmd_doctor_settings_propagates_failure(self):
        """cmd_doctor must fail-propagate the settings check result."""
        text = _source_text()
        body = "\n".join(_cmd_doctor_body(text))
        assert "_doctor_check_settings || failed=1" in body, (
            "cmd_doctor must propagate _doctor_check_settings failure via '|| failed=1'."
        )

    def test_inline_settings_block_removed_from_cmd_doctor(self):
        """The inlined settings block must no longer live inside cmd_doctor."""
        text = _source_text()
        body = "\n".join(_cmd_doctor_body(text))
        assert 'local settings_file=' not in body, (
            "'local settings_file=' still present inside cmd_doctor. "
            "The settings block was not extracted — check OI-1573 implementation."
        )

    def test_inline_worktree_block_removed_from_cmd_doctor(self):
        """The inlined worktree block must no longer live inside cmd_doctor."""
        text = _source_text()
        body = "\n".join(_cmd_doctor_body(text))
        assert 'local wt_data=' not in body, (
            "'local wt_data=' still present inside cmd_doctor. "
            "The worktree block was not extracted — check OI-1573 implementation."
        )


# ---------------------------------------------------------------------------
# Test 3: cmd_doctor size constraint
# ---------------------------------------------------------------------------

class TestCmdDoctorSize:
    def test_cmd_doctor_at_most_190_lines(self):
        text = _source_text()
        body = _cmd_doctor_body(text)
        line_count = len(body)
        assert line_count <= 190, (
            f"cmd_doctor is {line_count} lines — exceeds the 190-line target. "
            "OI-1573 extraction must keep cmd_doctor compact."
        )


# ---------------------------------------------------------------------------
# Test 4: bash syntax still passes after extraction
# ---------------------------------------------------------------------------

class TestBashSyntax:
    def test_doctor_sh_passes_bash_syntax_check(self):
        import subprocess
        result = subprocess.run(
            ["bash", "-n", str(DOCTOR_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n failed on doctor.sh after OI-1573 extraction.\n"
            f"stderr: {result.stderr}"
        )
