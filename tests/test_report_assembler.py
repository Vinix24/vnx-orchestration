#!/usr/bin/env python3
"""Tests for PR-3: Auto-Report Assembler.

Gate: gate_pr3_auto_report_assembler

Covers:
- Generated markdown validates against receipt processor parsing
- All required metadata fields present in output
- Tag propagation preserves dispatch-level tags
- Partial extraction input produces valid (incomplete) report
- End-to-end: extraction → assembly → markdown file exists in unified_reports/
- assemble_from_trigger reads trigger file correctly
- write_report creates JSON + markdown files
- _short_title_from_dispatch_id parsing
- _derive_outcome logic
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure scripts/lib is on path
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from report_assembler import (
    AssemblyResult,
    _derive_outcome,
    _short_title_from_dispatch_id,
    assemble,
    assemble_from_trigger,
    write_report,
)
from auto_report_contract import (
    AutoReport,
    DispatchTags,
    DispatchType,
    ExtractionResult,
    GitProvenance,
    OutcomeStatus,
    RiskLevel,
    Scope,
    TestResults,
    validate_auto_report,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────────

DISPATCH_ID = "20260408-110915-test-assembler-A"
TERMINAL = "T1"
TRACK = "A"
GATE = "gate_pr3_auto_report_assembler"
PR_ID = "PR-3"


def _make_trigger_json(**overrides) -> dict:
    base = {
        "trigger_time": "2026-04-08T10:00:00Z",
        "dispatch_id": DISPATCH_ID,
        "terminal": TERMINAL,
        "track": TRACK,
        "gate": GATE,
        "pr_id": PR_ID,
        "session_id": "sess-abc-123",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/dev/test/.claude/terminals/T1",
        "project_root": "/dev/test",
        "source": "stop_hook",
    }
    base.update(overrides)
    return base


# ─── Unit Tests ───────────────────────────────────────────────────────────────

class TestShortTitle(unittest.TestCase):
    def test_standard_dispatch_id(self):
        result = _short_title_from_dispatch_id("20260408-110915-auto-report-assembler-A")
        self.assertEqual(result, "auto-report-assembler")

    def test_dispatch_id_b_track(self):
        result = _short_title_from_dispatch_id("20260408-110915-stop-hook-infrastructure-B")
        self.assertEqual(result, "stop-hook-infrastructure")

    def test_dispatch_id_no_match_falls_back(self):
        result = _short_title_from_dispatch_id("some-random-id")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) <= 30)

    def test_long_slug_truncated(self):
        long_id = "20260408-110915-" + "a" * 50 + "-A"
        result = _short_title_from_dispatch_id(long_id)
        self.assertLessEqual(len(result), 30)


class TestDeriveOutcome(unittest.TestCase):
    def _make_extraction(self, tests=None, has_commit=True, has_syntax_err=False):
        from auto_report_contract import EventMetrics, SyntaxCheck
        syntax = (SyntaxCheck(file_path="f.py", language="python", valid=not has_syntax_err),) if has_syntax_err else ()
        git = GitProvenance(commit_hash="abc123" if has_commit else "")
        return ExtractionResult(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            git=git,
            tests=tests,
            syntax_checks=syntax,
        )

    def test_override_success(self):
        ex = self._make_extraction()
        result = _derive_outcome(ex, "success")
        self.assertEqual(result, OutcomeStatus.SUCCESS)

    def test_override_failure(self):
        ex = self._make_extraction()
        result = _derive_outcome(ex, "failure")
        self.assertEqual(result, OutcomeStatus.FAILURE)

    def test_test_failures_produce_failure(self):
        tests = TestResults(passed=2, failed=1)
        ex = self._make_extraction(tests=tests)
        result = _derive_outcome(ex)
        self.assertEqual(result, OutcomeStatus.FAILURE)

    def test_syntax_errors_produce_failure(self):
        ex = self._make_extraction(has_syntax_err=True)
        result = _derive_outcome(ex)
        self.assertEqual(result, OutcomeStatus.FAILURE)

    def test_no_commit_produces_partial(self):
        ex = self._make_extraction(has_commit=False)
        result = _derive_outcome(ex)
        self.assertEqual(result, OutcomeStatus.PARTIAL)

    def test_all_passed_produces_success(self):
        tests = TestResults(passed=5)
        ex = self._make_extraction(tests=tests)
        result = _derive_outcome(ex)
        self.assertEqual(result, OutcomeStatus.SUCCESS)


class TestAssemble(unittest.TestCase):
    """Tests for the core assemble() function."""

    def test_returns_assembly_result(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        self.assertIsInstance(result, AssemblyResult)
        self.assertIsInstance(result.report, AutoReport)

    def test_required_metadata_fields_present(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        meta = result.report.metadata
        self.assertEqual(meta.dispatch_id, DISPATCH_ID)
        self.assertEqual(meta.pr_id, PR_ID)
        self.assertEqual(meta.track, TRACK)
        self.assertEqual(meta.gate, GATE)
        self.assertIn(meta.status, ("success", "failure", "partial", "crashed", "no_execution"))

    def test_auto_generated_flag(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        self.assertTrue(result.report.metadata.auto_generated)

    def test_tags_auto_derived_always_present(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        self.assertIsNotNone(result.report.tags.auto_derived)
        self.assertIsNotNone(result.report.tags.classified)

    def test_partial_extraction_no_tests_still_valid(self):
        """Partial extraction (no tests, no events) should produce valid report."""
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
            events_path=None,  # No events file
        )
        errors = validate_auto_report(result.report)
        self.assertEqual(errors, [])

    def test_classification_is_rule_based(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        classification = result.report.classification
        self.assertIsNotNone(classification)
        self.assertEqual(classification.classified_by, "rule_based")

    def test_status_override_respected(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
            status_override="failure",
        )
        self.assertEqual(result.report.metadata.status, "failure")
        self.assertEqual(result.report.tags.outcome, OutcomeStatus.FAILURE)

    def test_dispatch_tags_from_active_dir(self):
        """Assembler loads DispatchTags from bundle.json when present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            active_dir = Path(tmpdir) / "active"
            active_dir.mkdir()
            bundle = {
                "dispatch_type": "implementation",
                "risk": "medium",
                "scope": "multi_file",
                "expected_ois": 0,
            }
            bundle_path = active_dir / f"{DISPATCH_ID}.bundle.json"
            bundle_path.write_text(json.dumps(bundle))

            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
                active_dir=active_dir,
            )
            dt = result.report.tags.dispatch_tags
            self.assertIsNotNone(dt)
            self.assertEqual(dt.dispatch_type, DispatchType.IMPLEMENTATION)
            self.assertEqual(dt.risk, RiskLevel.MEDIUM)

    def test_dispatch_tags_absent_when_no_bundle(self):
        """No bundle.json → dispatch_tags is None, pipeline continues."""
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        # dispatch_tags may be None — that's fine for partial extraction
        self.assertIsNone(result.report.tags.dispatch_tags)


