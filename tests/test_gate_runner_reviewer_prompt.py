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

from gate_runner import (  # noqa: E402
    GateRunner,
    DELETION_FILE_WARN,
    _extract_deleted_files_from_diff,
    _format_net_deletion_alert,
    _build_deletion_prefix,
)


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
# Net-deletion sanity check helpers
# ---------------------------------------------------------------------------

def _make_deleted_file_diff(paths: list) -> str:
    """Build a synthetic unified diff that deletes the given file paths."""
    chunks = []
    for p in paths:
        chunks.append(
            f"diff --git a/{p} b/{p}\n"
            "deleted file mode 100644\n"
            "index abc123..0000000\n"
            f"--- a/{p}\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-line1\n"
            "-line2\n"
            "-line3\n"
        )
    return "\n".join(chunks)


class TestExtractDeletedFilesFromDiff:
    """_extract_deleted_files_from_diff must parse deleted files from a unified diff."""

    def test_empty_diff_returns_empty(self):
        assert _extract_deleted_files_from_diff("") == []

    def test_added_only_diff_returns_empty(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "new file mode 100644\n"
            "index 0000000..abc123\n"
            "--- /dev/null\n"
            "+++ b/foo.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line\n"
        )
        assert _extract_deleted_files_from_diff(diff) == []

    def test_modified_file_not_counted(self):
        diff = (
            "diff --git a/bar.py b/bar.py\n"
            "index abc..def 100644\n"
            "--- a/bar.py\n"
            "+++ b/bar.py\n"
            "@@ -1,2 +1,3 @@\n"
            " existing\n"
            "+added\n"
        )
        assert _extract_deleted_files_from_diff(diff) == []

    def test_single_deleted_file(self):
        diff = _make_deleted_file_diff(["scripts/old.py"])
        result = _extract_deleted_files_from_diff(diff)
        assert result == ["scripts/old.py"]

    def test_multiple_deleted_files(self):
        paths = [f"old/file_{i}.py" for i in range(3)]
        diff = _make_deleted_file_diff(paths)
        result = _extract_deleted_files_from_diff(diff)
        assert result == paths

    def test_mixed_diff_counts_only_deleted(self):
        deleted = _make_deleted_file_diff(["old/a.py", "old/b.py"])
        added = (
            "diff --git a/new/c.py b/new/c.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new/c.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+new\n"
        )
        result = _extract_deleted_files_from_diff(deleted + "\n" + added)
        assert result == ["old/a.py", "old/b.py"]

    def test_path_without_a_prefix_handled(self):
        diff = (
            "diff --git scripts/x.py scripts/x.py\n"
            "deleted file mode 100644\n"
        )
        result = _extract_deleted_files_from_diff(diff)
        # parts[2] = "scripts/x.py" — no "a/" prefix, returned as-is
        assert result == ["scripts/x.py"]


class TestFormatNetDeletionAlert:
    """_format_net_deletion_alert must produce a well-structured alert block."""

    def test_includes_file_count_in_header(self):
        alert = _format_net_deletion_alert(["a.py", "b.py", "c.py", "d.py", "e.py"])
        assert "5 file(s)" in alert

    def test_lists_each_file(self):
        files = ["old/foo.py", "old/bar.py"]
        alert = _format_net_deletion_alert(files)
        for f in files:
            assert f"`{f}`" in alert

    def test_includes_sanity_instruction(self):
        alert = _format_net_deletion_alert(["x.py"])
        assert "Net deletion sanity" in alert or "intentional" in alert

    def test_includes_warn_label(self):
        alert = _format_net_deletion_alert(["x.py"])
        assert "WARN" in alert or "Net-Deletion Alert" in alert


