#!/usr/bin/env python3
"""Unit tests for GateRunner net-deletion sanity checks.

Covers the three new static helpers added in gate_runner.py:
  - _extract_deleted_files_from_diff
  - _build_deletion_alert_section
  - Alert injection in _build_codex_prompt and _build_gemini_prompt

Threshold constant: _GATE_DELETION_FILE_WARN = 5
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

import json
import tempfile

from gate_runner import GateRunner, _GATE_DELETION_FILE_WARN, _GATE_DELETION_FILE_HOLD  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _diff_deleted(*paths: str) -> str:
    """Build a minimal unified diff that marks each path as a deleted file."""
    lines = []
    for p in paths:
        lines += [
            f"diff --git a/{p} b/{p}",
            "deleted file mode 100644",
            "index abc123..0000000",
            f"--- a/{p}",
            "+++ /dev/null",
            "@@ -1,3 +0,0 @@",
            "-content",
        ]
    return "\n".join(lines)


def _diff_modified(path: str) -> str:
    """Build a minimal unified diff for a modified (not deleted) file."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "index abc123..def456 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _mock_gh_success(diff_text: str) -> mock.Mock:
    return mock.Mock(returncode=0, stdout=diff_text, stderr="")


# ---------------------------------------------------------------------------
# _extract_deleted_files_from_diff
# ---------------------------------------------------------------------------

class TestExtractDeletedFilesFromDiff:

    def test_empty_diff_returns_empty_list(self):
        assert GateRunner._extract_deleted_files_from_diff("") == []

    def test_single_deleted_file_returned(self):
        diff = _diff_deleted("scripts/old.py")
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == ["scripts/old.py"]

    def test_multiple_deleted_files_all_returned(self):
        paths = [f"old/file_{i}.py" for i in range(6)]
        diff = _diff_deleted(*paths)
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == paths

    def test_modified_file_not_included(self):
        diff = _diff_modified("scripts/active.py")
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == []

    def test_mixed_deleted_and_modified(self):
        diff = _diff_deleted("old/remove_me.py") + "\n" + _diff_modified("src/keep.py")
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == ["old/remove_me.py"]

    def test_deleted_file_mode_without_preceding_diff_header_ignored(self):
        """A stray 'deleted file mode' line with no preceding diff --git is ignored."""
        diff = "deleted file mode 100644\nsome other content\n"
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == []

    def test_diff_header_with_no_b_prefix_ignored(self):
        """Malformed diff --git line without b/ prefix is skipped gracefully."""
        diff = "diff --git a/foo.py\ndeleted file mode 100644\n"
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == []

    def test_preserves_nested_paths(self):
        diff = _diff_deleted("scripts/lib/deep/module.py")
        result = GateRunner._extract_deleted_files_from_diff(diff)
        assert result == ["scripts/lib/deep/module.py"]


# ---------------------------------------------------------------------------
# _build_deletion_alert_section
# ---------------------------------------------------------------------------

