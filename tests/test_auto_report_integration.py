#!/usr/bin/env python3
"""Integration tests for the auto-report pipeline (PR-5, F37).

Covers:
  - Auto-generated markdown reports pass receipt processor (ReportParser) validation
  - auto_generated metadata field present in rendered output
  - End-to-end fixture: mock extraction → assembly → receipt processing
  - Tag flow integrity: dispatch → extraction → classification → receipt
  - Manual report backward compatibility (no breaking changes)
  - Subprocess trigger path: trigger.json → assemble_from_trigger → valid receipt
  - validate_auto_report() passes on well-formed reports
  - Partial extraction (no tests, no git commit) produces valid output
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"

sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

from auto_report_contract import (
    AutoDerivedTags,
    AutoReport,
    AutoReportMetadata,
    ClassifiedTags,
    Complexity,
    ContentType,
    DispatchTags,
    DispatchType,
    EventMetrics,
    ExtractionResult,
    GitProvenance,
    HaikuClassification,
    OutcomeStatus,
    RiskLevel,
    Scope,
    TestResults,
    UnifiedTagSet,
    render_markdown,
    validate_auto_report,
)
from report_assembler import (
    AssemblyResult,
    assemble,
    assemble_from_trigger,
    write_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DISPATCH_ID = "20260408-110915-receipt-processor-integration-B"
PR_ID = "PR-5"
TERMINAL = "T2"
TRACK = "B"
GATE = "gate_pr5_integration_tests"


def _make_extraction(
    dispatch_id: str = DISPATCH_ID,
    terminal: str = TERMINAL,
    track: str = TRACK,
    gate: str = GATE,
    commit_hash: str = "abc123def456",
    files_changed: tuple = ("scripts/lib/report_assembler.py", "tests/test_auto_report_integration.py"),
    insertions: int = 120,
    deletions: int = 10,
    passed: int = 12,
    failed: int = 0,
    exit_summary: str = "All integration tests passed. Pipeline wired end-to-end.",
) -> ExtractionResult:
    """Build a realistic ExtractionResult for testing."""
    git = GitProvenance(
        commit_hash=commit_hash,
        commit_message="feat(integration): wire receipt processor e2e tests",
        branch="feature/f37-pr5-integration",
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
        is_dirty=False,
    )
    tests = TestResults(
        passed=passed,
        failed=failed,
        errors=0,
        skipped=0,
        duration_seconds=0.42,
        raw_output=f"{passed} passed in 0.42s",
    ) if passed > 0 or failed > 0 else None
    events = EventMetrics(
        tool_use_count=18,
        text_block_count=5,
        thinking_block_count=3,
        error_count=0,
        session_duration_seconds=420,
        model_used="claude-sonnet-4-6",
    )
    return ExtractionResult(
        dispatch_id=dispatch_id,
        terminal=terminal,
        track=track,
        gate=gate,
        git=git,
        tests=tests,
        syntax_checks=(),
        events=events,
        exit_summary=exit_summary,
        extracted_at="2026-04-08T11:30:00+00:00",
    )


def _make_report(extraction: Optional[ExtractionResult] = None, status: str = "success") -> AutoReport:
    """Build a complete AutoReport for testing."""
    if extraction is None:
        extraction = _make_extraction()

    auto_derived = AutoDerivedTags.from_extraction(extraction)
    classification = HaikuClassification.rule_based(extraction)
    classified_tags = ClassifiedTags.from_classification(classification)
    dispatch_tags = DispatchTags(
        dispatch_type=DispatchType.TEST,
        risk=RiskLevel.MEDIUM,
        scope=Scope.MULTI_FILE,
        expected_ois=0,
    )
    tags = UnifiedTagSet(
        dispatch_tags=dispatch_tags,
        auto_derived=auto_derived,
        classified=classified_tags,
        outcome=OutcomeStatus.SUCCESS,
    )
    metadata = AutoReportMetadata(
        dispatch_id=extraction.dispatch_id,
        pr_id=PR_ID,
        terminal=extraction.terminal,
        track=extraction.track,
        gate=extraction.gate,
        status=status,
        auto_generated=True,
        assembled_at="2026-04-08T11:30:00+00:00",
    )
    return AutoReport(
        metadata=metadata,
        extraction=extraction,
        classification=classification,
        tags=tags,
        quality_checks=(),
    )


@pytest.fixture
def vnx_env(tmp_path, monkeypatch):
    """Set up a minimal VNX environment for testing."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    unified_dir = data_dir / "unified_reports"
    pipeline_dir = data_dir / "state" / "report_pipeline"
    dispatch_dir = data_dir / "dispatches" / "completed"
    receipts_file = state_dir / "t0_receipts.ndjson"

    for d in [state_dir, unified_dir, pipeline_dir, dispatch_dir]:
        d.mkdir(parents=True, exist_ok=True)
    receipts_file.touch()

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(unified_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))

    return {
        "data_dir": data_dir,
        "state_dir": state_dir,
        "unified_dir": unified_dir,
        "pipeline_dir": pipeline_dir,
        "receipts_file": receipts_file,
    }


