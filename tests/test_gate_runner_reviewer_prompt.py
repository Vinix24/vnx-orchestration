#!/usr/bin/env python3
"""tests/test_gate_runner_reviewer_prompt.py — Reviewer prompt assembly for gate_runner.

Validates Wave 4.5 PR-2b redo:
  - gh pr diff is used as the authoritative diff source (not _inline_file_contents)
  - Non-zero gh pr diff exit raises loudly (no silent empty-diff fallback)
  - Missing pr_number raises ValueError
  - Reviewer role context (from reviewer.md) is present in the assembled prompt
  - Verdict JSON template is preserved in both codex and gemini paths
  - Gemini path mirrors all codex constraints
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

from gate_runner import GateRunner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(pr_number=123, branch="feat/test-branch", risk_class="medium", **extra):
    return {
        "pr_number": pr_number,
        "branch": branch,
        "risk_class": risk_class,
        **extra,
    }


def _mock_gh_success(diff_text="diff --git a/foo.py b/foo.py\n+def bar(): pass\n"):
    return mock.Mock(returncode=0, stdout=diff_text, stderr="")


def _mock_gh_failure(returncode=1, stderr="error: no such PR"):
    return mock.Mock(returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# Codex path
# ---------------------------------------------------------------------------

class TestCodexUsesGhPrDiff:
    """gate_runner._build_codex_prompt must call gh pr diff, not local file reads."""

    def test_codex_uses_gh_pr_diff(self):
        payload = _make_payload(pr_number=123)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()) as mock_run:
            GateRunner._build_codex_prompt(payload)

        mock_run.assert_called_once_with(
            ["gh", "pr", "diff", "123"],
            capture_output=True, text=True, timeout=60,
        )

    def test_codex_diff_content_appears_in_prompt(self):
        diff_text = "diff --git a/scripts/gate_runner.py b/scripts/gate_runner.py\n+    new_line = 42\n"
        payload = _make_payload(pr_number=77)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff_text)):
            result = GateRunner._build_codex_prompt(payload)

        assert "new_line = 42" in result


class TestCodexFailsLoudOnGhDiffFailure:
    """gh pr diff failure must raise — never silently continue with empty diff."""

    def test_codex_fails_loud_on_gh_diff_failure(self):
        payload = _make_payload(pr_number=999)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_failure(1, "not found")):
            with pytest.raises(RuntimeError, match="gh pr diff 999 failed"):
                GateRunner._build_codex_prompt(payload)

    def test_codex_no_silent_empty_diff_fallback(self):
        payload = _make_payload(pr_number=456)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_failure(2, "auth error")):
            raised = False
            try:
                GateRunner._build_codex_prompt(payload)
            except (RuntimeError, ValueError):
                raised = True
            assert raised, "Expected exception on gh pr diff failure, not silent empty-diff fallback"

    def test_codex_fails_on_missing_pr_number(self):
        payload = _make_payload(pr_number=None)
        with pytest.raises(ValueError, match="pr_number is required"):
            GateRunner._build_codex_prompt(payload)

    def test_codex_fails_on_zero_pr_number(self):
        payload = _make_payload(pr_number=0)
        with pytest.raises(ValueError, match="pr_number is required"):
            GateRunner._build_codex_prompt(payload)


class TestCodexPromptContent:
    """Assembled codex prompt must contain reviewer role context and verdict template."""

    def test_codex_prompt_has_reviewer_role_context(self):
        payload = _make_payload(pr_number=42)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        # Distinctive content from reviewer.md
        assert "ADR-003" in result or "ADR-010" in result or "VNX governance" in result.lower()

    def test_codex_prompt_preserves_verdict_template(self):
        payload = _make_payload(pr_number=42)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        assert '"verdict"' in result
        assert '"findings"' in result
        assert '"out_of_scope"' in result
        assert '"introduced_by_prior_fix"' in result

    def test_codex_prompt_contains_branch_and_risk(self):
        payload = _make_payload(pr_number=55, branch="feat/my-feature", risk_class="high")
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        assert "feat/my-feature" in result
        assert "high" in result

    def test_codex_prompt_grounding_rule_present(self):
        payload = _make_payload(pr_number=88)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        assert "do not flag pre-existing code" in result


# ---------------------------------------------------------------------------
# Gemini path mirrors
# ---------------------------------------------------------------------------

class TestGeminiPathMirrors:
    """Gemini path must apply all the same constraints as the codex path."""

    def test_gemini_uses_gh_pr_diff(self):
        payload = _make_payload(pr_number=200)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()) as mock_run:
            GateRunner._build_gemini_prompt(payload)

        mock_run.assert_called_once_with(
            ["gh", "pr", "diff", "200"],
            capture_output=True, text=True, timeout=60,
        )

    def test_gemini_fails_loud_on_gh_diff_failure(self):
        payload = _make_payload(pr_number=201)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_failure(1, "pr not found")):
            with pytest.raises(RuntimeError, match="gh pr diff 201 failed"):
                GateRunner._build_gemini_prompt(payload)

    def test_gemini_no_silent_empty_diff_fallback(self):
        payload = _make_payload(pr_number=202)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_failure(1, "")):
            with pytest.raises((RuntimeError, ValueError)):
                GateRunner._build_gemini_prompt(payload)

    def test_gemini_fails_on_missing_pr_number(self):
        payload = _make_payload(pr_number=None)
        with pytest.raises(ValueError, match="pr_number is required"):
            GateRunner._build_gemini_prompt(payload)

    def test_gemini_prompt_preserves_verdict_template(self):
        payload = _make_payload(pr_number=203)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_gemini_prompt(payload)

        assert '"verdict"' in result
        assert '"findings"' in result
        assert '"out_of_scope"' in result
        assert '"introduced_by_prior_fix"' in result

    def test_gemini_prompt_has_reviewer_role_context(self):
        payload = _make_payload(pr_number=204)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_gemini_prompt(payload)

        assert "ADR-003" in result or "ADR-010" in result or "VNX governance" in result.lower()

    def test_gemini_returns_string(self):
        payload = _make_payload(pr_number=205)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_gemini_prompt(payload)

        assert isinstance(result, str)
        assert len(result) > 100


# ---------------------------------------------------------------------------
# _fetch_gh_pr_diff unit tests
# ---------------------------------------------------------------------------

class TestFetchGhPrDiff:
    """Direct unit tests for the shared _fetch_gh_pr_diff helper."""

    def test_returns_stdout_on_success(self):
        expected = "diff --git a/x.py b/x.py\n+line\n"
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=expected, stderr="")):
            result = GateRunner._fetch_gh_pr_diff(10)
        assert result == expected

    def test_raises_value_error_on_none_pr_number(self):
        with pytest.raises(ValueError):
            GateRunner._fetch_gh_pr_diff(None)

    def test_raises_runtime_error_on_nonzero_exit(self):
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=128, stdout="", stderr="fatal")):
            with pytest.raises(RuntimeError, match="exit 128"):
                GateRunner._fetch_gh_pr_diff(99)

    def test_calls_correct_gh_command(self):
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as m:
            GateRunner._fetch_gh_pr_diff(42)
        m.assert_called_once_with(
            ["gh", "pr", "diff", "42"],
            capture_output=True, text=True, timeout=60,
        )


# ---------------------------------------------------------------------------
# Net-deletion sanity check
# ---------------------------------------------------------------------------

def _make_deleted_file_diff(*paths: str) -> str:
    """Build a minimal unified diff with fully deleted files at the given paths."""
    parts = []
    for path in paths:
        parts.append(
            f"diff --git a/{path} b/{path}\n"
            f"deleted file mode 100644\n"
            f"index abc1234..0000000\n"
            f"--- a/{path}\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-# deleted content\n"
        )
    return "\n".join(parts)


def _make_modified_file_diff(path: str = "scripts/foo.py") -> str:
    """Build a minimal unified diff for a modified (not deleted) file."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 111aaa..222bbb 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,3 @@\n"
        " def existing(): pass\n"
        "+def new_func(): pass\n"
    )


