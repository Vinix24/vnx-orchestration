#!/usr/bin/env python3

import json
import sys
from pathlib import Path

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import review_gate_manager as rgm


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return project_root


def test_request_reviews_queues_gemini_and_skips_unconfigured_optional(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    result = manager.request_reviews(
        pr_number=12,
        branch="feature/demo",
        review_stack=["gemini_review", "claude_github_optional"],
        risk_class="medium",
        changed_files=["docs/guide.md"],
        mode="per_pr",
    )

    requested = {item["gate"]: item for item in result["requested"]}
    assert requested["gemini_review"]["status"] == "queued"
    assert requested["claude_github_optional"]["status"] == "not_configured"
    assert (manager.requests_dir / "pr-12-gemini_review.json").exists()


def test_codex_final_gate_blocks_when_required_but_not_available(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: None)
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    result = manager.request_reviews(
        pr_number=44,
        branch="feature/runtime-core",
        review_stack=["codex_gate"],
        risk_class="high",
        changed_files=["scripts/pr_queue_manager.py"],
        mode="final",
    )

    gate = result["requested"][0]
    assert gate["gate"] == "codex_gate"
    assert gate["status"] == "blocked"
    assert gate["required"] is True


def test_record_result_persists_structured_review_output(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    payload = manager.record_result(
        gate="gemini_review",
        pr_number=8,
        branch="feature/docs",
        status="pass",
        summary="No blocking findings",
        findings=[{"severity": "info", "title": "Minor wording"}],
        residual_risk="low",
    )

    result_path = manager.results_dir / "pr-8-gemini_review.json"
    assert result_path.exists()
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert saved["status"] == "pass"
    assert saved["findings"][0]["title"] == "Minor wording"
    assert payload["residual_risk"] == "low"