# ---------------------------------------------------------------------------
# 1. Markdown rendering: required fields present
# ---------------------------------------------------------------------------

class TestMarkdownRendering:

    def test_required_receipt_fields_present(self):
        """render_markdown produces all five fields required by receipt_processor_v4.sh."""
        report = _make_report()
        md = render_markdown(report)

        assert f"**Dispatch ID**: {DISPATCH_ID}" in md
        assert f"**PR**: {PR_ID}" in md
        assert f"**Track**: {TRACK}" in md
        assert f"**Gate**: {GATE}" in md
        assert "**Status**: success" in md

    def test_auto_generated_field_present(self):
        """Rendered markdown includes **Auto-Generated**: true for audit trail."""
        report = _make_report()
        md = render_markdown(report)
        assert "**Auto-Generated**: true" in md

    def test_files_modified_section(self):
        """Files Modified section lists changed files from git extraction."""
        report = _make_report()
        md = render_markdown(report)
        assert "report_assembler.py" in md
        assert "test_auto_report_integration.py" in md

    def test_test_results_section(self):
        """Test Results section contains pass/fail counts."""
        report = _make_report()
        md = render_markdown(report)
        assert "**Passed**: 12" in md
        assert "**Failed**: 0" in md

    def test_tags_section_dispatch_tags(self):
        """Tags section includes dispatch-level tags."""
        report = _make_report()
        md = render_markdown(report)
        assert "dispatch_type" in md or "type=test" in md

    def test_open_items_section_always_present(self):
        """Open Items section is always present, even when empty."""
        report = _make_report()
        md = render_markdown(report)
        assert "## Open Items" in md

    def test_empty_extraction_produces_valid_markdown(self):
        """Partial extraction (no tests, no git commit) still produces valid markdown."""
        extraction = ExtractionResult(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            exit_summary="Partial run: no commit staged.",
        )
        report = _make_report(extraction=extraction)
        md = render_markdown(report)
        # All required fields must be present even with empty extraction
        assert f"**Dispatch ID**: {DISPATCH_ID}" in md
        assert "**Auto-Generated**: true" in md
        assert "No file changes detected." in md
        assert "No test results captured." in md


# ---------------------------------------------------------------------------
# 2. validate_auto_report()
# ---------------------------------------------------------------------------

