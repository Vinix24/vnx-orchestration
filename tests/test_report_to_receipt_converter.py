#!/usr/bin/env python3
"""Tests for report_to_receipt_converter — generic report→receipt conversion.

Covers:
  1. YAML-frontmatter report → exactly one receipt emitted
  2. Re-run on same report → idempotent (no second receipt)
  3. Malformed report (no dispatch_id) → skipped with warning, no crash
  4. scan_and_convert() over a directory of mixed reports
  5. Watermark persistence: already-processed reports skipped across calls
  6. Isolation: converter does NOT read the Bash processor's
     processed_receipts.txt (separate dedup stores, no format conflation)
  7. Isolation: converter does NOT read/write the processor's mtime
     watermark (receipt_processor_watermark)
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from report_to_receipt_converter import (
    _WATERMARK_FILENAME,
    _compute_sha256,
    _extract_body_fields,
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

    def test_converter_ignores_bash_watermark(self, reports_dir, state_dir):
        """Pre-populated processed_receipts.txt does NOT block the converter.

        The converter owns its own dedup store (report_to_receipt_processed.txt).
        A hash in the Bash processor's processed_receipts.txt is irrelevant
        to the converter — it must NOT cause the converter to skip a report.
        """
        report = reports_dir / "20260601-ignored-bash.md"
        _write_frontmatter_report(report, "20260601-ignored-bash")
        file_hash = _compute_sha256(report)

        # Pre-populate the Bash watermark (processed_receipts.txt) with the
        # report's hash — simulating that the Bash processor already handled it.
        bash_wm = state_dir / "processed_receipts.txt"
        bash_wm.write_text(file_hash + "\n", encoding="utf-8")

        stats = scan_and_convert([reports_dir], state_dir)

        # Converter must NOT skip: it does not read the Bash watermark.
        assert stats.new_count == 1
        assert _count_receipts(state_dir) == 1

        # Converter's own watermark is now populated.
        py_wm = _load_watermark(state_dir / _WATERMARK_FILENAME)
        assert file_hash in py_wm

        # Second scan: converter's own watermark prevents re-emission.
        stats2 = scan_and_convert([reports_dir], state_dir)
        assert stats2.new_count == 0
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
        self._make_metadata_db(state_dir, [(dispatch_id, "vnx-dev", "backend-developer")])
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
