#!/usr/bin/env python3
"""Unit tests for GateRunner._run_codex_net_deletion_check.

Covers the pre-emptive deterministic check that blocks codex_gate when a PR
deletes >= _GATE_DELETION_FILE_HOLD files, without spawning the AI reviewer.

Test classes:
  - TestRunCodexNetDeletionCheckNoTrigger: returns None (AI gate proceeds)
  - TestRunCodexNetDeletionCheckTriggered: returns blocking result dict
  - TestRunCodexNetDeletionCheckResultShape: validates result structure
  - TestRunCodexNetDeletionCheckPersistence: verifies disk write + request completion
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_runner import GateRunner, _GATE_DELETION_FILE_HOLD, _GATE_DELETION_FILE_WARN  # noqa: E402


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
            "@@ -1,1 +0,0 @@",
            "-content",
        ]
    return "\n".join(lines)


def _mock_gh_success(diff_text: str) -> mock.Mock:
    return mock.Mock(returncode=0, stdout=diff_text, stderr="")


def _payload(pr_number=42, pr_id="PR-001", gate="codex_gate"):
    return {"pr_number": pr_number, "pr_id": pr_id, "gate": gate}


# ---------------------------------------------------------------------------
# Returns None (AI gate proceeds)
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckNoTrigger:
    """_run_codex_net_deletion_check must return None when it should not block."""

    def test_returns_none_when_pr_number_is_none(self, tmp_path):
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        result = GateRunner._run_codex_net_deletion_check(
            pr_number=None,
            request_payload=_payload(pr_number=None),
            results_dir=results_dir,
            requests_dir=requests_dir,
        )
        assert result is None

    def test_returns_none_when_pr_number_is_zero(self, tmp_path):
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        result = GateRunner._run_codex_net_deletion_check(
            pr_number=0,
            request_payload=_payload(pr_number=0),
            results_dir=results_dir,
            requests_dir=requests_dir,
        )
        assert result is None

    def test_returns_none_when_diff_fetch_raises_value_error(self, tmp_path):
        """ValueError from _fetch_gh_pr_diff → graceful degradation (None)."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        with mock.patch.object(
            GateRunner, "_fetch_gh_pr_diff", side_effect=ValueError("no pr_number"),
        ):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert result is None

    def test_returns_none_when_diff_fetch_raises_runtime_error(self, tmp_path):
        """RuntimeError from gh pr diff → graceful degradation (None)."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        with mock.patch.object(
            GateRunner, "_fetch_gh_pr_diff", side_effect=RuntimeError("gh pr diff failed"),
        ):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert result is None

    def test_returns_none_when_deleted_count_below_hold(self, tmp_path):
        """< _GATE_DELETION_FILE_HOLD deletions → None (let AI gate run)."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        count = _GATE_DELETION_FILE_HOLD - 1
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert result is None

    def test_returns_none_when_no_deletions(self, tmp_path):
        """Empty diff (no deletions) → None."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success("")):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert result is None

    def test_returns_none_at_warn_level_not_hold(self, tmp_path):
        """WARN level (< HOLD) must still return None — only HOLD triggers block."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        count = _GATE_DELETION_FILE_WARN + 1  # above WARN, below HOLD
        assert count < _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert result is None