class TestValidateAutoReport:

    def test_valid_report_returns_no_errors(self):
        """validate_auto_report() returns empty list for a well-formed report."""
        report = _make_report()
        errors = validate_auto_report(report)
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_missing_dispatch_id_flagged(self):
        """validate_auto_report() catches missing dispatch_id in metadata."""
        extraction = _make_extraction(dispatch_id=DISPATCH_ID)
        metadata = AutoReportMetadata(
            dispatch_id="",  # intentionally empty
            pr_id=PR_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            status="success",
        )
        report = AutoReport(metadata=metadata, extraction=extraction)
        errors = validate_auto_report(report)
        assert any("dispatch_id" in e for e in errors)

    def test_dispatch_id_mismatch_flagged(self):
        """validate_auto_report() catches extraction/metadata dispatch_id mismatch."""
        extraction = _make_extraction(dispatch_id="dispatch-A")
        metadata = AutoReportMetadata(
            dispatch_id="dispatch-B",
            pr_id=PR_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            status="success",
        )
        report = AutoReport(metadata=metadata, extraction=extraction)
        errors = validate_auto_report(report)
        assert any("dispatch_id" in e for e in errors)

    def test_partial_report_validates_with_no_tests(self):
        """Partial extraction with no test data still validates."""
        extraction = ExtractionResult(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
        )
        metadata = AutoReportMetadata(
            dispatch_id=DISPATCH_ID,
            pr_id=PR_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            status="partial",
        )
        report = AutoReport(metadata=metadata, extraction=extraction)
        errors = validate_auto_report(report)
        assert errors == []


# ---------------------------------------------------------------------------
# 3. ReportParser integration (receipt processor validation)
# ---------------------------------------------------------------------------