class TestBuildDeletionPrefix:
    """_build_deletion_prefix must return empty string below threshold and alert above."""

    def test_below_threshold_returns_empty(self):
        diff = _make_deleted_file_diff([f"f{i}.py" for i in range(DELETION_FILE_WARN - 1)])
        assert _build_deletion_prefix(diff) == ""

    def test_at_threshold_returns_alert(self):
        diff = _make_deleted_file_diff([f"f{i}.py" for i in range(DELETION_FILE_WARN)])
        prefix = _build_deletion_prefix(diff)
        assert "Net-Deletion Alert" in prefix

    def test_above_threshold_returns_alert(self):
        diff = _make_deleted_file_diff([f"f{i}.py" for i in range(DELETION_FILE_WARN + 3)])
        prefix = _build_deletion_prefix(diff)
        assert "Net-Deletion Alert" in prefix
        assert f"{DELETION_FILE_WARN + 3} file(s)" in prefix

    def test_empty_diff_returns_empty(self):
        assert _build_deletion_prefix("") == ""

    def test_no_deletions_returns_empty(self):
        diff = "diff --git a/foo.py b/foo.py\nindex abc..def 100644\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1,2 @@\n+added\n"
        assert _build_deletion_prefix(diff) == ""


class TestNetDeletionInjectedIntoCodexPrompt:
    """_build_codex_prompt must include Net-Deletion Alert when >= DELETION_FILE_WARN files deleted."""

    def _diff_with_n_deletions(self, n: int) -> str:
        return _make_deleted_file_diff([f"old/file_{i}.py" for i in range(n)])

    def test_no_alert_below_threshold(self):
        diff = self._diff_with_n_deletions(DELETION_FILE_WARN - 1)
        payload = {"pr_number": 1, "branch": "feat/x", "risk_class": "medium"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert "Net-Deletion Alert" not in result

    def test_alert_injected_at_threshold(self):
        diff = self._diff_with_n_deletions(DELETION_FILE_WARN)
        payload = {"pr_number": 2, "branch": "feat/x", "risk_class": "medium"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert "Net-Deletion Alert" in result

    def test_alert_injected_above_threshold(self):
        diff = self._diff_with_n_deletions(DELETION_FILE_WARN + 5)
        payload = {"pr_number": 3, "branch": "feat/x", "risk_class": "high"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert "Net-Deletion Alert" in result
        assert f"{DELETION_FILE_WARN + 5} file(s)" in result

    def test_deleted_file_paths_listed_in_prompt(self):
        diff = self._diff_with_n_deletions(DELETION_FILE_WARN)
        payload = {"pr_number": 4, "branch": "feat/x", "risk_class": "low"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_codex_prompt(payload)
        assert "`old/file_0.py`" in result


class TestNetDeletionInjectedIntoGeminiPrompt:
    """_build_gemini_prompt must apply the same net-deletion check as the codex path."""

    def _diff_with_n_deletions(self, n: int) -> str:
        return _make_deleted_file_diff([f"old/file_{i}.py" for i in range(n)])

    def test_no_alert_below_threshold(self):
        diff = self._diff_with_n_deletions(DELETION_FILE_WARN - 1)
        payload = {"pr_number": 10, "branch": "feat/x", "risk_class": "medium"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_gemini_prompt(payload)
        assert "Net-Deletion Alert" not in result

    def test_alert_injected_at_threshold(self):
        diff = self._diff_with_n_deletions(DELETION_FILE_WARN)
        payload = {"pr_number": 11, "branch": "feat/x", "risk_class": "medium"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff, stderr="")):
            result = GateRunner._build_gemini_prompt(payload)
        assert "Net-Deletion Alert" in result

    def test_gemini_and_codex_agree_on_threshold(self):
        """Both paths must trigger at the same DELETION_FILE_WARN count."""
        diff_below = self._diff_with_n_deletions(DELETION_FILE_WARN - 1)
        diff_at = self._diff_with_n_deletions(DELETION_FILE_WARN)
        payload = {"pr_number": 12, "branch": "feat/x", "risk_class": "medium"}
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff_below, stderr="")):
            codex_below = GateRunner._build_codex_prompt(payload)
            gemini_below = GateRunner._build_gemini_prompt(payload)
        with mock.patch("gate_runner.subprocess.run", return_value=mock.Mock(returncode=0, stdout=diff_at, stderr="")):
            codex_at = GateRunner._build_codex_prompt(payload)
            gemini_at = GateRunner._build_gemini_prompt(payload)

        assert ("Net-Deletion Alert" in codex_below) == ("Net-Deletion Alert" in gemini_below)
        assert ("Net-Deletion Alert" in codex_at) == ("Net-Deletion Alert" in gemini_at)
        assert "Net-Deletion Alert" not in codex_below
        assert "Net-Deletion Alert" in codex_at
