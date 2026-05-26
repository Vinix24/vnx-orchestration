#!/usr/bin/env python3
"""Unit tests for GateRunner._run_codex_net_deletion_check (HOLD-level pre-AI check).

Covers the deterministic blocking layer added in gate_runner.py:
  - Returns None when pr_number is falsy (guard)
  - Returns None when gh pr diff is unavailable (graceful degradation)
  - Returns None when deleted-file count is below _GATE_DELETION_FILE_HOLD
  - Returns a complete blocking result dict when threshold is met/exceeded
  - Result structure: status, finding severity, net_deletion_check metadata
  - Filesystem persistence: result file and request file written via _rec helpers
  - pr_id sourcing: from payload or fallback to str(pr_number)

Threshold constant: _GATE_DELETION_FILE_HOLD = 20
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

from gate_runner import GateRunner, _GATE_DELETION_FILE_HOLD  # noqa: E402


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


def _payload(pr_number: int = 42, pr_id: str = "", gate: str = "codex_gate") -> dict:
    return {"pr_number": pr_number, "pr_id": pr_id, "gate": gate}


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    results = tmp_path / "results"
    requests = tmp_path / "requests"
    results.mkdir()
    requests.mkdir()
    return results, requests


# ---------------------------------------------------------------------------
# Guard: pr_number is falsy
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckGuards:

    def test_none_pr_number_returns_none(self, tmp_path):
        results, requests = _dirs(tmp_path)
        result = GateRunner._run_codex_net_deletion_check(
            pr_number=None,
            request_payload=_payload(),
            results_dir=results,
            requests_dir=requests,
        )
        assert result is None

    def test_zero_pr_number_returns_none(self, tmp_path):
        results, requests = _dirs(tmp_path)
        result = GateRunner._run_codex_net_deletion_check(
            pr_number=0,
            request_payload=_payload(pr_number=0),
            results_dir=results,
            requests_dir=requests,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Graceful degradation: diff fetch fails
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckDegradation:

    def test_value_error_from_fetch_returns_none(self, tmp_path):
        results, requests = _dirs(tmp_path)
        with mock.patch.object(
            GateRunner, "_fetch_gh_pr_diff", side_effect=ValueError("no pr_number")
        ):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is None

    def test_runtime_error_from_fetch_returns_none(self, tmp_path):
        results, requests = _dirs(tmp_path)
        with mock.patch.object(
            GateRunner, "_fetch_gh_pr_diff", side_effect=RuntimeError("gh pr diff failed")
        ):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is None

    def test_no_files_written_on_degradation(self, tmp_path):
        """Graceful degradation must not write any files."""
        results, requests = _dirs(tmp_path)
        with mock.patch.object(
            GateRunner, "_fetch_gh_pr_diff", side_effect=RuntimeError("gh fail")
        ):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert list(results.iterdir()) == []
        assert list(requests.iterdir()) == []


# ---------------------------------------------------------------------------
# Below threshold — must NOT block
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckBelowThreshold:

    def test_zero_deletions_returns_none(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n"
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is None

    def test_one_below_threshold_returns_none(self, tmp_path):
        results, requests = _dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD - 1
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is None

    def test_no_files_written_below_threshold(self, tmp_path):
        results, requests = _dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD - 1
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert list(results.iterdir()) == []
        assert list(requests.iterdir()) == []


# ---------------------------------------------------------------------------
# At/above threshold — HOLD triggered
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckHoldTriggered:

    def test_exactly_at_threshold_returns_result(self, tmp_path):
        results, requests = _dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is not None

    def test_above_threshold_returns_result(self, tmp_path):
        results, requests = _dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD + 5
        diff = _diff_deleted(*[f"old/file_{i}.py" for i in range(count)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result is not None

    def test_result_status_is_fail(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["status"] == "fail"

    def test_finding_severity_is_blocking(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert len(result["findings"]) == 1
        assert result["findings"][0]["severity"] == "blocking"

    def test_blocking_findings_mirrors_findings(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["blocking_findings"] == result["findings"]
        assert result["advisory_findings"] == []

    def test_net_deletion_check_metadata_present(self, tmp_path):
        results, requests = _dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        ndc = result["net_deletion_check"]
        assert ndc["triggered"] is True
        assert ndc["deleted_count"] == count
        assert ndc["threshold"] == _GATE_DELETION_FILE_HOLD
        assert len(ndc["deleted_files"]) == count

    def test_summary_contains_deleted_count(self, tmp_path):
        results, requests = _dirs(tmp_path)
        count = _GATE_DELETION_FILE_HOLD + 3
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(count)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert str(count) in result["summary"]

    def test_finding_description_lists_deleted_files(self, tmp_path):
        results, requests = _dirs(tmp_path)
        paths = [f"old/module_{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)]
        diff = _diff_deleted(*paths)
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        description = result["findings"][0]["description"]
        for p in paths:
            assert f"`{p}`" in description

    def test_duration_seconds_is_zero(self, tmp_path):
        """Pre-AI deterministic check has no real duration."""
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["duration_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Filesystem persistence
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckPersistence:

    def test_result_file_written_to_results_dir(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(pr_number=42),
                results_dir=results,
                requests_dir=requests,
            )
        result_files = list(results.iterdir())
        assert len(result_files) == 1

    def test_result_file_is_valid_json(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(pr_number=42),
                results_dir=results,
                requests_dir=requests,
            )
        for f in results.iterdir():
            content = json.loads(f.read_text())
            assert content["status"] == "fail"

    def test_request_file_written_to_requests_dir(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(pr_number=42),
                results_dir=results,
                requests_dir=requests,
            )
        request_files = list(requests.iterdir())
        assert len(request_files) == 1

    def test_request_payload_status_set_to_completed(self, tmp_path):
        results, requests = _dirs(tmp_path)
        payload = _payload(pr_number=42)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=payload,
                results_dir=results,
                requests_dir=requests,
            )
        assert payload["status"] == "completed"
        assert "completed_at" in payload


# ---------------------------------------------------------------------------
# pr_id sourcing
# ---------------------------------------------------------------------------

class TestRunCodexNetDeletionCheckPrId:

    def test_pr_id_from_payload_used_in_result(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(pr_number=42, pr_id="PR-99"),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["pr_id"] == "PR-99"

    def test_pr_id_falls_back_to_str_pr_number(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=42,
                request_payload=_payload(pr_number=42, pr_id=""),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["pr_id"] == "42"

    def test_pr_number_preserved_in_result(self, tmp_path):
        results, requests = _dirs(tmp_path)
        diff = _diff_deleted(*[f"old/f{i}.py" for i in range(_GATE_DELETION_FILE_HOLD)])
        with mock.patch.object(GateRunner, "_fetch_gh_pr_diff", return_value=diff):
            result = GateRunner._run_codex_net_deletion_check(
                pr_number=99,
                request_payload=_payload(pr_number=99),
                results_dir=results,
                requests_dir=requests,
            )
        assert result["pr_number"] == 99
