#!/usr/bin/env python3
"""Tests for review_receipt: advisory vs blocking classification of gate findings.

Coverage targets:
- Advisory vs blocking findings are emitted distinctly in receipts
- ReviewGateManager.record_result persists both lists for any review gate
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from review_receipt import ReviewFinding, ReviewReceipt


# ---------------------------------------------------------------------------
# ReviewFinding
# ---------------------------------------------------------------------------

class TestReviewFinding:
    def test_is_blocking(self):
        f = ReviewFinding(severity="blocking", category="correctness", message="bug")
        assert f.is_blocking()
        assert not f.is_advisory()

    def test_is_advisory(self):
        f = ReviewFinding(severity="advisory", category="style", message="nit")
        assert f.is_advisory()
        assert not f.is_blocking()

    def test_to_dict(self):
        f = ReviewFinding(
            severity="blocking", category="security", message="SQL injection", file_path="app.py", line=42
        )
        d = f.to_dict()
        assert d["severity"] == "blocking"
        assert d["category"] == "security"
        assert d["message"] == "SQL injection"
        assert d["file_path"] == "app.py"
        assert d["line"] == 42


# ---------------------------------------------------------------------------
# ReviewReceipt.from_raw_findings — advisory vs blocking classification
# ---------------------------------------------------------------------------

class TestReviewReceiptFromRawFindings:
    def test_empty_findings_produces_pass(self):
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=[])
        assert receipt.status == "pass"
        assert receipt.advisory_count == 0
        assert receipt.blocking_count == 0
        assert receipt.summary == "LGTM — no findings"

    def test_blocking_severity_classified_as_blocking(self):
        raw = [{"severity": "blocking", "category": "correctness", "message": "bug"}]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.blocking_count == 1
        assert receipt.advisory_count == 0
        assert receipt.status == "fail"

    def test_error_severity_treated_as_blocking(self):
        raw = [{"severity": "error", "category": "security", "message": "XSS"}]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.blocking_count == 1
        assert receipt.status == "fail"

    def test_advisory_severity_classified_as_advisory(self):
        raw = [{"severity": "advisory", "category": "style", "message": "nit"}]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.advisory_count == 1
        assert receipt.blocking_count == 0
        assert receipt.status == "pass"

    def test_warning_and_info_classified_as_advisory(self):
        raw = [
            {"severity": "warning", "category": "style", "message": "nit1"},
            {"severity": "info", "category": "coverage", "message": "low cov"},
        ]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.advisory_count == 2
        assert receipt.blocking_count == 0

    def test_mixed_findings_separated_correctly(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "crash"},
            {"severity": "advisory", "category": "style", "message": "nit"},
            {"severity": "error", "category": "security", "message": "vuln"},
            {"severity": "warning", "category": "coverage", "message": "low"},
        ]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.blocking_count == 2
        assert receipt.advisory_count == 2
        assert receipt.status == "fail"

    def test_advisory_only_findings_produce_pass_status(self):
        raw = [
            {"severity": "advisory", "category": "style", "message": "nit1"},
            {"severity": "warning", "category": "style", "message": "nit2"},
        ]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.status == "pass"

    def test_contract_hash_preserved_in_receipt(self):
        receipt = ReviewReceipt.from_raw_findings(
            pr_id="PR-2", raw_findings=[], contract_hash="abc123"
        )
        assert receipt.contract_hash == "abc123"

    def test_summary_includes_counts(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "A"},
            {"severity": "advisory", "category": "style", "message": "B"},
        ]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert "1 blocking" in receipt.summary
        assert "1 advisory" in receipt.summary


# ---------------------------------------------------------------------------
# ReviewReceipt.to_dict — structure for downstream consumers
# ---------------------------------------------------------------------------

class TestReviewReceiptToDict:
    def test_to_dict_always_has_advisory_and_blocking_lists(self):
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=[])
        d = receipt.to_dict()
        assert "advisory_findings" in d
        assert "blocking_findings" in d
        assert isinstance(d["advisory_findings"], list)
        assert isinstance(d["blocking_findings"], list)

    def test_to_dict_has_counts(self):
        raw = [{"severity": "blocking", "category": "correctness", "message": "oops"}]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        d = receipt.to_dict()
        assert d["blocking_count"] == 1
        assert d["advisory_count"] == 0

    def test_to_dict_is_json_serializable(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "crash"},
            {"severity": "advisory", "category": "style", "message": "nit"},
        ]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        serialized = json.dumps(receipt.to_dict())
        parsed = json.loads(serialized)
        assert parsed["blocking_count"] == 1
        assert parsed["advisory_count"] == 1

    def test_blocking_findings_not_in_advisory_list(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "hard fail"},
            {"severity": "advisory", "category": "style", "message": "soft nit"},
        ]
        receipt = ReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        d = receipt.to_dict()
        advisory_msgs = [f["message"] for f in d["advisory_findings"]]
        blocking_msgs = [f["message"] for f in d["blocking_findings"]]
        assert "hard fail" not in advisory_msgs
        assert "hard fail" in blocking_msgs
        assert "soft nit" not in blocking_msgs
        assert "soft nit" in advisory_msgs


# ---------------------------------------------------------------------------
# Integration: ReviewGateManager.record_result now emits advisory/blocking fields
# ---------------------------------------------------------------------------

class TestRecordResultAdvisoryBlockingIntegration:
    @pytest.fixture
    def manager(self, tmp_path, monkeypatch):
        import review_gate_manager as rgm

        data_dir = tmp_path / ".vnx-data"
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
        monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
        monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
        monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
        monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
        monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(data_dir / "unified_reports" / "headless"))
        monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)

        return rgm.ReviewGateManager()

    def test_record_result_emits_advisory_and_blocking_fields(self, manager):
        report_path = str((manager.reports_dir / "manual-pr2-codex.md").resolve())
        # record_result now requires the report file to exist on disk.
        manager.reports_dir.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text("# codex report\n", encoding="utf-8")
        result = manager.record_result(
            gate="codex_gate",
            pr_number=2,
            branch="feature/test",
            status="fail",
            summary="1 blocking, 1 advisory",
            findings=[
                {"severity": "blocking", "category": "correctness", "message": "crash"},
                {"severity": "advisory", "category": "style", "message": "nit"},
            ],
            contract_hash="hash-pr2",
            pr_id="PR-2",
            report_path=report_path,
        )
        assert "advisory_findings" in result
        assert "blocking_findings" in result
        assert result["blocking_count"] == 1
        assert result["advisory_count"] == 1

    def test_record_result_persists_with_advisory_blocking_fields(self, manager, tmp_path):
        report_path = str((manager.reports_dir / "manual-pr3-codex.md").resolve())
        # record_result now requires the report file to exist on disk.
        manager.reports_dir.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text("# codex report\n", encoding="utf-8")
        manager.record_result(
            gate="codex_gate",
            pr_number=3,
            branch="feature/test",
            status="pass",
            summary="no blockers",
            findings=[{"severity": "advisory", "category": "style", "message": "nit"}],
            contract_hash="hash-pr3",
            pr_id="PR-3",
            report_path=report_path,
        )
        saved = json.loads(
            (manager.results_dir / "pr-3-codex_gate.json").read_text(encoding="utf-8")
        )
        assert "advisory_findings" in saved
        assert "blocking_findings" in saved
        assert saved["advisory_count"] == 1
        assert saved["blocking_count"] == 0