# ---------------------------------------------------------------------------
# Returns blocking result (HOLD triggered)
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckTriggered:
    """_run_codex_net_deletion_check returns a non-None blocking result at HOLD threshold."""

    def _run_at_hold(self, tmp_path, count=None):
        if count is None:
            count = _GATE_DELETION_FILE_HOLD
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True)
        requests_dir.mkdir(parents=True)
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            return GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )

    def test_returns_non_none_at_hold_threshold(self, tmp_path):
        result = self._run_at_hold(tmp_path, count=_GATE_DELETION_FILE_HOLD)
        assert result is not None

    def test_returns_non_none_above_hold_threshold(self, tmp_path):
        result = self._run_at_hold(tmp_path, count=_GATE_DELETION_FILE_HOLD + 5)
        assert result is not None

    def test_status_is_fail(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["status"] == "fail"

    def test_findings_non_empty(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert len(result["findings"]) > 0

    def test_blocking_findings_non_empty(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert len(result["blocking_findings"]) > 0

    def test_finding_severity_is_blocking(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["findings"][0]["severity"] == "blocking"

    def test_summary_mentions_deleted_count(self, tmp_path):
        count = _GATE_DELETION_FILE_HOLD + 3
        result = self._run_at_hold(tmp_path, count=count)
        assert str(count) in result["summary"]

    def test_net_deletion_check_field_present(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert "net_deletion_check" in result

    def test_net_deletion_check_triggered_true(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["net_deletion_check"]["triggered"] is True

    def test_net_deletion_check_threshold_matches_constant(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["net_deletion_check"]["threshold"] == _GATE_DELETION_FILE_HOLD

    def test_net_deletion_check_count_matches_diff(self, tmp_path):
        count = _GATE_DELETION_FILE_HOLD + 7
        result = self._run_at_hold(tmp_path, count=count)
        assert result["net_deletion_check"]["deleted_count"] == count

    def test_deleted_files_listed_in_net_deletion_check(self, tmp_path):
        count = _GATE_DELETION_FILE_HOLD
        result = self._run_at_hold(tmp_path, count=count)
        assert len(result["net_deletion_check"]["deleted_files"]) == count

    def test_description_lists_deleted_files(self, tmp_path):
        result = self._run_at_hold(tmp_path, count=_GATE_DELETION_FILE_HOLD)
        description = result["findings"][0]["description"]
        # At least one deleted file path must appear
        assert "old/file_0.py" in description

    def test_advisory_findings_empty(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["advisory_findings"] == []

    def test_required_reruns_empty(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["required_reruns"] == []

    def test_finding_out_of_scope_false(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["findings"][0]["out_of_scope"] is False

    def test_finding_introduced_by_prior_fix_false(self, tmp_path):
        result = self._run_at_hold(tmp_path)
        assert result["findings"][0]["introduced_by_prior_fix"] is False


# ---------------------------------------------------------------------------
# Result shape (required keys)
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckResultShape:
    """Triggered result must carry all required downstream keys."""

    REQUIRED_KEYS = (
        "gate", "pr_id", "pr_number", "status", "summary",
        "findings", "blocking_findings", "advisory_findings",
        "required_reruns", "residual_risk", "duration_seconds",
        "recorded_at", "net_deletion_check",
    )

    def _triggered_result(self, tmp_path) -> dict:
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True)
        requests_dir.mkdir(parents=True)
        diff = _diff_deleted(*[f"old/f_{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            return GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )

    def test_all_required_keys_present(self, tmp_path):
        result = self._triggered_result(tmp_path)
        for key in self.REQUIRED_KEYS:
            assert key in result, f"missing key: {key}"

    def test_pr_number_preserved(self, tmp_path):
        result = self._triggered_result(tmp_path)
        assert result["pr_number"] == 42

    def test_pr_id_from_payload(self, tmp_path):
        result = self._triggered_result(tmp_path)
        assert result["pr_id"] == "PR-001"

    def test_gate_from_payload(self, tmp_path):
        result = self._triggered_result(tmp_path)
        assert result["gate"] == "codex_gate"

    def test_duration_seconds_is_zero(self, tmp_path):
        """Pre-emptive check skips subprocess → duration is 0.0."""
        result = self._triggered_result(tmp_path)
        assert result["duration_seconds"] == 0.0

    def test_recorded_at_is_iso_string(self, tmp_path):
        result = self._triggered_result(tmp_path)
        ts = result["recorded_at"]
        assert isinstance(ts, str)
        assert "T" in ts  # basic ISO-8601 shape check

    def test_net_deletion_check_has_required_fields(self, tmp_path):
        result = self._triggered_result(tmp_path)
        ndc = result["net_deletion_check"]
        for field in ("deleted_count", "deleted_files", "threshold", "triggered"):
            assert field in ndc, f"net_deletion_check missing field: {field}"


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckPersistence:
    """Triggered check must persist result file and mark request as completed."""

    def _run(self, tmp_path, pr_number=42, pr_id="PR-001", count=None):
        if count is None:
            count = _GATE_DELETION_FILE_HOLD
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True)
        requests_dir.mkdir(parents=True)
        diff = _diff_deleted(*[f"old/f_{i}.py" for i in range(count)])
        payload = _payload(pr_number=pr_number, pr_id=pr_id)
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=pr_number,
                request_payload=payload,
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        return result, payload, results_dir, requests_dir

    def test_result_file_written_to_disk(self, tmp_path):
        _, _, results_dir, _ = self._run(tmp_path)
        # At least one file should exist in results_dir
        result_files = list(results_dir.iterdir())
        assert len(result_files) > 0, "no result file written to results_dir"

    def test_result_file_is_valid_json(self, tmp_path):
        _, _, results_dir, _ = self._run(tmp_path)
        result_files = list(results_dir.iterdir())
        for f in result_files:
            parsed = json.loads(f.read_text(encoding="utf-8"))
            assert "status" in parsed

    def test_result_file_status_is_fail(self, tmp_path):
        _, _, results_dir, _ = self._run(tmp_path)
        result_files = list(results_dir.iterdir())
        parsed = json.loads(result_files[0].read_text(encoding="utf-8"))
        assert parsed["status"] == "fail"

    def test_request_file_written_to_disk(self, tmp_path):
        _, payload, _, requests_dir = self._run(tmp_path)
        request_files = list(requests_dir.iterdir())
        assert len(request_files) > 0, "no request file written to requests_dir"

    def test_request_file_status_is_completed(self, tmp_path):
        _, _, _, requests_dir = self._run(tmp_path)
        request_files = list(requests_dir.iterdir())
        parsed = json.loads(request_files[0].read_text(encoding="utf-8"))
        assert parsed["status"] == "completed"

    def test_request_file_has_completed_at(self, tmp_path):
        _, _, _, requests_dir = self._run(tmp_path)
        request_files = list(requests_dir.iterdir())
        parsed = json.loads(request_files[0].read_text(encoding="utf-8"))
        assert "completed_at" in parsed

    def test_no_file_written_when_below_threshold(self, tmp_path):
        """Below HOLD threshold: no files written, no side effects."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True)
        requests_dir.mkdir(parents=True)
        count = _GATE_DELETION_FILE_HOLD - 1
        diff = _diff_deleted(*[f"old/f_{i}.py" for i in range(count)])
        with mock.patch("gate_runner.subprocess.run", return_value=_mock_gh_success(diff)):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert list(results_dir.iterdir()) == []
        assert list(requests_dir.iterdir()) == []

    def test_no_file_written_when_pr_number_none(self, tmp_path):
        """None pr_number: no files written."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True)
        requests_dir.mkdir(parents=True)
        GateRunner._run_codex_net_deletion_check(
            pr_number=None,
            request_payload=_payload(pr_number=None),
            results_dir=results_dir,
            requests_dir=requests_dir,
        )
        assert list(results_dir.iterdir()) == []
        assert list(requests_dir.iterdir()) == []

    def test_no_file_written_when_diff_fails(self, tmp_path):
        """Diff fetch failure: no files written, graceful degradation."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True)
        requests_dir.mkdir(parents=True)
        with mock.patch.object(
            GateRunner, "_fetch_gh_pr_diff", side_effect=RuntimeError("gh failed"),
        ):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results_dir,
                requests_dir=requests_dir,
            )
        assert list(results_dir.iterdir()) == []
        assert list(requests_dir.iterdir()) == []