class TestReportParserValidation:
    """Verify auto-generated markdown is parseable by the receipt processor."""

    @pytest.fixture(autouse=True)
    def _setup_report_parser(self, vnx_env):
        """Import ReportParser with VNX env set up."""
        from report_parser import ReportParser
        self.parser = ReportParser()

    def test_auto_report_dispatch_id_parsed(self, tmp_path):
        """ReportParser extracts correct dispatch_id from auto-generated markdown.

        parse_report() returns a flat receipt dict; dispatch_id is at the top level.
        """
        report = _make_report()
        md = render_markdown(report)
        md_path = tmp_path / "test_report.md"
        md_path.write_text(md, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        # receipt dict is flat: dispatch_id at top level
        assert result.get("dispatch_id") == DISPATCH_ID

    def test_auto_report_gate_parsed(self, tmp_path):
        """ReportParser extracts correct gate from auto-generated markdown."""
        report = _make_report()
        md = render_markdown(report)
        md_path = tmp_path / "test_report.md"
        md_path.write_text(md, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        assert result.get("gate") == GATE

    def test_auto_report_status_parsed(self, tmp_path):
        """ReportParser extracts correct status from auto-generated markdown."""
        report = _make_report()
        md = render_markdown(report)
        md_path = tmp_path / "test_report.md"
        md_path.write_text(md, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        assert result.get("status") == "success"

    def test_auto_report_terminal_detected(self, tmp_path):
        """ReportParser detects terminal T2 from Track B in auto-generated markdown."""
        report = _make_report()
        md = render_markdown(report)
        md_path = tmp_path / "test_report.md"
        md_path.write_text(md, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        # Track B → T2
        assert result.get("terminal") in ("T2", "B")

    def test_auto_generated_field_accessible(self, tmp_path):
        """auto_generated field is present in rendered markdown content."""
        report = _make_report()
        md = render_markdown(report)
        md_path = tmp_path / "test_report.md"
        md_path.write_text(md, encoding="utf-8")

        content = md_path.read_text()
        assert "Auto-Generated" in content


# ---------------------------------------------------------------------------
# 4. Manual report backward compatibility
# ---------------------------------------------------------------------------

class TestManualReportBackwardCompatibility:
    """Manual reports must still be parseable after PR-5 changes."""

    MANUAL_REPORT = """\
# Worker Report: receipt-processor-integration

**Dispatch ID**: {dispatch_id}
**PR**: PR-5
**Track**: B
**Gate**: gate_pr5_integration_tests
**Status**: success
**Terminal**: T2

## Implementation Summary

Wired receipt processor integration. All manual tests pass.

## Files Modified

- `scripts/receipt_processor_v4.sh` — validation logic
- `tests/test_receipt_processor.py` — regression tests

## Testing Evidence

12 passed in 0.31s

## Commit

`deadbeef1234`

## Open Items

None
"""

    @pytest.fixture(autouse=True)
    def _setup_report_parser(self, vnx_env):
        from report_parser import ReportParser
        self.parser = ReportParser()

    def test_manual_report_dispatch_id_parsed(self, tmp_path):
        """Manual report dispatch_id is still parsed correctly.

        parse_report() returns a flat receipt dict; fields are at the top level.
        """
        content = self.MANUAL_REPORT.format(dispatch_id=DISPATCH_ID)
        md_path = tmp_path / "manual_report.md"
        md_path.write_text(content, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        assert result.get("dispatch_id") == DISPATCH_ID

    def test_manual_report_gate_parsed(self, tmp_path):
        """Manual report gate is still parsed correctly."""
        content = self.MANUAL_REPORT.format(dispatch_id=DISPATCH_ID)
        md_path = tmp_path / "manual_report.md"
        md_path.write_text(content, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        assert result.get("gate") == "gate_pr5_integration_tests"

    def test_manual_report_status_parsed(self, tmp_path):
        """Manual report status is still parsed correctly."""
        content = self.MANUAL_REPORT.format(dispatch_id=DISPATCH_ID)
        md_path = tmp_path / "manual_report.md"
        md_path.write_text(content, encoding="utf-8")

        result = self.parser.parse_report(str(md_path))
        assert result.get("status") == "success"

    def test_manual_report_not_marked_auto_generated(self, tmp_path):
        """Manual reports do NOT have Auto-Generated field."""
        content = self.MANUAL_REPORT.format(dispatch_id=DISPATCH_ID)
        md_path = tmp_path / "manual_report.md"
        md_path.write_text(content, encoding="utf-8")

        assert "Auto-Generated" not in content


# ---------------------------------------------------------------------------
# 5. Tag flow integrity
# ---------------------------------------------------------------------------

class TestTagFlowIntegrity:
    """Tags must survive the full dispatch → extraction → classification → receipt chain."""

    def test_dispatch_tags_present_in_auto_report(self):
        """DispatchTags set at creation are present in assembled AutoReport."""
        report = _make_report()
        assert report.tags.dispatch_tags is not None
        assert report.tags.dispatch_tags.dispatch_type == DispatchType.TEST
        assert report.tags.dispatch_tags.risk == RiskLevel.MEDIUM
        assert report.tags.dispatch_tags.scope == Scope.MULTI_FILE

    def test_auto_derived_tags_populated_from_extraction(self):
        """AutoDerivedTags are populated from ExtractionResult."""
        extraction = _make_extraction(
            insertions=120,
            deletions=10,
            passed=12,
        )
        report = _make_report(extraction=extraction)
        ad = report.tags.auto_derived
        assert ad is not None
        assert ad.line_delta_add == 120
        assert ad.line_delta_del == 10
        assert ad.test_count == 12
        assert ad.has_commit is True

    def test_classified_tags_present(self):
        """ClassifiedTags from rule-based classification are present."""
        report = _make_report()
        assert report.tags.classified is not None
        assert report.tags.classified.content_type in (ct.value for ct in ContentType)
        assert 1 <= report.tags.classified.quality_score <= 5

    def test_outcome_tag_correct(self):
        """Outcome status is correctly set in UnifiedTagSet."""
        report = _make_report()
        assert report.tags.outcome == OutcomeStatus.SUCCESS

    def test_tag_chain_immutable(self):
        """UnifiedTagSet is frozen — cannot be modified after construction."""
        report = _make_report()
        with pytest.raises((AttributeError, TypeError)):
            report.tags.outcome = OutcomeStatus.FAILURE  # type: ignore

    def test_tags_serialization_round_trip(self):
        """Tag set survives JSON serialization → deserialization."""
        report = _make_report()
        serialized = report.tags.to_dict()
        restored = UnifiedTagSet.from_dict(serialized)

        assert restored.dispatch_tags.dispatch_type == report.tags.dispatch_tags.dispatch_type
        assert restored.auto_derived.test_count == report.tags.auto_derived.test_count
        assert restored.classified.quality_score == report.tags.classified.quality_score
        assert restored.outcome == report.tags.outcome

    def test_tags_in_rendered_markdown(self):
        """Classified and dispatch tags appear in rendered markdown."""
        report = _make_report()
        md = render_markdown(report)
        # Dispatch tags section
        assert "type=test" in md or "dispatch_type" in md
        assert "risk=medium" in md or "medium" in md
        # Classified tags section
        assert "quality=" in md or "quality_score" in md or "/5" in md


# ---------------------------------------------------------------------------
# 6. End-to-end fixture: mock dispatch → assembly → receipt
# ---------------------------------------------------------------------------

class TestEndToEndFixture:
    """Full pipeline: mock data → assemble → write → parse."""

    def test_assembly_with_mocked_extraction(self, vnx_env):
        """assemble() with patched run_extraction produces valid receipt-ready report."""
        mock_extraction = _make_extraction()

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
                vnx_data_dir=vnx_env["data_dir"],
            )

        assert result.is_valid, f"Assembly errors: {result.errors}"
        assert result.report.metadata.dispatch_id == DISPATCH_ID
        assert result.report.metadata.auto_generated is True

    def test_write_report_creates_files(self, vnx_env):
        """write_report() creates both JSON sidecar and markdown file."""
        mock_extraction = _make_extraction()

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
                vnx_data_dir=vnx_env["data_dir"],
            )

        json_path, md_path = write_report(result, vnx_env["data_dir"])

        assert json_path is not None and json_path.exists()
        assert md_path is not None and md_path.exists()
        assert md_path.suffix == ".md"
        assert "auto-" in md_path.name

    def test_written_markdown_parseable_by_report_parser(self, vnx_env):
        """Written markdown can be parsed by ReportParser (receipt processor)."""
        from report_parser import ReportParser
        parser = ReportParser()

        mock_extraction = _make_extraction()

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
                vnx_data_dir=vnx_env["data_dir"],
            )

        _, md_path = write_report(result, vnx_env["data_dir"])
        assert md_path is not None and md_path.exists()

        parsed = parser.parse_report(str(md_path))
        # parse_report returns a flat receipt dict
        assert parsed.get("dispatch_id") == DISPATCH_ID
        assert parsed.get("gate") == GATE
        assert parsed.get("status") == "success"

    def test_written_json_sidecar_round_trips(self, vnx_env):
        """JSON sidecar can be deserialized back to AutoReport."""
        mock_extraction = _make_extraction()

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
                vnx_data_dir=vnx_env["data_dir"],
            )

        json_path, _ = write_report(result, vnx_env["data_dir"])
        assert json_path is not None

        restored = AutoReport.from_json(json_path.read_text(encoding="utf-8"))
        assert restored.metadata.dispatch_id == DISPATCH_ID
        assert restored.metadata.auto_generated is True
        assert restored.tags.dispatch_tags is None  # no dispatch file in test env


# ---------------------------------------------------------------------------
# 7. Subprocess trigger path
# ---------------------------------------------------------------------------

class TestSubprocessTriggerPath:
    """Test the full trigger file → assemble_from_trigger → valid report path."""

    def test_trigger_file_produces_valid_report(self, vnx_env, tmp_path):
        """assemble_from_trigger() with a valid trigger JSON produces a valid report."""
        trigger = {
            "dispatch_id": DISPATCH_ID,
            "terminal": TERMINAL,
            "track": TRACK,
            "gate": GATE,
            "pr_id": PR_ID,
            "project_root": str(VNX_ROOT),
            "session_id": "test-session-abc123",
        }
        trigger_path = tmp_path / f"{DISPATCH_ID}.trigger.json"
        trigger_path.write_text(json.dumps(trigger), encoding="utf-8")

        mock_extraction = _make_extraction()

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble_from_trigger(trigger_path)

        assert result.report.metadata.dispatch_id == DISPATCH_ID
        assert result.report.metadata.auto_generated is True
        assert result.is_valid, f"Trigger assembly errors: {result.errors}"

    def test_trigger_file_missing_produces_failed_result(self, tmp_path):
        """assemble_from_trigger() with missing file returns a failed result, not an exception."""
        missing_path = tmp_path / "missing.trigger.json"
        result = assemble_from_trigger(missing_path)
        # Should return a failed result rather than raise
        assert result is not None
        assert len(result.errors) > 0

    def test_trigger_file_write_then_parse_receipt(self, vnx_env, tmp_path):
        """Full subprocess path: trigger → assemble → write → receipt processor parses output."""
        from report_parser import ReportParser
        parser = ReportParser()

        trigger = {
            "dispatch_id": DISPATCH_ID,
            "terminal": TERMINAL,
            "track": TRACK,
            "gate": GATE,
            "pr_id": PR_ID,
            "project_root": str(VNX_ROOT),
            "session_id": "test-session-xyz789",
        }
        trigger_path = tmp_path / f"{DISPATCH_ID}.trigger.json"
        trigger_path.write_text(json.dumps(trigger), encoding="utf-8")

        mock_extraction = _make_extraction()

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble_from_trigger(trigger_path)

        _, md_path = write_report(result, vnx_env["data_dir"])
        assert md_path is not None and md_path.exists()

        parsed = parser.parse_report(str(md_path))
        assert parsed.get("dispatch_id") == DISPATCH_ID

    def test_trigger_with_partial_fields_does_not_crash(self, vnx_env, tmp_path):
        """Trigger with minimal fields (no gate, no pr_id) produces partial report without crash."""
        trigger = {
            "dispatch_id": DISPATCH_ID,
            "terminal": TERMINAL,
            # gate and pr_id intentionally omitted
        }
        trigger_path = tmp_path / "partial.trigger.json"
        trigger_path.write_text(json.dumps(trigger), encoding="utf-8")

        mock_extraction = ExtractionResult(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track="A",  # default fallback
            gate="",
        )

        with patch("report_assembler.run_extraction", return_value=mock_extraction):
            result = assemble_from_trigger(trigger_path)

        assert result is not None
        assert result.report.metadata.dispatch_id == DISPATCH_ID


# ---------------------------------------------------------------------------
# 8. AutoReport schema: JSON round-trip and auto_generated field
# ---------------------------------------------------------------------------

class TestAutoReportSchema:

    def test_auto_generated_true_by_default(self):
        """AutoReportMetadata.auto_generated defaults to True."""
        metadata = AutoReportMetadata(
            dispatch_id=DISPATCH_ID,
            pr_id=PR_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            status="success",
        )
        assert metadata.auto_generated is True

    def test_auto_generated_in_json_output(self):
        """auto_generated field is present in AutoReport.to_json() output."""
        report = _make_report()
        data = json.loads(report.to_json())
        assert data["metadata"]["auto_generated"] is True

    def test_full_report_json_round_trip(self):
        """AutoReport serializes and deserializes correctly."""
        report = _make_report()
        data = json.loads(report.to_json())
        restored = AutoReport.from_dict(data)

        assert restored.metadata.dispatch_id == report.metadata.dispatch_id
        assert restored.metadata.auto_generated == report.metadata.auto_generated
        assert restored.extraction.terminal == report.extraction.terminal
        assert restored.extraction.git.commit_hash == report.extraction.git.commit_hash
        assert restored.extraction.tests.passed == report.extraction.tests.passed  # type: ignore

    def test_schema_version_present(self):
        """Schema version is present in JSON output."""
        report = _make_report()
        data = json.loads(report.to_json())
        assert "schema_version" in data
        assert data["schema_version"] == "1.0.0"