class TestExtractDeletedFilesFromDiff:
    """Unit tests for _extract_deleted_files_from_diff."""

    def test_empty_diff_returns_empty_list(self):
        assert GateRunner._extract_deleted_files_from_diff("") == []

    def test_modified_file_not_included(self):
        diff = _make_modified_file_diff("scripts/active.py")
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == []

    def test_single_deleted_file_returned(self):
        diff = _make_deleted_file_diff("scripts/old_module.py")
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == ["scripts/old_module.py"]

    def test_multiple_deleted_files_all_returned(self):
        paths = [f"scripts/module_{i}.py" for i in range(3)]
        diff = _make_deleted_file_diff(*paths)
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == paths

    def test_mixed_diff_only_deleted_returned(self):
        """Modified files in the same diff must not appear in the deleted-file list."""
        deleted_diff = _make_deleted_file_diff("scripts/gone.py")
        modified_diff = _make_modified_file_diff("scripts/still_here.py")
        combined = deleted_diff + "\n" + modified_diff
        result = GateRunner._extract_deleted_files_from_diff(combined)
        assert "scripts/gone.py" in result
        assert "scripts/still_here.py" not in result

    def test_paths_without_b_prefix_are_skipped_gracefully(self):
        """Malformed diff headers (no b/ prefix) must not raise."""
        diff = "diff --git a/scripts/foo.py scripts/foo.py\ndeleted file mode 100644\n"
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert isinstance(result, list)


