#!/usr/bin/env python3
"""Tests for OI-AT-7: headless gate reports written to unified_reports/headless/ subdir."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# vnx_paths.py: VNX_HEADLESS_REPORTS_DIR is defined and points to headless/
# ---------------------------------------------------------------------------

class TestVnxPathsHeadlessDir:
    def test_headless_reports_dir_in_resolve_paths(self, tmp_path):
        import vnx_paths
        env = {
            "VNX_HOME": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
        }
        with patch.dict(os.environ, env, clear=False):
            paths = vnx_paths.resolve_paths()
        assert "VNX_HEADLESS_REPORTS_DIR" in paths

    def test_headless_reports_dir_is_subdir_of_reports_dir(self, tmp_path):
        import vnx_paths
        env = {
            "VNX_HOME": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
        }
        with patch.dict(os.environ, env, clear=False):
            paths = vnx_paths.resolve_paths()
        reports = Path(paths["VNX_REPORTS_DIR"])
        headless = Path(paths["VNX_HEADLESS_REPORTS_DIR"])
        assert headless == reports / "headless"

    def test_headless_reports_dir_env_override(self, tmp_path):
        import vnx_paths
        custom = str(tmp_path / "custom_headless")
        env = {
            "VNX_HOME": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
            "VNX_HEADLESS_REPORTS_DIR": custom,
        }
        with patch.dict(os.environ, env, clear=False):
            paths = vnx_paths.resolve_paths()
        assert paths["VNX_HEADLESS_REPORTS_DIR"] == custom


# ---------------------------------------------------------------------------
# ReviewGateManager: _build_report_path uses headless_reports_dir
# ---------------------------------------------------------------------------

class TestReviewGateManagerHeadlessDir:
    def _make_manager(self, tmp_path):
        from review_gate_manager import ReviewGateManager
        env = {
            "VNX_HOME": str(tmp_path),
            "PROJECT_ROOT": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
            "VNX_STATE_DIR": str(tmp_path / ".vnx-data" / "state"),
            "VNX_REPORTS_DIR": str(tmp_path / ".vnx-data" / "unified_reports"),
            "VNX_HEADLESS_REPORTS_DIR": str(tmp_path / ".vnx-data" / "unified_reports" / "headless"),
        }
        with patch.dict(os.environ, env, clear=False):
            return ReviewGateManager(), tmp_path

    def test_headless_reports_dir_attribute(self, tmp_path):
        mgr, base = self._make_manager(tmp_path)
        expected = base / ".vnx-data" / "unified_reports" / "headless"
        assert mgr.headless_reports_dir == expected

    def test_headless_reports_dir_created_on_init(self, tmp_path):
        mgr, base = self._make_manager(tmp_path)
        assert mgr.headless_reports_dir.is_dir()

    def test_build_report_path_in_headless_subdir(self, tmp_path):
        mgr, base = self._make_manager(tmp_path)
        path = mgr._build_report_path(
            gate="gemini_review",
            requested_at="20260424-120000",
            pr_number=42,
        )
        report = Path(path)
        headless_dir = base / ".vnx-data" / "unified_reports" / "headless"
        assert report.parent == headless_dir

    def test_build_report_path_not_in_root_reports_dir(self, tmp_path):
        mgr, base = self._make_manager(tmp_path)
        path = mgr._build_report_path(
            gate="codex_gate",
            requested_at="20260424-120000",
            pr_number=7,
        )
        report = Path(path)
        root_reports = base / ".vnx-data" / "unified_reports"
        assert report.parent != root_reports

    def test_build_report_path_filename_pattern(self, tmp_path):
        mgr, _ = self._make_manager(tmp_path)
        path = mgr._build_report_path(
            gate="codex_gate",
            requested_at="20260424-120000",
            pr_number=5,
        )
        name = Path(path).name
        assert "HEADLESS" in name
        assert "codex_gate" in name
        assert "pr-5" in name
        assert name.endswith(".md")

    def test_build_report_path_with_pr_id(self, tmp_path):
        mgr, base = self._make_manager(tmp_path)
        path = mgr._build_report_path(
            gate="gemini_review",
            requested_at="20260424-150000",
            pr_id="pr-999",
        )
        report = Path(path)
        headless_dir = base / ".vnx-data" / "unified_reports" / "headless"
        assert report.parent == headless_dir


# ---------------------------------------------------------------------------
# gate_artifacts: materialize_artifacts writes to headless subdir path
# ---------------------------------------------------------------------------

class TestMaterializeArtifactsHeadlessPath:
    def test_report_written_to_provided_path(self, tmp_path):
        """materialize_artifacts honours report_path — writing to headless/ is a layout concern."""
        import gate_artifacts

        headless_dir = tmp_path / "unified_reports" / "headless"
        headless_dir.mkdir(parents=True)
        requests_dir = tmp_path / "requests"
        results_dir = tmp_path / "results"
        requests_dir.mkdir()
        results_dir.mkdir()

        report_file = headless_dir / "20260424-120000-HEADLESS-gemini_review-pr-1.md"
        stdout = "line1\nline2\nline3\nline4\n"
        request_payload = {
            "gate": "gemini_review",
            "pr_number": 1,
            "pr_id": "",
            "branch": "test-branch",
            "report_path": str(report_file),
            "dispatch_id": "test-dispatch-id",
        }

        result = gate_artifacts.materialize_artifacts(
            gate="gemini_review",
            pr_number=1,
            pr_id="",
            stdout=stdout,
            request_payload=request_payload,
            duration_seconds=1.5,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=headless_dir,
        )

        assert result.get("status") == "completed"
        assert report_file.exists()
        assert report_file.stat().st_size > 0

    def test_report_path_inside_headless_subdir(self, tmp_path):
        """result payload report_path reflects the headless/ location."""
        import gate_artifacts

        headless_dir = tmp_path / "unified_reports" / "headless"
        headless_dir.mkdir(parents=True)
        requests_dir = tmp_path / "requests"
        results_dir = tmp_path / "results"
        requests_dir.mkdir()
        results_dir.mkdir()

        report_file = headless_dir / "20260424-120000-HEADLESS-codex_gate-pr-2.md"
        stdout = "Finding A\nFinding B\nFinding C\n"
        request_payload = {
            "gate": "codex_gate",
            "pr_number": 2,
            "pr_id": "",
            "branch": "fix/headless-layout",
            "report_path": str(report_file),
            "dispatch_id": "test-dispatch-002",
        }

        result = gate_artifacts.materialize_artifacts(
            gate="codex_gate",
            pr_number=2,
            pr_id="",
            stdout=stdout,
            request_payload=request_payload,
            duration_seconds=2.0,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=headless_dir,
        )

        assert "headless" in result.get("report_path", "")
