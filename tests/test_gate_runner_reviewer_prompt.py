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

    def test_codex_prompt_has_net_deletion_sanity_instruction(self):
        payload = _make_payload(pr_number=88)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        assert "Net deletion sanity" in result

    def test_codex_prompt_net_deletion_includes_threshold(self):
        payload = _make_payload(pr_number=88)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        assert "≥5" in result

    def test_codex_prompt_net_deletion_mentions_intentional(self):
        payload = _make_payload(pr_number=88)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_codex_prompt(payload)

        assert "intentional" in result


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

    def test_gemini_prompt_has_net_deletion_sanity_instruction(self):
        payload = _make_payload(pr_number=205)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_gemini_prompt(payload)

        assert "Net deletion sanity" in result

    def test_gemini_prompt_net_deletion_includes_threshold(self):
        payload = _make_payload(pr_number=205)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_gemini_prompt(payload)

        assert "≥5" in result

    def test_gemini_net_deletion_mentions_scope_reduction(self):
        payload = _make_payload(pr_number=205)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success()):
            result = GateRunner._build_gemini_prompt(payload)

        assert "scope reduction" in result


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