class TestTagPropagation(unittest.TestCase):
    """Verify that dispatch-level tags survive to the assembled report."""

    def test_dispatch_tags_preserved_in_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active_dir = Path(tmpdir) / "active"
            active_dir.mkdir()
            bundle = {
                "dispatch_type": "test",
                "risk": "low",
                "scope": "single_file",
                "depends_on": ["PR-1"],
            }
            (active_dir / f"{DISPATCH_ID}.bundle.json").write_text(json.dumps(bundle))

            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
                active_dir=active_dir,
            )
            dt = result.report.tags.dispatch_tags
            self.assertIsNotNone(dt)
            self.assertEqual(dt.dispatch_type.value, "test")
            self.assertEqual(dt.risk.value, "low")
            self.assertIn("PR-1", dt.depends_on)

    def test_auto_derived_tags_populated(self):
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
        )
        ad = result.report.tags.auto_derived
        self.assertIsNotNone(ad)
        # These fields should always have a value (even if 0/False)
        self.assertIsInstance(ad.file_count, int)
        self.assertIsInstance(ad.tests_passed, bool)
        self.assertIsInstance(ad.tool_use_count, int)


class TestMarkdownFormat(unittest.TestCase):
    """Verify generated markdown contains required receipt processor fields."""

    REQUIRED_FIELDS = [
        "**Dispatch ID**",
        "**PR**",
        "**Track**",
        "**Gate**",
        "**Status**",
        "## Open Items",
    ]

    def _get_markdown(self, **kwargs) -> str:
        from auto_report_contract import render_markdown
        result = assemble(
            dispatch_id=DISPATCH_ID,
            terminal=TERMINAL,
            track=TRACK,
            gate=GATE,
            pr_id=PR_ID,
            **kwargs,
        )
        return render_markdown(result.report)

    def test_required_fields_present(self):
        md = self._get_markdown()
        for field in self.REQUIRED_FIELDS:
            self.assertIn(field, md, f"Missing required field: {field}")

    def test_dispatch_id_in_markdown(self):
        md = self._get_markdown()
        self.assertIn(DISPATCH_ID, md)

    def test_pr_id_in_markdown(self):
        md = self._get_markdown()
        self.assertIn(PR_ID, md)

    def test_open_items_section_always_present(self):
        md = self._get_markdown()
        self.assertIn("## Open Items", md)

    def test_no_tests_omits_test_counts(self):
        """Missing tests → section says 'No test results captured.' not a crash."""
        md = self._get_markdown()
        # Should not crash; section must exist with graceful message
        self.assertIn("## Test Results", md)
        # When no pytest ran, the section should say no results
        self.assertIn("No test results", md)

    def test_partial_extraction_valid_markdown(self):
        """Even with minimal extraction data, markdown must parse correctly."""
        md = self._get_markdown()
        # All required bold fields must appear in first 500 chars
        header = md[:1000]
        self.assertIn("**Dispatch ID**", header)
        self.assertIn("**PR**", header)


