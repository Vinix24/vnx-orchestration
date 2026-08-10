#!/usr/bin/env python3
"""Tests for report_to_receipt_converter — generic report→receipt conversion.

Covers:
  1. YAML-frontmatter report → exactly one receipt emitted
  2. Re-run on same report → idempotent (no second receipt)
  3. Malformed report (no dispatch_id) → skipped with warning, no crash
  4. scan_and_convert() over a directory of mixed reports
  5. Watermark persistence: already-processed reports skipped across calls
  6. Cross-processor dedup: converter reads AND writes the Bash processor's
     processed_receipts.txt (OI-1102: shared watermark, see test docstring
     for why the old separation was abandoned)
  7. Isolation: converter does NOT read/write the processor's mtime
     watermark (receipt_processor_watermark)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from report_to_receipt_converter import (
    _WATERMARK_FILENAME,
    _compute_sha256,
    _extract_body_fields,
    _is_known_dispatch,
    _load_route_decision,
    _load_watermark,
    build_receipt_from_report,
    convert_report_to_receipt,
    parse_frontmatter,
    scan_and_convert,
)

_POISONED_REPORT = Path(__file__).resolve().parent / "fixtures" / "poisoned_reports" / "plangate-p0-glm-harness.md"


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


def _v1_frontmatter_base(dispatch_id: str) -> dict:
    """Minimal v1-valid frontmatter mapping (15 required fields per schemas/unified_report_v1.json)."""
    return {
        "schema_version": 1,
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "terminal_id": "T1",
        "pool_id": "headless",
        "role": "identity_unresolved",  # canonical sentinel (dispatch-20260804-190000)
        "task_class": "implementation",
        "pr_id": "none",
        "duration_seconds": 12.5,
        "exit_code": 0,
        "token_usage": {"input": 1234, "output": 567, "cache_read": 89},
        "cost_usd": 0.0421,
        "route_decision": {
            "strategy": "default",
            "selected_provider": "claude",
            "selected_model": "claude-sonnet-4-6",
            "reason": "primary route",
        },
        "status": "unknown",
        "terminal": "T1",
        "timestamp": "2026-06-01T21:34:16Z",
    }


def _write_frontmatter_report(path: Path, dispatch_id: str, **extra) -> Path:
    """Write a well-formed v1-valid YAML-frontmatter report.

    Produces a report whose frontmatter validates against
    schemas/unified_report_v1.json so the fail-closed gate passes.
    ``**extra`` overrides any individual field (e.g. status, model).
    """
    fm = _v1_frontmatter_base(dispatch_id)
    fm.update(extra)
    fm_text = yaml.safe_dump(fm, sort_keys=False).strip()
    content = (
        f"---\n{fm_text}\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _write_non_v1_success_report(
    path: Path, dispatch_id: str, *, with_headings: bool = True, **extra
) -> Path:
    """Write a report with non-v1 (simple flat) frontmatter claiming success.

    Used to test that reports without v1 frontmatter get fail-closed rejection
    when they carry a terminal success status.
    """
    fields = {
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "provider": "claude",
        "model": "claude-sonnet-5",
        "status": "success",
        "timestamp": "2026-06-01T21:34:16Z",
        **extra,
    }
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fields.items())

    if with_headings:
        body = (
            "## Summary\n\nImplemented the feature per dispatch specification. "
            "All tests pass and coverage is at target.\n\n"
            "## Changes\n\n- scripts/lib/example.py: added X\n\n"
            "## Verification\n\npytest tests/ -x: 42 passed\n\n"
            "## Open Items\n\nNone\n"
        )
    else:
        body = "Echoed prompt — no real report content here.\n"

    content = f"---\n{fm_lines}\n---\n\n{body}"
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

        stats = scan_and_convert([reports_dir], state_dir)

        assert stats.new_count == 2
        assert _count_receipts(state_dir) == 2

    def test_idempotent_rescan(self, reports_dir, state_dir):
        _write_frontmatter_report(reports_dir / "20260601-rescan.md", "20260601-rescan")

        stats1 = scan_and_convert([reports_dir], state_dir)
        stats2 = scan_and_convert([reports_dir], state_dir)

        assert stats1.new_count == 1
        assert stats2.new_count == 0  # watermark prevents re-emission
        assert _count_receipts(state_dir) == 1

    def test_malformed_report_skipped_no_crash(self, reports_dir, state_dir):
        # "unknown.md" → stem "unknown" is in the rejection list → no dispatch_id
        reports_dir.joinpath("unknown.md").write_text("No dispatch ID anywhere in this file.", encoding="utf-8")
        _write_frontmatter_report(reports_dir / "20260601-good.md", "20260601-good")

        stats = scan_and_convert([reports_dir], state_dir)

        # Only the good report is counted as new; the malformed one is
        # counted separately (OI-998) and not marked processed.
        assert stats.new_count == 1
        assert stats.malformed_count == 1
        assert _count_receipts(state_dir) == 1

    def test_nonexistent_dir_no_crash(self, state_dir, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        stats = scan_and_convert([nonexistent], state_dir)
        assert stats.new_count == 0

    def test_multiple_dirs(self, tmp_path, state_dir):
        dir_a = tmp_path / "reports_a"
        dir_b = tmp_path / "reports_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_frontmatter_report(dir_a / "20260601-a.md", "20260601-a")
        _write_frontmatter_report(dir_b / "20260601-b.md", "20260601-b")

        stats = scan_and_convert([dir_a, dir_b], state_dir)
        assert stats.new_count == 2


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
        stats2 = scan_and_convert([reports_dir], state_dir)
        assert stats2.new_count == 0
        assert _count_receipts(state_dir) == 1

    def test_converter_respects_bash_watermark_cross_processor_dedup(
        self, reports_dir, state_dir
    ):
        """Cross-processor dedup: the converter reads the Bash processor's watermark.

        The converter and the Bash receipt processor (report_parser.py) both
        scan unified_reports/ and emit receipts to the same t0_receipts.ndjson.
        Before OI-1102, each processor maintained its own dedup watermark, so
        the same report file was processed by both — producing duplicate
        receipts for the same file (observed 2026-08-09 with dispatch
        20260809-fix1417-envelope-plan-test: two receipts at 12:38:49 and
        12:41:23 for the same report file, one from each processor).

        The old separation existed because the Bash processor could mark a
        report as "processed" before the converter had a chance to validate
        and enrich it — a half-processed Bash receipt would block the
        converter from ever seeing that report, leaving a gap in the audit
        trail.  Three later changes close that gap:

        1. The converter's fail-closed validation gate (OI-1035) ensures
           every receipt is complete before it is emitted — a half-processed
           receipt is no longer possible.
        2. Both processors now share one dedup watermark, so a report
           processed by either lane is skipped by the other.
        3. Dead-letter quarantining (OI-1102) routes unverifiable reports to
           receipt_deadletter/ instead of silently dropping them, preserving
           forensic access.

        The shared watermark eliminates cross-processor duplicates while the
        fail-closed gate and dead-letter quarantine together prevent the
        "silent skip" the old separation was designed to avoid.
        """
        report = reports_dir / "20260601-shared-dedup.md"
        _write_frontmatter_report(report, "20260601-shared-dedup")
        file_hash = _compute_sha256(report)

        # Pre-populate the Bash watermark (processed_receipts.txt) with the
        # report's hash — simulating that the Bash processor already handled it.
        bash_wm = state_dir / "processed_receipts.txt"
        bash_wm.write_text(file_hash + "\n", encoding="utf-8")

        stats = scan_and_convert([reports_dir], state_dir)

        # Converter skips the report because the Bash processor already processed
        # it — cross-processor dedup is the intended OI-1102 behavior.
        assert stats.new_count == 0
        assert _count_receipts(state_dir) == 0

        # Converter's own watermark is NOT populated: it never converted the
        # report, so it has nothing to watermark.
        py_wm = _load_watermark(state_dir / _WATERMARK_FILENAME)
        assert file_hash not in py_wm

        # Verify the other direction: when the converter processes a report,
        # it writes to BOTH watermarks so the Bash processor also skips it.
        report2 = reports_dir / "20260601-shared-dedup-2.md"
        _write_frontmatter_report(report2, "20260601-shared-dedup-2")
        file_hash2 = _compute_sha256(report2)

        stats2 = scan_and_convert([reports_dir], state_dir)
        assert stats2.new_count == 1
        assert _count_receipts(state_dir) == 1

        # Both watermarks now contain the new report's hash.
        py_wm2 = _load_watermark(state_dir / _WATERMARK_FILENAME)
        bash_wm2 = _load_watermark(state_dir / "processed_receipts.txt")
        assert file_hash2 in py_wm2
        assert file_hash2 in bash_wm2

        # Rescan: both watermarks prevent re-emission.
        stats3 = scan_and_convert([reports_dir], state_dir)
        assert stats3.new_count == 0
        assert _count_receipts(state_dir) == 1

    def test_converter_does_not_read_mtime_watermark(self, reports_dir, state_dir):
        """The processor's receipt_processor_watermark is never consulted.

        The Bash processor uses receipt_processor_watermark as an mtime
        watermark.  The converter must never read or write it — the two
        systems own separate dedup stores.
        """
        mtime_wm = state_dir / "receipt_processor_watermark"
        # Pre-populate with a bogus mtime value.
        mtime_wm.write_text("9999999999\n", encoding="utf-8")

        report = reports_dir / "20260601-mtime-isolation.md"
        _write_frontmatter_report(report, "20260601-mtime-isolation")

        stats = scan_and_convert([reports_dir], state_dir)

        # Converter processes the report — it does not read the mtime watermark.
        assert stats.new_count == 1

        # The mtime watermark file is untouched by the converter.
        assert mtime_wm.read_text(encoding="utf-8").strip() == "9999999999"


# ---------------------------------------------------------------------------
# Part 6: receipt content correctness
# ---------------------------------------------------------------------------

class TestReceiptContent:
    def test_receipt_has_required_fields(self, tmp_path, state_dir, monkeypatch):
        import report_to_receipt_converter as rtc
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)

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
        # dispatch-20260802-model-ssot-en-ketenlink: receipts carry the canonical
        # wave7 registry key, not the free-form claude-* string.
        assert r["model"] == "opus-4-8"
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
        # Full valid body but no frontmatter or bold-field dispatch_id. A model
        # IS carried (fail-closed model check: dispatch receipts must name the
        # model that ran); the contract violation is the missing dispatch_id.
        report.write_text(
            "---\nmodel: claude-sonnet-5\n---\n\n"
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
        # Has dispatch_id in frontmatter but missing required sections + summary
        # too short. A model is carried (fail-closed model check); the contract
        # violations are the missing sections.
        report.write_text(
            "---\ndispatch_id: 20260601-badbody\nterminal: T1\nmodel: claude-sonnet-5\n---\n\n"
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
            "---\nmodel: claude-sonnet-5\n---\n\n"
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
            "---\ndispatch_id: 20260601-idem-invalid\nmodel: claude-sonnet-5\n---\n\n"
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
            "---\ndispatch_id: 20260601-sc-bad\nmodel: claude-sonnet-5\n---\n\n## Summary\n\nShort.\n",
            encoding="utf-8",
        )

        stats = scan_and_convert([reports_dir], state_dir)

        # Both get receipts emitted (appended), so new_count == 2
        assert stats.new_count == 2
        receipts = _receipts(state_dir)
        assert len(receipts) == 2
        event_types = {r["event_type"] for r in receipts}
        assert "task_complete" in event_types
        assert "report_contract_invalid" in event_types


# ---------------------------------------------------------------------------
# Part 7b: dispatch identity propagation (receipt-quality PR-4)
# ---------------------------------------------------------------------------

class TestIdentityPropagation:
    """Converter receipts carry the real role from dispatch_metadata via
    dispatch_identity.resolve_dispatch_role; fail-open to identity_unresolved
    (never "unknown", never the fake backend-developer literal).
    """

    def _make_metadata_db(self, state_dir: Path, rows) -> None:
        import sqlite3
        db_path = state_dir / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE dispatch_metadata ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " dispatch_id TEXT NOT NULL,"
            " project_id TEXT NOT NULL,"
            " role TEXT"
            ")"
        )
        for dispatch_id, project_id, role in rows:
            conn.execute(
                "INSERT INTO dispatch_metadata (dispatch_id, project_id, role) VALUES (?, ?, ?)",
                (dispatch_id, project_id, role),
            )
        conn.commit()
        conn.close()

    def test_receipt_carries_real_role_when_db_has_one(self, tmp_path, state_dir):
        dispatch_id = "20260728-pr4-real-role"
        self._make_metadata_db(state_dir, [(dispatch_id, "vnx-dev", "debugger")])
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id, project_id="vnx-dev")

        result = convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["role"] == "debugger"
        assert r["receipt_kind"] == "dispatch"

    def test_receipt_stamps_identity_unresolved_when_no_metadata(self, tmp_path, state_dir):
        dispatch_id = "20260728-pr4-unresolved"
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id)

        result = convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["role"] == "identity_unresolved"
        assert r["role"] != "unknown"

    def test_receipt_never_propagates_fake_default(self, tmp_path, state_dir):
        dispatch_id = "20260728-pr4-fake-default"
        # The DB holds the canonical sentinel — it must still be filtered
        # (never propagated as a real role, even when it IS in the DB).
        self._make_metadata_db(state_dir, [(dispatch_id, "vnx-dev", "identity_unresolved")])
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id, project_id="vnx-dev")

        result = convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["role"] == "identity_unresolved"

    def test_role_resolution_error_fails_open(self, tmp_path, state_dir, monkeypatch):
        import dispatch_identity
        monkeypatch.setattr(
            dispatch_identity,
            "resolve_dispatch_role",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        dispatch_id = "20260728-pr4-failopen"
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id, project_id="vnx-dev")

        result = convert_report_to_receipt(
            report, receipts_file=str(state_dir / "t0_receipts.ndjson")
        )

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["role"] == "identity_unresolved"


# ---------------------------------------------------------------------------
# Part 8: smart_router strategy-tag detection (PR-SR-FIX-1)
# ---------------------------------------------------------------------------

class TestSmartRouterStrategyTag:
    """Receipt gets route_decision enrichment when per-dispatch JSON exists.

    smart_router.write_route_decision() writes
    state_dir/route_decisions/<dispatch_id>.json with strategy='smart_router'.
    The converter reads it back so the receipt reflects the actual routing
    strategy instead of the default 'default' tag from governance_emit.
    """

    def _write_route_decision_json(
        self, state_dir: Path, dispatch_id: str, task_class: str, model_id: str
    ) -> None:
        """Write a per-dispatch route decision JSON as smart_router would."""
        rd_dir = state_dir / "route_decisions"
        rd_dir.mkdir(parents=True, exist_ok=True)
        (rd_dir / f"{dispatch_id}.json").write_text(
            json.dumps({
                "strategy": "smart_router",
                "task_class": task_class,
                "selected_model": model_id,
                "timestamp": "2026-06-03T19:45:00Z",
            }),
            encoding="utf-8",
        )

    def test_receipt_contains_smart_router_strategy_when_route_decision_exists(
        self, tmp_path, state_dir
    ):
        """Converting a report where a route decision JSON exists sets strategy=smart_router."""
        dispatch_id = "20260603-sr-strategy-test"
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id)
        self._write_route_decision_json(
            state_dir, dispatch_id, "02_code_review", "claude-opus-4-6"
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        assert result.status == "appended"
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_complete"
        assert "route_decision" in r
        assert r["route_decision"]["strategy"] == "smart_router"
        assert r["route_decision"]["task_class"] == "02_code_review"
        assert r["route_decision"]["selected_model"] == "claude-opus-4-6"

    def test_receipt_has_no_route_decision_when_json_absent(self, tmp_path, state_dir):
        """When no route decision JSON exists, receipt must not have route_decision key."""
        dispatch_id = "20260603-sr-no-decision"
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id)
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert "route_decision" not in r

    def test_load_route_decision_returns_none_for_missing_file(self, state_dir):
        """_load_route_decision returns None gracefully when no file exists."""
        result = _load_route_decision("nonexistent-dispatch", state_dir)
        assert result is None

    def test_malformed_route_decision_json_logs_warning(self, tmp_path, state_dir, caplog):
        """Malformed route decision JSON triggers logger.warning (ADR-021 no silent swallow)."""
        import logging
        dispatch_id = "20260603-sr-malformed-json"
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(report, dispatch_id)

        rd_dir = state_dir / "route_decisions"
        rd_dir.mkdir(parents=True, exist_ok=True)
        (rd_dir / f"{dispatch_id}.json").write_text("{not valid json", encoding="utf-8")

        receipts_file = str(state_dir / "t0_receipts.ndjson")

        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        assert result.status == "appended"
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_complete"
        assert "route_decision" not in r
        assert any(
            "route_decision lookup failed" in rec.message
            and dispatch_id in rec.message
            for rec in caplog.records
        )

    def test_load_route_decision_returns_none_for_malformed_json(self, state_dir):
        """_load_route_decision returns None without raising on corrupt JSON."""
        rd_dir = state_dir / "route_decisions"
        rd_dir.mkdir(parents=True, exist_ok=True)
        (rd_dir / "bad-dispatch.json").write_text("not valid json{{", encoding="utf-8")

        result = _load_route_decision("bad-dispatch", state_dir)
        assert result is None


# ---------------------------------------------------------------------------
# Part 9: poisoned-report resilience (OI-997) — dispatch-20260804-064708-pra-
# converter-resilience
# ---------------------------------------------------------------------------

class TestPoisonedReportResilience:
    """A single crashed report must not crash the whole converter scan.

    ``tests/fixtures/poisoned_reports/plangate-p0-glm-harness.md`` is a copy
    of the real quarantined report that stopped receipt emission entirely on
    2026-08-03 18:36: a ``**bold-key**`` whose value falls entirely inside
    the ``text[:3000]`` scan window as trailing whitespace only —
    ``group(2).strip()`` reduces to ``""``, and the old
    ``.splitlines()[0]`` raised ``IndexError`` before the guard added here.
    """

    def test_poisoned_report_does_not_crash_extract_body_fields(self):
        text = _POISONED_REPORT.read_text(encoding="utf-8")
        fields = _extract_body_fields(text)  # must not raise
        assert isinstance(fields, dict)

    def test_poisoned_report_does_not_crash_build_receipt_from_report(self, tmp_path):
        text = _POISONED_REPORT.read_text(encoding="utf-8")
        dest = tmp_path / _POISONED_REPORT.name
        dest.write_text(text, encoding="utf-8")

        receipt = build_receipt_from_report(dest, text)  # must not raise

        assert receipt is not None
        assert receipt["dispatch_id"] == "plangate-p0-glm-harness"

    def test_one_poisoned_report_does_not_block_the_batch(self, reports_dir, state_dir):
        """A dir with 1 poisoned + 2 healthy reports must yield 3 receipts —
        one per report, none dropped.

        Before the OI-997 fix, the poisoned report's IndexError propagated
        out of build_receipt_from_report() uncaught inside
        scan_and_convert()'s loop, aborting the ENTIRE scan — every report
        after the poisoned one in sort order was never even attempted. This
        is the literal 2026-08-03 18:36 incident (300+ reports, 0 receipts
        emitted until the poisoned file was quarantined by hand).

        Sorted glob order places the poisoned file BEFORE both healthy
        reports (`plangate-...` < `zzz-healthy-...`) so a regression here
        (no try/except around the per-report conversion) reproduces the
        original all-or-nothing failure, not a partial one.

        The poisoned report itself lands as a "report_contract_invalid"
        receipt (its body lacks the required ## Summary/## Changes/etc
        headings — a separate, pre-existing contract check, not this
        dispatch's concern) rather than "task_complete"; the point proven
        here is that it no longer crashes the SCAN, so the two healthy
        reports after it still get their receipts.
        """
        shutil.copy(_POISONED_REPORT, reports_dir / _POISONED_REPORT.name)
        _write_frontmatter_report(reports_dir / "zzz-healthy-a.md", "zzz-healthy-a")
        _write_frontmatter_report(reports_dir / "zzz-healthy-b.md", "zzz-healthy-b")

        stats = scan_and_convert([reports_dir], state_dir)

        assert stats.new_count == 3
        assert stats.error_count == 0
        assert _count_receipts(state_dir) == 3
        receipts = _receipts(state_dir)
        event_types = {r["event_type"] for r in receipts}
        assert "task_complete" in event_types
        assert "report_contract_invalid" in event_types


class TestScanCrashGuard:
    """OI-997 point 2: scan_and_convert()'s OWN try/except around the
    per-report call site must survive ANY exception — not only the specific
    IndexError fixed in _extract_body_fields() / guarded inside
    _convert_one_detailed(). Simulates a bug that bypasses those inner
    guards entirely to prove the outer guard in scan_and_convert() itself
    is what is being tested here.
    """

    def test_unhandled_exception_in_one_report_does_not_abort_the_batch(
        self, reports_dir, state_dir, monkeypatch, caplog
    ):
        import report_to_receipt_converter as rtc

        _write_frontmatter_report(reports_dir / "20260601-outer-a.md", "20260601-outer-a")
        _write_frontmatter_report(reports_dir / "20260601-outer-b.md", "20260601-outer-b")
        _write_frontmatter_report(reports_dir / "20260601-outer-c.md", "20260601-outer-c")

        real_convert_one = rtc._convert_one_detailed

        def _boom(report_path, **kwargs):
            if "outer-b" in report_path.name:
                raise RuntimeError("simulated bug bypassing _convert_one_detailed's own guards")
            return real_convert_one(report_path, **kwargs)

        monkeypatch.setattr(rtc, "_convert_one_detailed", _boom)

        with caplog.at_level(logging.ERROR, logger="report_to_receipt_converter"):
            stats = rtc.scan_and_convert([reports_dir], state_dir)

        assert stats.new_count == 2
        assert stats.error_count == 1
        assert _count_receipts(state_dir) == 2
        assert any(
            "unhandled exception" in r.message and "outer-b" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Part 10: fail-closed rejection must be counted and loud (OI-998)
# ---------------------------------------------------------------------------

def _write_report_without_model(path: Path, dispatch_id: str) -> Path:
    """A well-formed dispatch report that omits Model — triggers the
    fail-closed rejection in append_receipt_internals/validation.py."""
    path.write_text(
        f"---\ndispatch_id: {dispatch_id}\nprovider: claude\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n",
        encoding="utf-8",
    )
    return path


class TestRejectionVisibility:
    """A fail-closed rejection (no real Model) must be counted separately
    from malformed/errored conversions and logged loudly (WARNING with
    dispatch-id + reason) — not disappear as a silent None."""

    def test_rejected_report_counted_separately_and_logs_warning(
        self, reports_dir, state_dir, caplog
    ):
        _write_report_without_model(reports_dir / "20260601-no-model.md", "20260601-no-model")

        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            stats = scan_and_convert([reports_dir], state_dir)

        assert stats.rejected_count == 1
        assert stats.new_count == 0
        assert stats.malformed_count == 0
        assert stats.error_count == 0
        assert _count_receipts(state_dir) == 0
        assert any(
            "REJECTED" in r.message
            and "20260601-no-model" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_rejected_report_retried_on_next_scan_not_watermarked(self, reports_dir, state_dir):
        """A rejected report must NOT be marked processed — it is retried
        every scan until the cause (missing Model) is fixed."""
        _write_report_without_model(reports_dir / "20260601-retry-me.md", "20260601-retry-me")

        stats1 = scan_and_convert([reports_dir], state_dir)
        stats2 = scan_and_convert([reports_dir], state_dir)

        assert stats1.rejected_count == 1
        assert stats2.rejected_count == 1  # retried, not silently watermarked away

    def test_convert_report_to_receipt_returns_none_for_rejected(
        self, tmp_path, state_dir, caplog
    ):
        """convert_report_to_receipt() keeps its exact Optional[AppendResult]
        contract for the 'rejected' outcome too — existing callers (the tmux
        stop-hook, direct-call tests) need no changes for this dispatch."""
        report = _write_report_without_model(tmp_path / "20260601-direct-reject.md", "20260601-direct-reject")

        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            result = convert_report_to_receipt(
                report, receipts_file=str(state_dir / "t0_receipts.ndjson")
            )

        assert result is None
        assert any("REJECTED" in r.message for r in caplog.records)

    def test_zero_receipts_scan_flips_health_beacon_to_fail(self, reports_dir, state_dir):
        """The scan's counts land on the existing HealthBeacon channel
        (health/report_to_receipt_converter.json — health_beacon.py, the
        same mechanism producer_freshness_monitor.py uses for its own
        heartbeat) so a scan that attempted conversions but produced zero
        receipts reads as unhealthy, not as a quiet no-op."""
        _write_report_without_model(reports_dir / "20260601-no-model-2.md", "20260601-no-model-2")

        scan_and_convert([reports_dir], state_dir)

        beacon_path = state_dir / "health" / "report_to_receipt_converter.json"
        assert beacon_path.exists()
        beacon = json.loads(beacon_path.read_text(encoding="utf-8"))
        assert beacon["status"] == "fail"
        assert beacon["details"]["rejected_count"] == 1
        assert beacon["details"]["new_count"] == 0

    def test_scan_with_at_least_one_success_keeps_beacon_ok(self, reports_dir, state_dir):
        """A rejection alongside a successful receipt is normal, expected,
        contract-driven behavior — it must NOT flip the whole scan unhealthy."""
        _write_report_without_model(reports_dir / "20260601-no-model-3.md", "20260601-no-model-3")
        _write_frontmatter_report(reports_dir / "20260601-healthy-c.md", "20260601-healthy-c")

        stats = scan_and_convert([reports_dir], state_dir)
        assert stats.new_count == 1
        assert stats.rejected_count == 1

        beacon_path = state_dir / "health" / "report_to_receipt_converter.json"
        beacon = json.loads(beacon_path.read_text(encoding="utf-8"))
        assert beacon["status"] == "ok"


# ---------------------------------------------------------------------------
# Part 11: fail-closed gate — OI-1035, OI-1011, OI-1002, OI-659, OI-1017
# ---------------------------------------------------------------------------


class TestFailClosedV1Frontmatter:
    """Check (a): a report with terminal success status but no v1 frontmatter
    must produce an explicit failure receipt, not a silent success."""

    def test_non_v1_frontmatter_with_success_status_becomes_failure(
        self, tmp_path, state_dir
    ):
        report = tmp_path / "20260601-no-v1.md"
        _write_non_v1_success_report(report, "20260601-no-v1")
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_failed"
        assert r["status"] == "failure"
        assert "fail_closed_violations" in r
        violations = r["fail_closed_violations"]
        assert any("frontmatter_v1" in v for v in violations), (
            f"expected frontmatter_v1 violation, got: {violations}"
        )

    def test_v1_frontmatter_with_success_status_passes(self, tmp_path, state_dir, monkeypatch):
        import report_to_receipt_converter as rtc
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)

        report = tmp_path / "20260601-v1-ok.md"
        _write_frontmatter_report(report, "20260601-v1-ok", status="success")
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_complete"
        assert r["status"] == "success"

    def test_non_v1_with_unknown_status_not_affected(self, tmp_path, state_dir):
        """Reports with non-terminal-success status (unknown) keep existing behavior."""
        report = tmp_path / "20260601-unknown-status.md"
        report.write_text(
            "---\ndispatch_id: 20260601-unknown-status\n"
            "provider: claude\nmodel: claude-sonnet-5\nstatus: unknown\n---\n\n"
            "## Summary\n\nImplemented the feature per dispatch specification. "
            "All tests pass and coverage is at target.\n\n"
            "## Changes\n\n- scripts/lib/example.py: added X\n\n"
            "## Verification\n\npytest tests/ -x: 42 passed\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        # "unknown" is not terminal success → passes through as task_complete
        assert r["event_type"] == "task_complete"
        assert r["status"] == "unknown"


class TestFailClosedBodyContract:
    """Check (b): a report claiming terminal success but missing mandatory
    headings must produce an explicit failure, not contract_invalid."""

    def test_success_status_without_headings_becomes_failure(
        self, tmp_path, state_dir
    ):
        """A report that echoes the prompt back misses the headings entirely.
        Even with v1 frontmatter, if the body is garbage the success claim fails."""
        report = tmp_path / "20260601-echoed.md"
        report.write_text(
            "---\ndispatch_id: 20260601-echoed\n"
            "provider: claude\nmodel: claude-sonnet-5\nstatus: success\n"
            "terminal: T1\n---\n\n"
            "Echoed dispatch prompt — the worker echoed the instruction "
            "back instead of writing a real report. No headings at all.\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_failed"
        assert r["status"] == "failure"
        violations = r["fail_closed_violations"]
        assert any("body_contract" in v for v in violations), (
            f"expected body_contract violation, got: {violations}"
        )

    def test_success_status_with_too_short_summary_becomes_failure(
        self, tmp_path, state_dir
    ):
        """Headings present but summary < 50 chars must still fail."""
        report = tmp_path / "20260601-short-summary.md"
        report.write_text(
            "---\ndispatch_id: 20260601-short-summary\n"
            "provider: claude\nmodel: claude-sonnet-5\nstatus: success\n"
            "terminal: T1\n---\n\n"
            "## Summary\n\nShort.\n\n"
            "## Changes\n\nNone.\n\n"
            "## Verification\n\nNone.\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_failed"
        assert r["status"] == "failure"
        violations = r["fail_closed_violations"]
        assert any("body_contract" in v for v in violations), (
            f"expected body_contract violation, got: {violations}"
        )


class TestFailClosedBranchNotOnOrigin:
    """Check (c): a dispatch with a terminal success status whose branch
    does not exist on origin must produce an explicit failure.

    This is the core OI-1011 signal: worker committed locally, never pushed,
    worktree was reaped, deur exit 0.  The test uses real ``git ls-remote``,
    never a mock — a mock proves nothing about whether the check bites.
    """

    def test_branch_not_on_origin_rejects_success(self, tmp_path, state_dir):
        """Use a dispatch_id that cannot possibly exist on origin."""
        report = tmp_path / "20260601-no-branch.md"
        _write_frontmatter_report(
            report,
            "20260804-zzz-nonexistent-branch-oi1011",
            status="success",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_failed"
        assert r["status"] == "failure"
        violations = r["fail_closed_violations"]
        assert any("branch_not_on_origin" in v for v in violations), (
            f"expected branch_not_on_origin violation, got: {violations}"
        )

    def test_branch_check_uses_real_git_ls_remote(self, tmp_path):
        """Directly test _check_branch_on_origin with a real git call.

        A branch named after a random UUID cannot exist on origin.  The call
        must return False (branch not found), proving the check does real I/O.
        """
        import uuid
        from report_to_receipt_converter import _check_branch_on_origin

        fake_dispatch_id = f"20260804-{uuid.uuid4().hex[:12]}-does-not-exist"
        result = _check_branch_on_origin(fake_dispatch_id)
        assert result is False, (
            f"Expected _check_branch_on_origin to return False for "
            f"nonexistent branch dispatch/{fake_dispatch_id}, got {result}"
        )

    def test_existing_branch_passes_check(self):
        """A branch that DOES exist on origin returns True.

        Finds any dispatch branch on origin via ``git ls-remote`` as a
        positive control — no hardcoded branch name, no assumption that
        the current worktree has been pushed.
        """
        from report_to_receipt_converter import _check_branch_on_origin

        cwd = str(Path(__file__).resolve().parent.parent)
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        if result.returncode != 0 or not result.stdout.strip():
            pytest.skip("git ls-remote origin returned nothing — cannot run positive test")

        # Find any dispatch/ branch on origin to use as a positive control.
        for line in result.stdout.strip().splitlines():
            # Format: <hash>\trefs/heads/dispatch/<name>
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            ref = parts[1]
            if not ref.startswith("refs/heads/dispatch/"):
                continue
            dispatch_id = ref[len("refs/heads/dispatch/"):]
            exists = _check_branch_on_origin(dispatch_id)
            assert exists is True, (
                f"Expected branch dispatch/{dispatch_id} to exist on "
                f"origin, but _check_branch_on_origin returned False"
            )
            return  # Found one — test passes

        pytest.skip("No dispatch/* branch found on origin for positive control")

    def test_non_terminal_status_skips_branch_check(self, tmp_path, state_dir):
        """Reports with status='unknown' skip the fail-closed gate entirely,
        so a nonexistent branch does NOT block them."""
        report = tmp_path / "20260601-unknown-branch.md"
        _write_frontmatter_report(
            report,
            "20260804-zzz-skipped-branch-check",
            status="unknown",
        )
        receipts_file = str(state_dir / "t0_receipts.ndjson")

        result = convert_report_to_receipt(report, receipts_file=receipts_file)

        assert result is not None
        r = _receipts(state_dir)[0]
        assert r["event_type"] == "task_complete"
        assert r["status"] == "unknown"


# ---------------------------------------------------------------------------
# OI-1017/OI-1048: converter double-counting guard
# ---------------------------------------------------------------------------


class TestConverterDoubleCountGuard:
    """When the envelope hot-path already wrote a contract_invalid receipt,
    the converter must not process the same report again — the NDJSON guard
    short-circuits before append_receipt_payload."""

    def test_existing_contract_invalid_skips_conversion(
        self, tmp_path, state_dir, monkeypatch,
    ):
        """Pre-populate the NDJSON with a contract_invalid receipt and verify
        the converter detects the duplicate and skips new work."""
        import json

        dispatch_id = "20260809-guard-test"
        receipts_file = state_dir / "t0_receipts.ndjson"

        # Simulate the hot-path having already written a contract_invalid receipt.
        # Use compact JSON (separators) matching how append_receipt_payload
        # serialises receipts.  The old test used json.dumps() defaults which
        # happened to pass the old substring needle — the compact form is the
        # real format on disk and the parsed-field check handles both.
        existing_receipt = json.dumps({
            "dispatch_id": dispatch_id,
            "event_type": "report_contract_invalid",
            "status": "contract_invalid",
            "provider": "codex",
            "model": "gpt-test",
            "terminal": "T1",
            "receipt_kind": "dispatch",
            "role": "identity_unresolved",
            "task_id": "unknown",
            "timestamp": "2026-08-09T00:00:00Z",
            "report_path": f"unified_reports/{dispatch_id}.md",
            "contract_violations": [
                "## Summary",
                "## Changes",
                "## Verification",
                "## Open Items",
            ],
        }, separators=(",", ":"))
        receipts_file.write_text(existing_receipt + "\n", encoding="utf-8")

        # Write a report that the converter would normally process.
        report = tmp_path / f"{dispatch_id}.md"
        _write_frontmatter_report(
            report, dispatch_id, status="success",
        )

        result = convert_report_to_receipt(
            report, receipts_file=str(receipts_file),
        )

        # The guard detected the existing receipt and returned a duplicate
        # result — semantically correct per the function's contract (OI-1110:
        # the guard now actually triggers after the needle fix, and returns
        # a proper AppendResult instead of None).
        assert result is not None, (
            "expected converter to return a duplicate result, got None"
        )
        assert result.status == "duplicate", (
            f"expected duplicate status, got {result.status}"
        )

        # The NDJSON must still have exactly 1 line (the original).
        lines = receipts_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, (
            f"expected 1 receipt line (original), got {len(lines)}"
        )

    def test_no_existing_receipt_still_converts(
        self, tmp_path, state_dir,
    ):
        """Without a pre-existing receipt, the converter must still process
        normally (regression guard — the skip check must not false-positive)."""
        dispatch_id = "20260809-guard-no-existing"
        receipts_file = state_dir / "t0_receipts.ndjson"

        # Write a report — no existing receipt in NDJSON.
        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            "---\ndispatch_id: 20260809-guard-no-existing\n"
            "provider: codex\nmodel: gpt-test\nstatus: unknown\n"
            "terminal: T1\n---\n\n"
            "## Summary\n\nNo existing receipt — this should process normally, "
            "with a summary longer than fifty characters.\n\n"
            "## Changes\n\nNone.\n\n"
            "## Verification\n\nNone.\n\n"
            "## Open Items\n\nNone.\n\n"
            "**Model**: codex\n",
            encoding="utf-8",
        )

        result = convert_report_to_receipt(
            report, receipts_file=str(receipts_file),
        )

        # Normal processing — result is not None.
        assert result is not None, (
            "converter should process normally when no existing receipt"
        )
        lines = receipts_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1, "expected at least one receipt line"


# ---------------------------------------------------------------------------
# Part 12: _is_known_dispatch — dispatch register cross-check (OI-1110)
# ---------------------------------------------------------------------------

class TestIsKnownDispatch:
    """_is_known_dispatch reads the NDJSON dispatch register and checks
    whether a dispatch_id is present.  OI-1110: the original implementation
    used a raw substring search with ``"dispatch_id": "<value>"`` (space
    after colon), but the register is written as compact JSON without spaces
    (``separators=(",", ":")`` in ``register_emit.py``) — so the needle
    could NEVER match and every lookup returned False.
    """

    def _write_register(self, state_dir: Path, entries: list) -> Path:
        """Write a dispatch_register.ndjson with compact JSON (no spaces)."""
        import json
        reg = state_dir / "dispatch_register.ndjson"
        with reg.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=False))
                fh.write("\n")
        return reg

    def test_known_dispatch_returns_true(self, state_dir):
        """dispatch_id present in the register (compact JSON) -> True."""
        self._write_register(state_dir, [
            {"timestamp": "2026-08-09T18:11:01Z", "event": "dispatch_created",
             "dispatch_id": "20260808-t0-gate-pr1416", "project_id": "vnx-dev"},
            {"timestamp": "2026-08-09T18:12:00Z", "event": "dispatch_created",
             "dispatch_id": "20260809e-fix-something", "project_id": "vnx-dev"},
        ])

        result = _is_known_dispatch("20260808-t0-gate-pr1416", state_dir)
        assert result is True, (
            "_is_known_dispatch returned False for a dispatch_id "
            "that IS in the register (compact JSON)"
        )

    def test_unknown_dispatch_returns_false(self, state_dir):
        """dispatch_id NOT in the register -> False."""
        self._write_register(state_dir, [
            {"timestamp": "2026-08-09T18:11:01Z", "event": "dispatch_created",
             "dispatch_id": "d-001", "project_id": "vnx-dev"},
        ])

        result = _is_known_dispatch("nonexistent-dispatch-id", state_dir)
        assert result is False, (
            "_is_known_dispatch returned True for a dispatch_id "
            "that is NOT in the register"
        )

    def test_missing_register_file_returns_true_fail_open(self, state_dir):
        """No register file -> True (fail-open)."""
        # state_dir is empty — no dispatch_register.ndjson
        result = _is_known_dispatch("anything", state_dir)
        assert result is True

    def test_empty_register_returns_false(self, state_dir):
        """Empty register file -> False (no match possible)."""
        reg = state_dir / "dispatch_register.ndjson"
        reg.write_text("", encoding="utf-8")

        result = _is_known_dispatch("anything", state_dir)
        assert result is False

    def test_dispatch_id_with_special_chars(self, state_dir):
        """dispatch_id values with hyphens, dots, underscores all match."""
        did = "20260809-abc.def_ghi-123"
        self._write_register(state_dir, [
            {"dispatch_id": did, "event": "dispatch_created"},
        ])

        result = _is_known_dispatch(did, state_dir)
        assert result is True

    def test_partial_id_no_false_match(self, state_dir):
        """A substring of a stored dispatch_id must NOT match."""
        self._write_register(state_dir, [
            {"dispatch_id": "20260809-full-id-abc", "event": "dispatch_created"},
        ])

        # "full-id" is a substring of "20260809-full-id-abc" — must NOT match
        result = _is_known_dispatch("full-id", state_dir)
        assert result is False, (
            "substring of stored dispatch_id must not produce a false match"
        )

    def test_malformed_line_skipped(self, state_dir):
        """A malformed NDJSON line is skipped without crashing the scan."""
        reg = state_dir / "dispatch_register.ndjson"
        import json
        good_line = json.dumps(
            {"dispatch_id": "good-one", "event": "dispatch_created"},
            separators=(",", ":"),
        )
        reg.write_text(
            good_line + "\n"
            "this is not valid json\n"
            + good_line + "\n",
            encoding="utf-8",
        )

        result = _is_known_dispatch("good-one", state_dir)
        assert result is True, "malformed line must not prevent matching a valid record"


# ---------------------------------------------------------------------------
# Part 13: provider normalisation + lane-identity resolution (OI-1111)
# ---------------------------------------------------------------------------


class TestNormaliseProvider:
    """_normalise_provider maps every known variant to one canonical lane value."""

    def test_canonical_passthrough(self):
        from report_to_receipt_converter import _normalise_provider
        for canonical in ("claude", "codex", "gemini", "kimi", "deepseek-harness",
                          "deepseek", "glm-harness"):
            assert _normalise_provider(canonical) == canonical

    def test_deepseek_variants_normalised(self):
        from report_to_receipt_converter import _normalise_provider
        assert _normalise_provider("litellm:deepseek") == "deepseek-harness"
        assert _normalise_provider("deepseek (harness, key-auth)") == "deepseek-harness"

    def test_kimi_variants_normalised(self):
        from report_to_receipt_converter import _normalise_provider
        assert _normalise_provider("kimi-k3") == "kimi"
        assert _normalise_provider("kimi-code/k3") == "kimi"

    def test_glm_variants_normalised(self):
        from report_to_receipt_converter import _normalise_provider
        assert _normalise_provider("glm-harness") == "glm-harness"
        assert _normalise_provider("litellm:zai") == "glm-harness"

    def test_empty_or_none_returns_unknown(self):
        from report_to_receipt_converter import _normalise_provider
        assert _normalise_provider("") == "unknown"
        assert _normalise_provider("  ") == "unknown"


class TestLaneIdentityResolution:
    """Provider and model come from the lane (route_decision), not the body.

    On harness lanes (GLM, DeepSeek) the worker introspects as sonnet/claude
    and stamps a false identity in the body.  The lane's route decision holds
    the authoritative identity; the body is only a fallback when no route
    decision exists.
    """

    def _write_route_decision_for(self, state_dir: Path, dispatch_id: str,
                                  selected_model: str) -> None:
        import json as _json
        rd_dir = state_dir / "route_decisions"
        rd_dir.mkdir(parents=True, exist_ok=True)
        (rd_dir / f"{dispatch_id}.json").write_text(_json.dumps({
            "strategy": "smart_router",
            "task_class": "03_bug_fix",
            "selected_model": selected_model,
            "timestamp": "2026-08-09T12:00:00Z",
        }), encoding="utf-8")

    def test_glm_harness_lane_wins_over_body_sonnet_claude(self, tmp_path):
        """Core OI-1111 case: route_decision says glm-5.2, body says sonnet/claude.
        The receipt must carry glm-harness/glm-5.2, not sonnet/claude."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        dispatch_id = "20260809d-w2-worker-permissieprofielen"
        self._write_route_decision_for(state_dir, dispatch_id, "glm-5.2")

        # Simulate the exact body the worker wrote: sonnet/claude.
        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            "---\ndispatch_id: 20260809d-w2-worker-permissieprofielen\n"
            "provider: claude\nmodel: sonnet\nstatus: success\n"
            "terminal: T2\n---\n\n"
            "## Summary\n\nImplemented the feature per dispatch specification. "
            "All tests pass and coverage is at target.\n\n"
            "## Changes\n\n- scripts/lib/example.py: added X\n\n"
            "## Verification\n\npytest tests/ -x: 42 passed\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        # Lane identity wins — not the body's sonnet/claude
        assert receipt["provider"] == "glm-harness", (
            f"Expected glm-harness from lane, got {receipt.get('provider')}"
        )
        assert receipt["model"] == "glm-5.2", (
            f"Expected glm-5.2 from lane, got {receipt.get('model')}"
        )

    def test_deepseek_harness_lane_wins(self, tmp_path):
        """Route decision for deepseek-v4-pro — receipt carries deepseek-harness."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        dispatch_id = "20260809-deepseek-test"
        self._write_route_decision_for(state_dir, dispatch_id, "deepseek-v4-pro")

        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            "---\ndispatch_id: 20260809-deepseek-test\n"
            "provider: claude\nmodel: sonnet\nstatus: success\nterminal: T1\n---\n\n"
            "## Summary\n\nDeepSeek harness worker that reports as claude/sonnet. "
            "The receipt must carry the lane identity, not the body.\n\n"
            "## Changes\n\n- scripts/lib/foo.py: edited\n\n"
            "## Verification\n\npytest tests/ -x: all green\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        assert receipt["provider"] == "deepseek-harness", (
            f"Expected deepseek-harness, got {receipt.get('provider')}"
        )
        assert receipt["model"] == "deepseek-v4-pro", (
            f"Expected deepseek-v4-pro, got {receipt.get('model')}"
        )

    def test_body_fallback_when_no_route_decision(self, tmp_path):
        """When no route decision JSON exists, body fields are the fallback."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        # No route_decision JSON written.

        report = tmp_path / "20260601-no-rd.md"
        report.write_text(
            "---\ndispatch_id: 20260601-no-rd\n"
            "provider: kimi\nmodel: kimi-k3\nstatus: success\nterminal: T2\n---\n\n"
            "## Summary\n\nReport without route decision — body values should be "
            "used (with normalisation).\n\n"
            "## Changes\n\n- scripts/lib/foo.py: edited\n\n"
            "## Verification\n\npytest tests/ -x: all green\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        # Body provider "kimi" normalises to "kimi" (already canonical).
        assert receipt["provider"] == "kimi"
        assert receipt["model"] == "kimi-k3"

    def test_body_provider_normalised_when_fallback(self, tmp_path):
        """When body is the fallback, provider strings are still normalised."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        report = tmp_path / "20260601-body-norm.md"
        report.write_text(
            "---\ndispatch_id: 20260601-body-norm\n"
            "provider: litellm:deepseek\nmodel: deepseek-v4-flash\nstatus: success\n"
            "terminal: T1\n---\n\n"
            "## Summary\n\nBody carries litellm:deepseek — must normalise to "
            "deepseek-harness. Summary with sufficient length for validation.\n\n"
            "## Changes\n\n- scripts/lib/foo.py: edited\n\n"
            "## Verification\n\npytest tests/ -x: all green\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        assert receipt["provider"] == "deepseek-harness"

    def test_kimi_lane_wins_and_normalises(self, tmp_path):
        """Route decision kimi-k3 → provider kimi, model kimi-k3."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        dispatch_id = "20260809-kimi-test"
        self._write_route_decision_for(state_dir, dispatch_id, "kimi-k3")

        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            "---\ndispatch_id: 20260809-kimi-test\n"
            "provider: kimi-k3\nmodel: kimi-k3\nstatus: success\nterminal: T1\n---\n\n"
            "## Summary\n\nKimi lane worker whose body provider kimi-k3 normalises "
            "to kimi. The lane is authoritative even when the body is close.\n\n"
            "## Changes\n\n- scripts/lib/foo.py: edited\n\n"
            "## Verification\n\npytest tests/ -x: all green\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        assert receipt["provider"] == "kimi"
        assert receipt["model"] == "kimi-k3"

    def test_claude_lane_model_preserved(self, tmp_path):
        """Claude route_decision is honest — lane still wins but values match."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        dispatch_id = "20260809-claude-test"
        self._write_route_decision_for(state_dir, dispatch_id, "claude-sonnet-4-6")

        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            "---\ndispatch_id: 20260809-claude-test\n"
            "provider: claude\nmodel: sonnet\nstatus: unknown\nterminal: T1\n---\n\n"
            "## Summary\n\nClaude lane is honest — lane values match body values "
            "for provider/model. Summary with adequate length to pass validation.\n\n"
            "## Changes\n\n- scripts/lib/foo.py: edited\n\n"
            "## Verification\n\npytest tests/ -x: all green\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        assert receipt["provider"] == "claude"
        # parse_route_model_id("claude-sonnet-4-6") splits on first hyphen:
        # variant = "claude-sonnet-4-6".split("-")[1] = "sonnet"
        assert receipt["model"] == "sonnet"

    def test_dispatch_type_b_matches_this_spec(self, tmp_path):
        """Prove the guard binds: WITHOUT route_decision the body lies."""
        from report_to_receipt_converter import build_receipt_from_report

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        dispatch_id = "20260809d-w2-worker-permissieprofielen"
        # Deliberately NO route_decision written — simulate body-only path.
        # This is the pre-fix behaviour: body says sonnet/claude, receipt
        # believes it. The lane-resolution fix above is what corrects this.

        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            "---\ndispatch_id: 20260809d-w2-worker-permissieprofielen\n"
            "provider: claude\nmodel: sonnet\nstatus: success\nterminal: T2\n---\n\n"
            "## Summary\n\nPre-fix behaviour: without route decision, body lies "
            "unchecked. The lane fix changes this for harness dispatches where "
            "the route decision IS present.\n\n"
            "## Changes\n\n- scripts/lib/foo.py: edited\n\n"
            "## Verification\n\npytest tests/ -x: all green\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )

        receipt = build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )
        assert receipt is not None
        # No route_decision → body is the fallback. "claude" is already canonical.
        assert receipt["provider"] == "claude"
        assert receipt["model"] == "sonnet"
