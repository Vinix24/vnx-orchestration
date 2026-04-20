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
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
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
    assert requested["gemini_review"]["status"] == "requested"
    assert requested["gemini_review"]["report_path"].startswith(str((review_env / ".vnx-data" / "unified_reports").resolve()))
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
    assert gate["status"] == "not_executable"
    assert gate["required"] is True


def test_record_result_persists_structured_review_output(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()
    report_file = review_env / ".vnx-data" / "unified_reports" / "manual-gemini-report.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# Gemini report\n", encoding="utf-8")
    report_path = str(report_file.resolve())

    payload = manager.record_result(
        gate="gemini_review",
        pr_number=8,
        branch="feature/docs",
        status="pass",
        summary="No blocking findings",
        findings=[{"severity": "info", "title": "Minor wording"}],
        residual_risk="low",
        contract_hash="hash-123",
        report_path=report_path,
    )

    result_path = manager.results_dir / "pr-8-gemini_review.json"
    assert result_path.exists()
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert saved["status"] == "pass"
    assert saved["findings"][0]["title"] == "Minor wording"
    assert payload["residual_risk"] == "low"
    assert saved["contract_hash"] == "hash-123"
    assert saved["report_path"] == report_path


def test_record_result_canonicalizes_relative_report_path(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    report_rel = ".vnx-data/unified_reports/review.md"
    report_file = review_env / ".vnx-data" / "unified_reports" / "review.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# Review report\n", encoding="utf-8")
    payload = manager.record_result(
        gate="codex_gate",
        pr_number=9,
        branch="feature/docs",
        status="pass",
        summary="No blocking findings",
        contract_hash="hash-456",
        report_path=report_rel,
    )

    expected = str((review_env / report_rel).resolve())
    saved = json.loads((manager.results_dir / "pr-9-codex_gate.json").read_text(encoding="utf-8"))
    assert payload["report_path"] == expected
    assert saved["report_path"] == expected


def test_record_result_uses_request_report_path_when_report_path_omitted(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

    manager = rgm.ReviewGateManager()
    requested = manager.request_reviews(
        pr_number=17,
        branch="feature/runtime",
        review_stack=["gemini_review"],
        risk_class="high",
        changed_files=["scripts/runtime.py"],
        mode="per_pr",
    )["requested"][0]

    # Create the report file that the request reserved
    Path(requested["report_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(requested["report_path"]).write_text("# Gemini headless report\n", encoding="utf-8")

    payload = manager.record_result(
        gate="gemini_review",
        pr_number=17,
        branch="feature/runtime",
        status="pass",
        summary="No blocking findings",
        contract_hash="hash-789",
    )

    assert payload["report_path"] == requested["report_path"]


def test_record_result_rejects_pass_without_contract_hash(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    with pytest.raises(ValueError, match="contract_hash is required"):
        manager.record_result(
            gate="gemini_review",
            pr_number=21,
            branch="feature/docs",
            status="pass",
            summary="No blocking findings",
            report_path=str((review_env / ".vnx-data" / "unified_reports" / "gate.md").resolve()),
        )


def test_record_result_rejects_pass_without_report_path_or_request(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    with pytest.raises(ValueError, match="report_path is required"):
        manager.record_result(
            gate="codex_gate",
            pr_number=22,
            branch="feature/runtime",
            status="pass",
            summary="No blocking findings",
            contract_hash="hash-999",
        )
