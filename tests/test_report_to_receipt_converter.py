#!/usr/bin/env python3
"""Tests for report_to_receipt_converter — generic report→receipt conversion.

Covers:
  1. YAML-frontmatter report → exactly one receipt emitted
  2. Re-run on same report → idempotent (no second receipt)
  3. Malformed report (no dispatch_id) → skipped with warning, no crash
  4. scan_and_convert() over a directory of mixed reports
  5. Watermark persistence: already-processed reports skipped across calls
  6. Bash-watermark sync: reports in processed_receipts.txt are skipped
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from report_to_receipt_converter import (
    _BASH_WATERMARK_FILENAME,
    _WATERMARK_FILENAME,
    _compute_sha256,
    _load_watermark,
    build_receipt_from_report,
    convert_report_to_receipt,
    parse_frontmatter,
    scan_and_convert,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    return sd


@pytest.fixture()
def reports_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "unified_reports"
    rd.mkdir(parents=True)
    return rd


def _write_frontmatter_report(path: Path, dispatch_id: str, **extra) -> Path:
    """Write a well-formed YAML-frontmatter report."""
    fields = {
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "status": "complete",
        "timestamp": "2026-06-01T21:34:16Z",
        **extra,
    }
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
    content = (
        f"---\n{fm_lines}\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _count_receipts(state_dir: Path) -> int:
    receipts_file = state_dir / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return 0
    lines = [l.strip() for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    return len(lines)


def _receipts(state_dir: Path) -> list:
    receipts_file = state_dir / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return []
    return [json.loads(l) for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Part 1: parse_frontmatter()
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_parses_standard_yaml_frontmatter(self):
        text = "---\ndispatch_id: abc-123\nprovider: claude\n---\n\n## Body"
        fm = parse_frontmatter(text)
        assert fm["dispatch_id"] == "abc-123"
        assert fm["provider"] == "claude"

    def test_returns_empty_when_absent(self):
        text = "## No frontmatter\n\nJust a body."
        assert parse_frontmatter(text) == {}

    def test_ignores_comment_lines(self):
        text = "---\n# comment\ndispatch_id: xyz\n---\n"
        fm = parse_frontmatter(text)
        assert fm == {"dispatch_id": "xyz"}

    def test_handles_hyphen_in_key(self):
        text = "---\ndispatch-id: my-dispatch\n---\n"
        fm = parse_frontmatter(text)
        assert fm.get("dispatch_id") == "my-dispatch"


# ---------------------------------------------------------------------------
# Part 2: build_receipt_from_report()
# ---------------------------------------------------------------------------

class TestBuildReceiptFromReport:
    def test_builds_from_frontmatter(self, tmp_path):
        p = tmp_path / "20260601-test.md"
        _write_frontmatter_report(p, "20260601-test-dispatch")
        receipt = build_receipt_from_report(p, p.read_text(encoding="utf-8"))
        assert receipt is not None
        assert receipt["dispatch_id"] == "20260601-test-dispatch"
        assert receipt["provider"] == "claude"
        assert receipt["event_type"] == "task_complete"
        assert receipt["timestamp"] == "2026-06-01T21:34:16Z"

    def test_falls_back_to_filename_dispatch_id_as_contract_invalid(self, tmp_path):
        # Filename-only dispatch_id (no content dispatch_id) is a contract
        # violation: produces a receipt but NOT as task_complete.
        p = tmp_path / "20260601-fallback-dispatch.md"
        p.write_text("## Summary\n\nNo frontmatter here.\n\n## Changes\n\n-\n\n## Verification\n\n-\n\n## Open Items\n\nNone\n", encoding="utf-8")
        receipt = build_receipt_from_report(p, p.read_text(encoding="utf-8"))
        assert receipt is not None
        assert receipt["dispatch_id"] == "20260601-fallback-dispatch"
        assert receipt["event_type"] == "report_contract_invalid"
        assert receipt["status"] == "contract_invalid"

    def test_returns_none_for_truly_malformed(self, tmp_path):
        p = tmp_path / "unknown.md"
        p.write_text("## No identifiable dispatch ID at all\n", encoding="utf-8")
        receipt = build_receipt_from_report(p, p.read_text(encoding="utf-8"))
        assert receipt is None

    def test_extracts_bold_field_dispatch_id(self, tmp_path):
        p = tmp_path / "report.md"
        p.write_text(
            "**Dispatch-ID**: 20260601-bold-field\n\n"
            "## Summary\n\nDone.\n\n## Changes\n\n-\n\n## Verification\n\n-\n\n## Open Items\n\nNone\n",
            encoding="utf-8",
        )
        receipt = build_receipt_from_report(p, p.read_text(encoding="utf-8"))
        assert receipt is not None
        assert receipt["dispatch_id"] == "20260601-bold-field"


# ---------------------------------------------------------------------------
# Part 3: convert_report_to_receipt() — single-file conversion
# ---------------------------------------------------------------------------

class TestConvertReportToReceipt:
    def test_emits_exactly_one_receipt(self, tmp_path, state_dir):
        report = tmp_path / "20260601-single-test.md"
        _write_frontmatter_report(report, "20260601-single-test")
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(
            report,
            receipts_file=receipts_file,
            cache_window_seconds=300,
        )

        assert result is not None
        assert result.status == "appended"
        assert _count_receipts(state_dir) == 1

        r = _receipts(state_dir)[0]
        assert r["dispatch_id"] == "20260601-single-test"
        assert r["event_type"] == "task_complete"
        assert r["provider"] == "claude"

    def test_idempotent_same_call_twice(self, tmp_path, state_dir):
        report = tmp_path / "20260601-idempotent.md"
        _write_frontmatter_report(report, "20260601-idempotent")
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        r1 = convert_report_to_receipt(report, receipts_file=receipts_file, cache_window_seconds=300)
        r2 = convert_report_to_receipt(report, receipts_file=receipts_file, cache_window_seconds=300)

        assert r1 is not None
        assert r1.status == "appended"
        assert r2 is not None
        assert r2.status == "duplicate"
        # Only one physical receipt line
        assert _count_receipts(state_dir) == 1

    def test_malformed_report_no_crash(self, tmp_path, state_dir, caplog):
        report = tmp_path / "unknown.md"
        report.write_text("## No dispatch ID anywhere", encoding="utf-8")
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        import logging
        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is None
        assert _count_receipts(state_dir) == 0
        assert any("dispatch_id" in r.message or "skipping" in r.message for r in caplog.records)

    def test_unreadable_file_no_crash(self, tmp_path, state_dir):
        report = tmp_path / "nonexistent.md"
        # Do NOT create the file
        result = convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )
        assert result is None
        assert _count_receipts(state_dir) == 0


# ---------------------------------------------------------------------------
# Part 4: scan_and_convert() — directory scan
# ---------------------------------------------------------------------------

class TestScanAndConvert:
    def test_converts_new_reports(self, reports_dir, state_dir):
        _write_frontmatter_report(reports_dir / "20260601-scan-a.md", "20260601-scan-a")
        _write_frontmatter_report(reports_dir / "20260601-scan-b.md", "20260601-scan-b")

        n = scan_and_convert([reports_dir], state_dir)

        assert n == 2
        assert _count_receipts(state_dir) == 2

    def test_idempotent_rescan(self, reports_dir, state_dir):
        _write_frontmatter_report(reports_dir / "20260601-rescan.md", "20260601-rescan")

        n1 = scan_and_convert([reports_dir], state_dir)
        n2 = scan_and_convert([reports_dir], state_dir)

        assert n1 == 1
        assert n2 == 0  # watermark prevents re-emission
        assert _count_receipts(state_dir) == 1

    def test_malformed_report_skipped_no_crash(self, reports_dir, state_dir):
        # "unknown.md" → stem "unknown" is in the rejection list → no dispatch_id
        reports_dir.joinpath("unknown.md").write_text("No dispatch ID anywhere in this file.", encoding="utf-8")
        _write_frontmatter_report(reports_dir / "20260601-good.md", "20260601-good")

        n = scan_and_convert([reports_dir], state_dir)

        # Only the good report is counted
        assert n == 1
        assert _count_receipts(state_dir) == 1

    def test_nonexistent_dir_no_crash(self, state_dir, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        n = scan_and_convert([nonexistent], state_dir)
        assert n == 0

    def test_multiple_dirs(self, tmp_path, state_dir):
        dir_a = tmp_path / "reports_a"
        dir_b = tmp_path / "reports_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_frontmatter_report(dir_a / "20260601-a.md", "20260601-a")
        _write_frontmatter_report(dir_b / "20260601-b.md", "20260601-b")

        n = scan_and_convert([dir_a, dir_b], state_dir)
        assert n == 2


# ---------------------------------------------------------------------------
# Part 5: watermark persistence
# ---------------------------------------------------------------------------

class TestWatermarkPersistence:
    def test_watermark_file_created(self, reports_dir, state_dir):
        _write_frontmatter_report(reports_dir / "20260601-wm.md", "20260601-wm")
        scan_and_convert([reports_dir], state_dir)

        wm = state_dir / _WATERMARK_FILENAME
        assert wm.exists()
        hashes = _load_watermark(wm)
        assert len(hashes) == 1

    def test_watermark_prevents_rescan_new_instance(self, reports_dir, state_dir):
        report = reports_dir / "20260601-persist.md"
        _write_frontmatter_report(report, "20260601-persist")

        # First scan
        scan_and_convert([reports_dir], state_dir)

        # Second scan with fresh in-memory state (simulates restart)
        # The watermark file on disk prevents re-processing
        n2 = scan_and_convert([reports_dir], state_dir)
        assert n2 == 0
        assert _count_receipts(state_dir) == 1

    def test_bash_watermark_sync_skips_report(self, reports_dir, state_dir):
        """Reports in processed_receipts.txt (Bash watermark) are skipped."""
        report = reports_dir / "20260601-bash.md"
        _write_frontmatter_report(report, "20260601-bash")
        file_hash = _compute_sha256(report)

        # Pre-populate the Bash watermark
        bash_wm = state_dir / _BASH_WATERMARK_FILENAME
        bash_wm.write_text(file_hash + "\n", encoding="utf-8")

        n = scan_and_convert([reports_dir], state_dir)

        # The Python converter sees it in the Bash watermark → skips
        assert n == 0
        assert _count_receipts(state_dir) == 0

        # Our watermark is synced (so future scans also skip)
        py_wm = _load_watermark(state_dir / _WATERMARK_FILENAME)
        assert file_hash in py_wm


# ---------------------------------------------------------------------------
# Part 6: receipt content correctness
# ---------------------------------------------------------------------------

class TestReceiptContent:
    def test_receipt_has_required_fields(self, tmp_path, state_dir):
        report = tmp_path / "20260601-content-check.md"
        _write_frontmatter_report(
            report,
            "20260601-content-check",
            terminal="T2",
            model="claude-opus-4-8",
            status="success",
        )
        convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )
        r = _receipts(state_dir)[0]

        assert r["dispatch_id"] == "20260601-content-check"
        assert r["event_type"] == "task_complete"
        assert r["terminal"] == "T2"
        assert r["model"] == "claude-opus-4-8"
        assert r["status"] == "success"
        assert "timestamp" in r
        assert "report_path" in r

    def test_receipt_task_id_defaults_to_unknown(self, tmp_path, state_dir):
        report = tmp_path / "20260601-taskid.md"
        _write_frontmatter_report(report, "20260601-taskid")
        convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )
        r = _receipts(state_dir)[0]
        # task_id="unknown" aligns with report_parser.py default so
        # append_receipt_payload() idempotency key matches the Bash path's key.
        assert r.get("task_id") == "unknown"


# ---------------------------------------------------------------------------
# Part 7: contract validation before receipt emission
# ---------------------------------------------------------------------------

class TestContractValidation:
    """Report body contract is validated before emitting any receipt.

    Contract-VALID: dispatch_id in content + valid body -> task_complete.
    Contract-INVALID: missing content dispatch_id OR body violations ->
      report_contract_invalid (audit breadcrumb, never a clean completion).
    """

    def test_contract_valid_report_emits_task_complete(self, tmp_path, state_dir):
        report = tmp_path / "20260601-cv-valid.md"
        _write_frontmatter_report(report, "20260601-cv-valid")
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        assert result.status == "appended"
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_complete"
        assert r["status"] != "contract_invalid"

    def test_missing_content_dispatch_id_not_task_complete(self, tmp_path, state_dir):
        """Filename-only dispatch_id is a contract violation: must not be task_complete."""
        report = tmp_path / "20260601-nodid-content.md"
        # Full valid body but no frontmatter or bold-field dispatch_id
        report.write_text(
            "## Summary\n\n"
            "Implemented the feature per dispatch specification. All tests pass and coverage is at target.\n\n"
            "## Changes\n\n- scripts/lib/example.py: added X\n\n"
            "## Verification\n\npytest tests/ -x: 42 passed\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        # Must emit an audit breadcrumb — not silently drop
        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "report_contract_invalid"
        assert r["status"] == "contract_invalid"
        # dispatch_id falls back to filename for the audit key
        assert r["dispatch_id"] == "20260601-nodid-content"
        assert "missing_content_dispatch_id" in r["contract_violations"]

    def test_body_contract_violations_not_task_complete(self, tmp_path, state_dir):
        """Content dispatch_id present but body fails contract: must not be task_complete."""
        report = tmp_path / "20260601-badbody.md"
        # Has dispatch_id in frontmatter but missing required sections + summary too short
        report.write_text(
            "---\ndispatch_id: 20260601-badbody\nterminal: T1\n---\n\n"
            "## Summary\n\nShort.\n\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "report_contract_invalid"
        assert r["status"] == "contract_invalid"
        assert r["dispatch_id"] == "20260601-badbody"
        assert len(r["contract_violations"]) > 0

    def test_missing_sections_and_no_content_dispatch_id_not_task_complete(
        self, tmp_path, state_dir
    ):
        """Both content dispatch_id and body are invalid: must not be task_complete."""
        report = tmp_path / "20260601-double-invalid.md"
        report.write_text(
            "## Summary\n\nShort.\n\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "report_contract_invalid"
        assert r["dispatch_id"] == "20260601-double-invalid"
        violations = r["contract_violations"]
        assert "missing_content_dispatch_id" in violations

    def test_idempotency_holds_for_contract_invalid(self, tmp_path, state_dir):
        """contract_invalid receipts are idempotent: second call returns duplicate."""
        report = tmp_path / "20260601-idem-invalid.md"
        report.write_text(
            "---\ndispatch_id: 20260601-idem-invalid\n---\n\n"
            "## Summary\n\nShort.\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        r1 = convert_report_to_receipt(report, receipts_file=receipts_file, cache_window_seconds=300)
        r2 = convert_report_to_receipt(report, receipts_file=receipts_file, cache_window_seconds=300)

        assert r1 is not None
        assert r1.status == "appended"
        assert r2 is not None
        assert r2.status == "duplicate"
        # Only one physical receipt line even for contract_invalid
        assert _count_receipts(state_dir) == 1

    def test_scan_contract_invalid_not_counted_as_clean_completion(
        self, reports_dir, state_dir
    ):
        """scan_and_convert with a mixed set: contract-invalid reports leave an audit
        breadcrumb but the clean-completion count only reflects task_complete receipts."""
        _write_frontmatter_report(reports_dir / "20260601-sc-good.md", "20260601-sc-good")
        # Invalid report: has dispatch_id in frontmatter, body is missing sections
        (reports_dir / "20260601-sc-bad.md").write_text(
            "---\ndispatch_id: 20260601-sc-bad\n---\n\n## Summary\n\nShort.\n",
            encoding="utf-8",
        )

        n = scan_and_convert([reports_dir], state_dir)

        # Both get receipts emitted (appended), so n == 2
        assert n == 2
        receipts = _receipts(state_dir)
        assert len(receipts) == 2
        event_types = {r["event_type"] for r in receipts}
        assert "task_complete" in event_types
        assert "report_contract_invalid" in event_types