class TestBuildDeletionAlertSection:

    def test_below_threshold_returns_empty_string(self):
        """Fewer than _GATE_DELETION_FILE_WARN deletions → no alert."""
        count = _GATE_DELETION_FILE_WARN - 1
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        result = GateRunner._build_deletion_alert_section(diff)
        assert result == ""

    def test_at_threshold_returns_alert(self):
        """Exactly _GATE_DELETION_FILE_WARN deletions → alert injected."""
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        result = GateRunner._build_deletion_alert_section(diff)
        assert result != ""
        assert "## Net-Deletion Alert" in result

    def test_above_threshold_returns_alert(self):
        """More than _GATE_DELETION_FILE_WARN deletions → alert injected."""
        count = _GATE_DELETION_FILE_WARN + 3
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        result = GateRunner._build_deletion_alert_section(diff)
        assert "## Net-Deletion Alert" in result

    def test_alert_includes_file_count(self):
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        result = GateRunner._build_deletion_alert_section(diff)
        assert str(count) in result

    def test_alert_lists_each_deleted_path(self):
        paths = [f"old/file_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _diff_deleted(*paths)
        result = GateRunner._build_deletion_alert_section(diff)
        for p in paths:
            assert f"`{p}`" in result

    def test_alert_contains_review_required_note(self):
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        result = GateRunner._build_deletion_alert_section(diff)
        assert "Review required" in result

    def test_no_deletions_returns_empty_string(self):
        diff = _diff_modified("src/main.py")
        result = GateRunner._build_deletion_alert_section(diff)
        assert result == ""

    def test_zero_files_empty_diff_returns_empty_string(self):
        assert GateRunner._build_deletion_alert_section("") == ""


# ---------------------------------------------------------------------------
# _build_codex_prompt: alert injection
# ---------------------------------------------------------------------------

class TestCodexPromptDeletionAlertInjection:

    def _payload(self, pr_number=42, branch="feat/test", risk_class="medium"):
        return {"pr_number": pr_number, "branch": branch, "risk_class": risk_class}

    def test_alert_absent_when_below_threshold(self):
        """< _GATE_DELETION_FILE_WARN deletions → no alert in codex prompt."""
        count = _GATE_DELETION_FILE_WARN - 1
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_codex_prompt(self._payload())
        assert "## Net-Deletion Alert" not in result

    def test_alert_present_at_threshold(self):
        """= _GATE_DELETION_FILE_WARN deletions → alert injected in codex prompt."""
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_codex_prompt(self._payload())
        assert "## Net-Deletion Alert" in result

    def test_alert_present_above_threshold(self):
        """>> _GATE_DELETION_FILE_WARN deletions → alert injected in codex prompt."""
        count = _GATE_DELETION_FILE_WARN + 10
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_codex_prompt(self._payload())
        assert "## Net-Deletion Alert" in result

    def test_alert_lists_deleted_paths_in_codex_prompt(self):
        """Deleted file paths appear verbatim inside the codex prompt."""
        paths = [f"old/module_{i}.py" for i in range(_GATE_DELETION_FILE_WARN)]
        diff = _diff_deleted(*paths)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_codex_prompt(self._payload())
        for p in paths:
            assert f"`{p}`" in result

    def test_verdict_template_still_present_with_alert(self):
        """Verdict JSON template must not be displaced by the deletion alert."""
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_codex_prompt(self._payload())
        assert '"verdict"' in result
        assert '"findings"' in result


# ---------------------------------------------------------------------------
# _build_gemini_prompt: alert injection mirrors codex behaviour
# ---------------------------------------------------------------------------

class TestGeminiPromptDeletionAlertInjection:

    def _payload(self, pr_number=42, branch="feat/test", risk_class="medium"):
        return {"pr_number": pr_number, "branch": branch, "risk_class": risk_class}

    def test_alert_absent_when_below_threshold(self):
        count = _GATE_DELETION_FILE_WARN - 1
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_gemini_prompt(self._payload())
        assert "## Net-Deletion Alert" not in result

    def test_alert_present_at_threshold(self):
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_gemini_prompt(self._payload())
        assert "## Net-Deletion Alert" in result

    def test_alert_present_above_threshold(self):
        count = _GATE_DELETION_FILE_WARN + 7
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_gemini_prompt(self._payload())
        assert "## Net-Deletion Alert" in result

    def test_verdict_template_still_present_with_alert(self):
        count = _GATE_DELETION_FILE_WARN
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._build_gemini_prompt(self._payload())
        assert '"verdict"' in result
        assert '"findings"' in result

    def test_gemini_alert_count_matches_codex_alert_count(self):
        """Gemini and codex paths must produce the same alert when diff is identical."""
        count = _GATE_DELETION_FILE_WARN + 2
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            gemini_result = GateRunner._build_gemini_prompt(self._payload())
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            codex_result = GateRunner._build_codex_prompt(self._payload())
        # Both must contain the same file count in the heading
        heading = f"## Net-Deletion Alert ({count} file(s) deleted)"
        assert heading in gemini_result
        assert heading in codex_result


# ---------------------------------------------------------------------------
# _run_codex_net_deletion_check: deterministic blocking gate (HOLD threshold)
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheck:
    """Tests for the pre-AI HOLD check that short-circuits when >= _GATE_DELETION_FILE_HOLD
    files are deleted.  Uses tmp dirs to verify filesystem side effects."""

    def _dirs(self, tmp_path: Path) -> tuple:
        results = tmp_path / "results"
        requests = tmp_path / "requests"
        results.mkdir()
        requests.mkdir()
        return results, requests

    def _payload(self, pr_number=42, pr_id=""):
        return {"gate": "codex_gate", "pr_id": pr_id, "pr_number": pr_number}

    # --- no-op / passthrough cases ---

    def test_none_pr_number_returns_none(self, tmp_path):
        results, requests = self._dirs(tmp_path)
        result = GateRunner._run_codex_net_deletion_check(
            pr_number=None,
            request_payload=self._payload(pr_number=None),
            results_dir=results,
            requests_dir=requests,
        )
        assert result is None

    def test_below_hold_threshold_returns_none(self, tmp_path):
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD - 1
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is None

    def test_gh_pr_diff_failure_degrades_gracefully(self, tmp_path):
        """RuntimeError from gh pr diff → None, AI gate proceeds."""
        results, requests = self._dirs(tmp_path)
        failing = mock.Mock(returncode=1, stdout="", stderr="authentication required")
        with mock.patch("gate_runner.subprocess.run", return_value=failing):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is None

    # --- HOLD triggers ---

    def test_at_hold_threshold_returns_fail_result(self, tmp_path):
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is not None
        assert result["status"] == "fail"

    def test_above_hold_threshold_returns_fail_result(self, tmp_path):
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD + 5
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["status"] == "fail"
        assert result["net_deletion_check"]["triggered"] is True
        assert result["net_deletion_check"]["deleted_count"] == count

    def test_result_contains_blocking_finding(self, tmp_path):
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert len(result["blocking_findings"]) == 1
        assert result["blocking_findings"][0]["severity"] == "blocking"

    def test_result_lists_all_deleted_paths(self, tmp_path):
        results, requests = self._dirs(tmp_path)
        paths = [f"old/module_{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)]
        diff = _diff_deleted(*paths)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        check = result["net_deletion_check"]
        assert check["deleted_files"] == paths

    def test_result_file_written_to_disk(self, tmp_path):
        """HOLD result is persisted to results_dir as JSON."""
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(pr_number=42),
                results_dir=results,
                requests_dir=requests,
            )
        written = list(results.iterdir())
        assert len(written) == 1
        data = json.loads(written[0].read_text())
        assert data["status"] == "fail"

    def test_request_payload_status_set_to_completed(self, tmp_path):
        """After HOLD, request_payload['status'] is mutated to 'completed'."""
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        payload = self._payload()
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=payload,
                results_dir=results,
                requests_dir=requests,
            )
        assert payload["status"] == "completed"
        assert "completed_at" in payload

    def test_threshold_value_reflected_in_result(self, tmp_path):
        """net_deletion_check.threshold must equal _GATE_DELETION_FILE_HOLD."""
        results, requests = self._dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=self._payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["net_deletion_check"]["threshold"] == _GATE_DELETION_FILE_HOLD