class TestBuildDeletionAlertSection:
    """Unit tests for _build_deletion_alert_section."""

    def test_below_threshold_returns_empty_string(self):
        """Fewer than _GATE_DELETION_FILE_WARN deletions → no alert."""
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN - 1)]
        diff = _make_deleted_file_diff(*paths) if paths else ""
        result = GateRunner._build_deletion_alert_section(diff)
        assert result == ""

    def test_at_threshold_returns_alert_block(self):
        """Exactly _GATE_DELETION_FILE_WARN deletions triggers the alert."""
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        result = GateRunner._build_deletion_alert_section(diff)
        assert "Net-Deletion Alert" in result

    def test_above_threshold_includes_count(self):
        """Count in alert header must match actual deleted-file count."""
        from gate_runner import _GATE_DELETION_FILE_WARN
        count = _GATE_DELETION_FILE_WARN + 3
        paths = [f"scripts/file_{i}.py" for i in range(count)]
        diff = _make_deleted_file_diff(*paths)
        result = GateRunner._build_deletion_alert_section(diff)
        assert f"{count} file(s) deleted" in result

    def test_alert_lists_deleted_file_paths(self):
        """Alert block must include every deleted file path."""
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/important_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        result = GateRunner._build_deletion_alert_section(diff)
        for path in paths:
            assert path in result

    def test_no_alert_for_empty_diff(self):
        assert GateRunner._build_deletion_alert_section("") == ""


class TestNetDeletionAlertInCodexPrompt:
    """_build_codex_prompt must inject the deletion alert when threshold is met."""

    def test_no_alert_below_threshold(self):
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN - 1)]
        diff = _make_deleted_file_diff(*paths) if paths else _make_modified_file_diff()
        payload = _make_payload(pr_number=10)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert "Net-Deletion Alert" not in result

    def test_alert_injected_at_threshold(self):
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        payload = _make_payload(pr_number=11)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert "Net-Deletion Alert" in result

    def test_alert_includes_deleted_file_names(self):
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/module_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        payload = _make_payload(pr_number=12)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        for path in paths:
            assert path in result

    def test_verdict_template_still_present_with_alert(self):
        """Verdict template must appear even when the deletion alert is injected."""
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        payload = _make_payload(pr_number=13)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert '"verdict"' in result
        assert '"findings"' in result


class TestNetDeletionAlertInGeminiPrompt:
    """_build_gemini_prompt must mirror the deletion alert behaviour of the codex path."""

    def test_no_alert_below_threshold(self):
        from gate_runner import _GATE_DELETION_FILE_WARN
        diff = _make_modified_file_diff()
        payload = _make_payload(pr_number=20)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_gemini_prompt(payload)
        assert "Net-Deletion Alert" not in result

    def test_alert_injected_at_threshold(self):
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        payload = _make_payload(pr_number=21)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_gemini_prompt(payload)
        assert "Net-Deletion Alert" in result

    def test_alert_includes_deleted_file_names(self):
        from gate_runner import _GATE_DELETION_FILE_WARN
        paths = [f"scripts/removed_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _make_deleted_file_diff(*paths)
        payload = _make_payload(pr_number=22)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_gemini_prompt(payload)
        for path in paths:
            assert path in result