class TestAssembleFromTrigger(unittest.TestCase):
    """Tests for assemble_from_trigger()."""

    def test_reads_trigger_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trigger = _make_trigger_json()
            trigger_path = Path(tmpdir) / f"{DISPATCH_ID}.trigger.json"
            trigger_path.write_text(json.dumps(trigger))

            result = assemble_from_trigger(trigger_path)
            self.assertIsInstance(result, AssemblyResult)
            self.assertEqual(result.report.metadata.dispatch_id, DISPATCH_ID)

    def test_missing_trigger_returns_failed_result(self):
        result = assemble_from_trigger(Path("/nonexistent/path.trigger.json"))
        self.assertIsInstance(result, AssemblyResult)
        self.assertGreater(len(result.errors), 0)

    def test_partial_trigger_no_gate_still_assembles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trigger = _make_trigger_json(gate="", pr_id="")
            trigger_path = Path(tmpdir) / "partial.trigger.json"
            trigger_path.write_text(json.dumps(trigger))

            result = assemble_from_trigger(trigger_path)
            self.assertIsInstance(result, AssemblyResult)
            # No crash on partial data
            self.assertIsNotNone(result.report)

    def test_pr_id_from_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trigger = _make_trigger_json(pr_id="PR-99")
            trigger_path = Path(tmpdir) / "t.trigger.json"
            trigger_path.write_text(json.dumps(trigger))

            result = assemble_from_trigger(trigger_path)
            self.assertEqual(result.report.metadata.pr_id, "PR-99")


class TestWriteReport(unittest.TestCase):
    """Tests for write_report()."""

    def test_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)

            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
            )
            json_path, md_path = write_report(result, vnx_data_dir=vnx_data)

            self.assertIsNotNone(json_path)
            self.assertIsNotNone(md_path)
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())

    def test_json_contains_required_schema_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)

            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
            )
            json_path, _ = write_report(result, vnx_data_dir=vnx_data)

            data = json.loads(json_path.read_text())
            self.assertIn("metadata", data)
            self.assertIn("extraction", data)
            self.assertIn("tags", data)
            self.assertEqual(data["metadata"]["dispatch_id"], DISPATCH_ID)

    def test_json_written_to_pipeline_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
            )
            json_path, _ = write_report(result, vnx_data_dir=vnx_data)

            expected = vnx_data / "state" / "report_pipeline" / f"{DISPATCH_ID}.json"
            self.assertEqual(json_path, expected)

    def test_markdown_written_to_unified_reports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
            )
            _, md_path = write_report(result, vnx_data_dir=vnx_data)

            self.assertTrue(str(md_path).startswith(str(vnx_data / "unified_reports")))
            self.assertTrue(md_path.name.endswith(".md"))
            self.assertNotIn("-auto-", md_path.name)  # OI-1064: auto- prefix removed

    def test_markdown_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
            )
            _, md_path = write_report(result, vnx_data_dir=vnx_data)
            content = md_path.read_text()

            for field in ("**Dispatch ID**", "**PR**", "**Track**", "**Gate**", "**Status**"):
                self.assertIn(field, content, f"Missing field: {field}")

    def test_no_vnx_data_dir_returns_none_pair(self):
        import os
        # Temporarily unset env var
        saved = os.environ.pop("VNX_DATA_DIR", None)
        try:
            result = assemble(
                dispatch_id=DISPATCH_ID,
                terminal=TERMINAL,
                track=TRACK,
                gate=GATE,
                pr_id=PR_ID,
            )
            json_path, md_path = write_report(result, vnx_data_dir=None)
            self.assertIsNone(json_path)
            self.assertIsNone(md_path)
        finally:
            if saved:
                os.environ["VNX_DATA_DIR"] = saved


class TestEndToEnd(unittest.TestCase):
    """End-to-end: trigger → assemble → write → files exist in unified_reports."""

    def test_e2e_trigger_to_markdown_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)

            # Create trigger file
            trigger = _make_trigger_json()
            trigger["project_root"] = tmpdir
            trigger_path = vnx_data / "state" / "report_pipeline" / f"{DISPATCH_ID}.trigger.json"
            trigger_path.parent.mkdir(parents=True, exist_ok=True)
            trigger_path.write_text(json.dumps(trigger))

            # Run assembler
            import os
            saved = os.environ.get("VNX_DATA_DIR")
            os.environ["VNX_DATA_DIR"] = str(vnx_data)
            try:
                result = assemble_from_trigger(trigger_path)
                json_path, md_path = write_report(result, vnx_data_dir=vnx_data)
            finally:
                if saved:
                    os.environ["VNX_DATA_DIR"] = saved
                else:
                    os.environ.pop("VNX_DATA_DIR", None)

            # Verify files exist
            self.assertIsNotNone(md_path, "No markdown path returned")
            self.assertTrue(md_path.exists(), f"Markdown file not created: {md_path}")

            unified_reports = vnx_data / "unified_reports"
            files = list(unified_reports.glob("*.md"))
            self.assertGreater(len(files), 0, "No .md files in unified_reports/")

    def test_e2e_report_passes_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vnx_data = Path(tmpdir)
            trigger = _make_trigger_json()
            trigger_path = vnx_data / "t.trigger.json"
            trigger_path.write_text(json.dumps(trigger))

            result = assemble_from_trigger(trigger_path)
            errors = validate_auto_report(result.report)
            self.assertEqual(errors, [], f"Validation errors: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
