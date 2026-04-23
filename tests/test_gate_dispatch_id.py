"""Tests for dispatch_id propagation through gate request payloads and artifacts.

Covers OI-AT-4: headless gate writer must emit real dispatch_id, not synthetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_artifacts
from gate_artifacts import materialize_artifacts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def artifact_env(tmp_path):
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    return {
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": requests_dir,
        "results_dir": results_dir,
    }


def _make_request_payload(gate="gemini_review", pr_number=1, **overrides):
    base = {
        "gate": gate,
        "status": "requested",
        "provider": "gemini_cli",
        "branch": "fix/test",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/test.py"],
        "requested_at": "20260423T100000Z",
        "prompt": "Review this code",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# gate_artifacts: dispatch_id in result and sidecar
# ---------------------------------------------------------------------------


class TestMaterializeArtifactsDispatchId:
    """Validates OI-AT-4: real dispatch_id emitted in result and sidecar."""

    def _run_materialize(self, env, request_payload, stdout="Output line one.\nLine two.\nLine three.\n"):
        report_file = env["reports_dir"] / "test-report.md"
        request_payload.setdefault("report_path", str(report_file))
        return materialize_artifacts(
            gate=request_payload["gate"],
            pr_number=request_payload.get("pr_number"),
            pr_id=request_payload.get("pr_id", ""),
            stdout=stdout,
            request_payload=request_payload,
            duration_seconds=1.5,
            requests_dir=env["requests_dir"],
            results_dir=env["results_dir"],
            reports_dir=env["reports_dir"],
        )

    def test_result_payload_includes_real_dispatch_id(self, artifact_env):
        payload = _make_request_payload(dispatch_id="20260423-120000-test-dispatch-A")
        result = self._run_materialize(artifact_env, payload)

        assert result["status"] == "completed"
        assert result["dispatch_id"] == "20260423-120000-test-dispatch-A"

    def test_result_payload_omits_dispatch_id_when_not_provided(self, artifact_env):
        payload = _make_request_payload()
        result = self._run_materialize(artifact_env, payload)

        assert result["status"] == "completed"
        assert "dispatch_id" not in result

    def test_sidecar_uses_real_dispatch_id_when_present(self, artifact_env):
        payload = _make_request_payload(dispatch_id="20260423-120000-test-dispatch-A")
        self._run_materialize(artifact_env, payload)

        sidecar_dir = artifact_env["reports_dir"].parent / "state" / "report_pipeline"
        sidecar_files = list(sidecar_dir.glob("*.json"))
        assert len(sidecar_files) == 1, "Expected exactly one sidecar file"

        sidecar = json.loads(sidecar_files[0].read_text())
        assert sidecar["dispatch_id"] == "20260423-120000-test-dispatch-A"

    def test_sidecar_falls_back_to_synthetic_when_no_dispatch_id(self, artifact_env):
        payload = _make_request_payload(pr_number=42)
        self._run_materialize(artifact_env, payload)

        sidecar_dir = artifact_env["reports_dir"].parent / "state" / "report_pipeline"
        sidecar_files = list(sidecar_dir.glob("*.json"))
        assert len(sidecar_files) == 1

        sidecar = json.loads(sidecar_files[0].read_text())
        # Synthetic fallback format: gate-<gate>-pr-<pr_number>
        assert sidecar["dispatch_id"] == "gate-gemini_review-pr-42"

    def test_result_file_written_with_dispatch_id(self, artifact_env):
        payload = _make_request_payload(pr_number=10, dispatch_id="20260423-090000-real-dispatch-B")
        self._run_materialize(artifact_env, payload)

        result_file = artifact_env["results_dir"] / "pr-10-gemini_review.json"
        assert result_file.exists(), "Result JSON file must be written"
        result = json.loads(result_file.read_text())
        assert result["dispatch_id"] == "20260423-090000-real-dispatch-B"

    def test_real_dispatch_id_overrides_synthetic_for_codex(self, artifact_env):
        payload = _make_request_payload(
            gate="codex_gate",
            pr_number=99,
            dispatch_id="20260423-150000-codex-dispatch-C",
        )
        result = self._run_materialize(artifact_env, payload)

        assert result["dispatch_id"] == "20260423-150000-codex-dispatch-C"
        sidecar_dir = artifact_env["reports_dir"].parent / "state" / "report_pipeline"
        sidecar = json.loads(next(sidecar_dir.glob("*.json")).read_text())
        assert sidecar["dispatch_id"] == "20260423-150000-codex-dispatch-C"


# ---------------------------------------------------------------------------
# gate_request_handler: dispatch_id in request payloads
# ---------------------------------------------------------------------------


class TestRequestHandlerDispatchId:
    """Validates that request methods include dispatch_id in persisted payloads."""

    @pytest.fixture
    def manager_env(self, tmp_path, monkeypatch):
        project_root = tmp_path / "project"
        data_dir = project_root / ".vnx-data"
        state_dir = data_dir / "state"
        reports_dir = data_dir / "unified_reports"
        state_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
        (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
        monkeypatch.setenv("PROJECT_ROOT", str(project_root))
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
        monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
        monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
        monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
        monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        return {
            "project_root": project_root,
            "state_dir": state_dir,
            "reports_dir": reports_dir,
            "requests_dir": state_dir / "review_gates" / "requests",
        }

    def _make_manager(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from review_gate_manager import ReviewGateManager
        return ReviewGateManager()

    def test_request_reviews_propagates_dispatch_id_to_gemini(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = self._make_manager()
        dispatch_id = "20260423-180000-manager-test-A"

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=5,
                branch="fix/test",
                review_stack=["gemini_review"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id=dispatch_id,
            )

        req_file = manager_env["requests_dir"] / "pr-5-gemini_review.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert payload["dispatch_id"] == dispatch_id

    def test_request_reviews_propagates_dispatch_id_to_codex(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = self._make_manager()
        dispatch_id = "20260423-180000-manager-test-B"

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=6,
                branch="fix/test",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id=dispatch_id,
            )

        req_file = manager_env["requests_dir"] / "pr-6-codex_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert payload["dispatch_id"] == dispatch_id

    def test_request_reviews_omits_dispatch_id_when_not_provided(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = self._make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=7,
                branch="fix/test",
                review_stack=["gemini_review"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
            )

        req_file = manager_env["requests_dir"] / "pr-7-gemini_review.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "dispatch_id" not in payload

    def test_request_reviews_propagates_dispatch_id_to_claude_github(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = self._make_manager()
        dispatch_id = "20260423-180000-manager-test-C"

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=8,
                branch="fix/test",
                review_stack=["claude_github_optional"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id=dispatch_id,
            )

        req_file = manager_env["requests_dir"] / "pr-8-claude_github_optional.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert payload["dispatch_id"] == dispatch_id


# ---------------------------------------------------------------------------
# CLI integration: --dispatch-id arg threads through
# ---------------------------------------------------------------------------


class TestCLIDispatchIdArg:
    """Validates CLI --dispatch-id argument presence and propagation."""

    def test_request_subcommand_accepts_dispatch_id(self, tmp_path, monkeypatch):
        """--dispatch-id is accepted without error by the request subcommand."""
        project_root = tmp_path / "project"
        data_dir = project_root / ".vnx-data"
        state_dir = data_dir / "state"
        reports_dir = data_dir / "unified_reports"
        for d in (
            state_dir / "review_gates" / "requests",
            state_dir / "review_gates" / "results",
            reports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
        monkeypatch.setenv("PROJECT_ROOT", str(project_root))
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
        monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
        monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
        monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
        monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib
        import review_gate_manager as rgm
        importlib.reload(rgm)

        with patch("governance_receipts.emit_governance_receipt"):
            rc = rgm.main([
                "request",
                "--pr", "20",
                "--branch", "fix/test-cli",
                "--review-stack", "gemini_review",
                "--changed-files", "scripts/x.py",
                "--dispatch-id", "20260423-200000-cli-dispatch-D",
            ])

        assert rc == 0
        req_file = state_dir / "review_gates" / "requests" / "pr-20-gemini_review.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert payload["dispatch_id"] == "20260423-200000-cli-dispatch-D"

    def test_request_subcommand_without_dispatch_id_still_works(self, tmp_path, monkeypatch):
        """Omitting --dispatch-id does not break the request subcommand."""
        project_root = tmp_path / "project"
        data_dir = project_root / ".vnx-data"
        state_dir = data_dir / "state"
        reports_dir = data_dir / "unified_reports"
        for d in (
            state_dir / "review_gates" / "requests",
            state_dir / "review_gates" / "results",
            reports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
        monkeypatch.setenv("PROJECT_ROOT", str(project_root))
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
        monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
        monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
        monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
        monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib
        import review_gate_manager as rgm
        importlib.reload(rgm)

        with patch("governance_receipts.emit_governance_receipt"):
            rc = rgm.main([
                "request",
                "--pr", "21",
                "--branch", "fix/test-cli",
                "--review-stack", "gemini_review",
                "--changed-files", "scripts/x.py",
            ])

        assert rc == 0
        req_file = state_dir / "review_gates" / "requests" / "pr-21-gemini_review.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "dispatch_id" not in payload
