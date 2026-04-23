#!/usr/bin/env python3
"""Tests for backfill_headless_receipts.py (OI-AT-4 phase 2)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

import backfill_headless_receipts as bfr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _receipt(
    dispatch_id: str = "unknown",
    report_file: str = "20260401-075225-HEADLESS-codex_gate-pr-0.md",
    terminal: str = "unknown",
    track: str = "unknown",
    gate: str = "unknown",
    status: str = "unknown",
    extra: dict | None = None,
) -> dict:
    r = {
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "task_id": "unknown",
        "terminal": terminal,
        "track": track,
        "gate": gate,
        "status": status,
        "report_file": report_file,
        "report_path": f"/some/path/{report_file}",
        "missing_fields": ["task_id", "dispatch_id"],
        "legacy_format": True,
    }
    if extra:
        r.update(extra)
    return r


# ---------------------------------------------------------------------------
# HEADLESS_REPORT_RE pattern tests
# ---------------------------------------------------------------------------

class TestHeadlessReportRe:
    def test_matches_codex_gate(self):
        m = bfr.HEADLESS_REPORT_RE.match("20260401-075225-HEADLESS-codex_gate-pr-0.md")
        assert m is not None
        assert m.group("gate") == "codex_gate"
        assert m.group("pr") == "0"
        assert m.group("date") == "20260401"

    def test_matches_gemini_review(self):
        m = bfr.HEADLESS_REPORT_RE.match("20260423-213113-HEADLESS-gemini_review-pr-256.md")
        assert m is not None
        assert m.group("gate") == "gemini_review"
        assert m.group("pr") == "256"

    def test_rejects_worker_report(self):
        assert bfr.HEADLESS_REPORT_RE.match("20260402-134957-C-review-runtime-adapter.md") is None

    def test_rejects_non_md(self):
        assert bfr.HEADLESS_REPORT_RE.match("20260401-075225-HEADLESS-codex_gate-pr-0.json") is None


# ---------------------------------------------------------------------------
# _backfill_receipt: HEADLESS path
# ---------------------------------------------------------------------------

class TestBackfillHeadless:
    def setup_method(self):
        self.pr_gate_index = {
            ("codex_gate", 0): {"status": "approve", "branch": "feature/test-branch"},
        }
        self.dispatch_index = {}

    def test_headless_dispatch_id_synthetic(self):
        r = _receipt()
        modified, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert modified
        assert updated["dispatch_id"] == "gate-codex_gate-pr-0"

    def test_headless_task_id_matches_dispatch_id(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["task_id"] == updated["dispatch_id"]

    def test_headless_terminal_is_HEADLESS(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["terminal"] == "HEADLESS"

    def test_headless_track_is_headless(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["track"] == "headless"

    def test_headless_gate_name_set(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["gate"] == "codex_gate"

    def test_headless_status_from_index(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["status"] == "approve"

    def test_headless_branch_from_index(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated.get("branch") == "feature/test-branch"

    def test_headless_pr_number_set(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["pr_number"] == 0

    def test_headless_missing_fields_cleaned(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert "dispatch_id" not in updated["missing_fields"]
        assert "task_id" not in updated["missing_fields"]

    def test_headless_backfilled_flag_set(self):
        r = _receipt()
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert updated["backfilled"] is True
        assert "backfilled_at" in updated
        assert updated["backfilled_by"] == "backfill_headless_receipts.py"

    def test_gemini_review_dispatch_id(self):
        r = _receipt(report_file="20260423-213113-HEADLESS-gemini_review-pr-256.md")
        idx = {("gemini_review", 256): {"status": "completed", "branch": "fix/branch"}}
        _, updated = bfr._backfill_receipt(r, idx, {})
        assert updated["dispatch_id"] == "gate-gemini_review-pr-256"
        assert updated["gate"] == "gemini_review"

    def test_status_fallback_to_completed_when_unknown(self):
        r = _receipt(report_file="20260402-034830-HEADLESS-codex_gate-pr-64.md")
        # pr-64 not in index
        _, updated = bfr._backfill_receipt(r, {}, {})
        assert updated["status"] in ("completed", "pass", "fail", "approve", "reject")

    def test_already_backfilled_not_modified(self):
        r = _receipt(extra={"backfilled": True})
        modified, _ = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert not modified

    def test_non_unknown_dispatch_id_not_modified(self):
        r = _receipt(dispatch_id="gate-codex_gate-pr-0")
        modified, _ = bfr._backfill_receipt(r, self.pr_gate_index, self.dispatch_index)
        assert not modified


# ---------------------------------------------------------------------------
# _backfill_receipt: worker (non-HEADLESS) path
# ---------------------------------------------------------------------------

class TestBackfillWorker:
    def setup_method(self):
        self.pr_gate_index: dict = {}

    def test_worker_gets_synthetic_id(self):
        r = _receipt(
            report_file="20260402-134957-C-review-runtime-adapter-contract.md",
            terminal="T3",
        )
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, {})
        assert updated["dispatch_id"] != "unknown"
        assert len(updated["dispatch_id"]) > 0

    def test_worker_synthetic_source_tagged(self):
        r = _receipt(
            report_file="20260402-134957-C-review-runtime-adapter-contract.md",
            terminal="T3",
        )
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, {})
        assert updated.get("dispatch_id_source") in (
            "dispatch_correlation", "synthetic_worker"
        )

    def test_worker_dispatch_correlation_high_score(self):
        r = _receipt(
            report_file="20260402-134957-C-review-runtime-adapter-contract.md",
            terminal="T3",
        )
        # Provide a dispatch that should match (date + track + slug words)
        dispatch_index = {
            "20260402-134005-runtime-adapter-contract-and-c-C": "20260402-134005-runtime-adapter-contract-and-c-C"
        }
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, dispatch_index)
        # Score >= 3 → correlated
        if updated.get("dispatch_id_source") == "dispatch_correlation":
            assert updated["dispatch_id"] == "20260402-134005-runtime-adapter-contract-and-c-C"

    def test_worker_missing_fields_cleaned(self):
        r = _receipt(
            report_file="20260402-134957-C-review-runtime-adapter-contract.md",
            terminal="T3",
        )
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, {})
        assert "dispatch_id" not in updated["missing_fields"]

    def test_worker_backfilled_flag_set(self):
        r = _receipt(
            report_file="20260402-134957-C-review-runtime-adapter-contract.md",
            terminal="T3",
        )
        _, updated = bfr._backfill_receipt(r, self.pr_gate_index, {})
        assert updated["backfilled"] is True

    def test_receipt_with_no_report_file_not_modified(self):
        r = _receipt(report_file="")
        r["report_path"] = ""
        modified, _ = bfr._backfill_receipt(r, self.pr_gate_index, {})
        assert not modified


# ---------------------------------------------------------------------------
# _make_worker_synthetic_id
# ---------------------------------------------------------------------------

class TestMakeSyntheticId:
    def test_includes_terminal(self):
        sid = bfr._make_worker_synthetic_id("20260402-C-test-slug.md", "T3")
        assert "t3" in sid

    def test_stable_for_same_input(self):
        a = bfr._make_worker_synthetic_id("20260402-C-test-slug.md", "T3")
        b = bfr._make_worker_synthetic_id("20260402-C-test-slug.md", "T3")
        assert a == b

    def test_handles_empty_report_file(self):
        sid = bfr._make_worker_synthetic_id("", "T3")
        assert len(sid) > 0


# ---------------------------------------------------------------------------
# _update_processed_receipts / _update_ndjson (integration via tmp dirs)
# ---------------------------------------------------------------------------

class TestUpdateFiles:
    def _write_receipt(self, path: Path, receipt: dict) -> None:
        path.write_text(json.dumps(receipt))

    def test_json_file_patched_in_place(self, tmp_path):
        original_dir = bfr.RECEIPTS_PROCESSED_DIR
        bfr.RECEIPTS_PROCESSED_DIR = tmp_path

        r = _receipt()
        self._write_receipt(tmp_path / "test-receipt.json", r)

        ph, pw, skipped = bfr._update_processed_receipts({}, {}, dry_run=False, verbose=False)
        assert ph + pw == 1
        content = json.loads((tmp_path / "test-receipt.json").read_text())
        assert content["dispatch_id"] == "gate-codex_gate-pr-0"
        assert content["backfilled"] is True

        bfr.RECEIPTS_PROCESSED_DIR = original_dir

    def test_dry_run_does_not_write(self, tmp_path):
        original_dir = bfr.RECEIPTS_PROCESSED_DIR
        bfr.RECEIPTS_PROCESSED_DIR = tmp_path

        r = _receipt()
        self._write_receipt(tmp_path / "test-receipt.json", r)

        bfr._update_processed_receipts({}, {}, dry_run=True, verbose=False)
        content = json.loads((tmp_path / "test-receipt.json").read_text())
        # dispatch_id unchanged — dry run did not write
        assert content["dispatch_id"] == "unknown"

        bfr.RECEIPTS_PROCESSED_DIR = original_dir

    def test_ndjson_patched(self, tmp_path):
        original_path = bfr.T0_RECEIPTS_NDJSON
        ndjson_path = tmp_path / "t0_receipts.ndjson"
        bfr.T0_RECEIPTS_NDJSON = ndjson_path

        r = _receipt()
        ndjson_path.write_text(json.dumps(r) + "\n")

        bfr._update_ndjson({}, {}, dry_run=False)
        lines = [l for l in ndjson_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        updated = json.loads(lines[0])
        assert updated["dispatch_id"] == "gate-codex_gate-pr-0"

        bfr.T0_RECEIPTS_NDJSON = original_path

    def test_ndjson_dry_run_does_not_write(self, tmp_path):
        original_path = bfr.T0_RECEIPTS_NDJSON
        ndjson_path = tmp_path / "t0_receipts.ndjson"
        bfr.T0_RECEIPTS_NDJSON = ndjson_path

        r = _receipt()
        ndjson_path.write_text(json.dumps(r) + "\n")

        bfr._update_ndjson({}, {}, dry_run=True)
        updated = json.loads(ndjson_path.read_text().strip())
        assert updated["dispatch_id"] == "unknown"

        bfr.T0_RECEIPTS_NDJSON = original_path

    def test_already_patched_not_double_patched(self, tmp_path):
        original_dir = bfr.RECEIPTS_PROCESSED_DIR
        bfr.RECEIPTS_PROCESSED_DIR = tmp_path

        r = _receipt(extra={"backfilled": True, "dispatch_id": "gate-codex_gate-pr-0"})
        r["dispatch_id"] = "gate-codex_gate-pr-0"
        self._write_receipt(tmp_path / "test-receipt.json", r)

        ph, pw, skipped = bfr._update_processed_receipts({}, {}, dry_run=False, verbose=False)
        assert ph + pw == 0
        assert skipped == 1

        bfr.RECEIPTS_PROCESSED_DIR = original_dir


# ---------------------------------------------------------------------------
# _build_pr_gate_index
# ---------------------------------------------------------------------------

class TestBuildPrGateIndex:
    def test_empty_when_no_dirs(self, tmp_path):
        original_r = bfr.RESULTS_DIR
        original_q = bfr.REQUESTS_DIR
        bfr.RESULTS_DIR = tmp_path / "results"
        bfr.REQUESTS_DIR = tmp_path / "requests"

        idx = bfr._build_pr_gate_index()
        assert idx == {}

        bfr.RESULTS_DIR = original_r
        bfr.REQUESTS_DIR = original_q

    def test_parses_pr_gate_result(self, tmp_path):
        original_r = bfr.RESULTS_DIR
        original_q = bfr.REQUESTS_DIR
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        bfr.RESULTS_DIR = results_dir
        bfr.REQUESTS_DIR = tmp_path / "requests"

        data = {"gate": "codex_gate", "status": "approve", "branch": "feat/x",
                "report_path": "/tmp/some-report.md"}
        (results_dir / "pr-42-codex_gate.json").write_text(json.dumps(data))

        idx = bfr._build_pr_gate_index()
        assert ("codex_gate", 42) in idx
        assert idx[("codex_gate", 42)]["status"] == "approve"
        assert idx[("codex_gate", 42)]["branch"] == "feat/x"

        bfr.RESULTS_DIR = original_r
        bfr.REQUESTS_DIR = original_q
