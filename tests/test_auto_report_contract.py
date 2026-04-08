"""Tests for auto_report_contract.py — PR-0 schema validation.

Covers: construction, serialization, validation, round-trip, edge cases,
and receipt processor compatibility.
"""

import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from auto_report_contract import (
    SCHEMA_VERSION,
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
    StopHookInput,
    StopHookOutput,
    SyntaxCheck,
    TestResults,
    UnifiedTagSet,
    render_markdown,
    validate_auto_report,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def dispatch_tags():
    return DispatchTags(
        dispatch_type=DispatchType.IMPLEMENTATION,
        risk=RiskLevel.MEDIUM,
        scope=Scope.MULTI_FILE,
        expected_ois=2,
        depends_on=("20260408-dep1-A",),
    )


@pytest.fixture
def git_provenance():
    return GitProvenance(
        commit_hash="abc123def456",
        commit_message="feat(core): add new feature",
        branch="feature/f37-auto-report",
        files_changed=("scripts/lib/auto_report_contract.py", "tests/test_auto_report_contract.py"),
        insertions=150,
        deletions=20,
        is_dirty=False,
    )


@pytest.fixture
def test_results():
    return TestResults(passed=10, failed=0, errors=0, skipped=1, duration_seconds=2.5)


@pytest.fixture
def event_metrics():
    return EventMetrics(
        tool_use_count=15,
        text_block_count=8,
        thinking_block_count=5,
        error_count=0,
        session_duration_seconds=120,
        model_used="claude-sonnet-4-6",
    )


@pytest.fixture
def extraction_result(git_provenance, test_results, event_metrics):
    return ExtractionResult(
        dispatch_id="20260408-110915-auto-report-C",
        terminal="T3",
        track="C",
        gate="gate_pr0_auto_report_contract",
        git=git_provenance,
        tests=test_results,
        syntax_checks=(
            SyntaxCheck("scripts/lib/auto_report_contract.py", "python", True),
        ),
        events=event_metrics,
        exit_summary="Contract module implemented with all schemas.",
        extracted_at="2026-04-08T11:30:00Z",
    )


@pytest.fixture
def haiku_classification():
    return HaikuClassification(
        content_type=ContentType.IMPLEMENTATION,
        quality_score=4,
        complexity=Complexity.MEDIUM,
        consistency_score=0.95,
        summary="Schema definition for auto-report pipeline contract.",
        classified_by="haiku",
    )


@pytest.fixture
def full_auto_report(extraction_result, haiku_classification, dispatch_tags):
    auto_derived = AutoDerivedTags.from_extraction(extraction_result)
    classified = ClassifiedTags.from_classification(haiku_classification)
    tags = UnifiedTagSet(
        dispatch_tags=dispatch_tags,
        auto_derived=auto_derived,
        classified=classified,
        outcome=OutcomeStatus.SUCCESS,
    )
    return AutoReport(
        metadata=AutoReportMetadata(
            dispatch_id="20260408-110915-auto-report-C",
            pr_id="PR-0",
            terminal="T3",
            track="C",
            gate="gate_pr0_auto_report_contract",
            status="success",
            assembled_at="2026-04-08T11:30:00Z",
        ),
        extraction=extraction_result,
        classification=haiku_classification,
        tags=tags,
    )


# ─── Stop Hook Contract ──────────────────────────────────────────────────────

class TestStopHookInput:
    def test_from_stdin_minimal(self):
        raw = json.dumps({
            "session_id": "abc123",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/Users/test/.claude/terminals/T1",
        })
        hook = StopHookInput.from_stdin(raw)
        assert hook.session_id == "abc123"
        assert hook.hook_event_name == "Stop"
        assert hook.permission_mode == "default"

    def test_from_stdin_full(self):
        raw = json.dumps({
            "session_id": "abc123",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/Users/test/.claude/terminals/T2",
            "hook_event_name": "Stop",
            "permission_mode": "bypassPermissions",
        })
        hook = StopHookInput.from_stdin(raw)
        assert hook.permission_mode == "bypassPermissions"

    def test_detect_terminal_t1(self):
        hook = StopHookInput("s1", "/tmp/t.jsonl", "/Users/x/.claude/terminals/T1")
        assert hook.detect_terminal() == "T1"

    def test_detect_terminal_t3(self):
        hook = StopHookInput("s1", "/tmp/t.jsonl", "/Users/x/.claude/terminals/T3")
        assert hook.detect_terminal() == "T3"

    def test_detect_terminal_t0_returns_none(self):
        hook = StopHookInput("s1", "/tmp/t.jsonl", "/Users/x/.claude/terminals/T0")
        assert hook.detect_terminal() is None

    def test_detect_terminal_non_vnx_returns_none(self):
        hook = StopHookInput("s1", "/tmp/t.jsonl", "/Users/x/projects/myapp")
        assert hook.detect_terminal() is None

    def test_from_stdin_missing_field_raises(self):
        raw = json.dumps({"session_id": "abc"})
        with pytest.raises(KeyError):
            StopHookInput.from_stdin(raw)


class TestStopHookOutput:
    def test_to_json_minimal(self):
        out = StopHookOutput()
        parsed = json.loads(out.to_json())
        assert parsed["skipped"] is False

    def test_to_json_with_report(self):
        out = StopHookOutput(
            auto_report_path="/tmp/report.json",
            dispatch_id="20260408-test-C",
            terminal="T3",
        )
        parsed = json.loads(out.to_json())
        assert parsed["dispatch_id"] == "20260408-test-C"

    def test_to_json_skipped(self):
        out = StopHookOutput(skipped=True, skip_reason="No active dispatch")
        parsed = json.loads(out.to_json())
        assert parsed["skipped"] is True
        assert parsed["skip_reason"] == "No active dispatch"


# ─── Tag Taxonomy ─────────────────────────────────────────────────────────────

class TestDispatchTags:
    def test_construction(self, dispatch_tags):
        assert dispatch_tags.dispatch_type == DispatchType.IMPLEMENTATION
        assert dispatch_tags.risk == RiskLevel.MEDIUM
        assert dispatch_tags.expected_ois == 2
        assert len(dispatch_tags.depends_on) == 1

    def test_negative_expected_ois_raises(self):
        with pytest.raises(ValueError, match="expected_ois"):
            DispatchTags(
                dispatch_type=DispatchType.TEST,
                risk=RiskLevel.LOW,
                scope=Scope.SINGLE_FILE,
                expected_ois=-1,
            )

    def test_round_trip(self, dispatch_tags):
        d = dispatch_tags.to_dict()
        restored = DispatchTags.from_dict(d)
        assert restored == dispatch_tags

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            DispatchType("nonexistent")


class TestAutoDerivedTags:
    def test_from_extraction(self, extraction_result):
        tags = AutoDerivedTags.from_extraction(extraction_result)
        assert tags.file_count == 2
        assert tags.test_count == 11  # 10 passed + 1 skipped
        assert tags.line_delta_add == 150
        assert tags.line_delta_del == 20
        assert tags.has_commit is True
        assert tags.syntax_valid is True
        assert tags.tests_passed is True
        assert tags.model_used == "claude-sonnet-4-6"

    def test_from_extraction_no_tests(self):
        ext = ExtractionResult(
            dispatch_id="test-1",
            terminal="T1",
            track="A",
            gate="test",
            extracted_at="2026-04-08T00:00:00Z",
        )
        tags = AutoDerivedTags.from_extraction(ext)
        assert tags.test_count == 0
        assert tags.tests_passed is True  # No tests = not failed


class TestClassifiedTags:
    def test_from_classification(self, haiku_classification):
        tags = ClassifiedTags.from_classification(haiku_classification)
        assert tags.content_type == "implementation"
        assert tags.quality_score == 4
        assert tags.classified_by == "haiku"


class TestUnifiedTagSet:
    def test_round_trip(self, dispatch_tags, extraction_result, haiku_classification):
        auto = AutoDerivedTags.from_extraction(extraction_result)
        classified = ClassifiedTags.from_classification(haiku_classification)
        tags = UnifiedTagSet(
            dispatch_tags=dispatch_tags,
            auto_derived=auto,
            classified=classified,
            outcome=OutcomeStatus.SUCCESS,
        )
        d = tags.to_dict()
        restored = UnifiedTagSet.from_dict(d)
        assert restored.outcome == OutcomeStatus.SUCCESS
        assert restored.dispatch_tags.risk == RiskLevel.MEDIUM
        assert restored.classified.quality_score == 4

    def test_partial_tags(self):
        tags = UnifiedTagSet(outcome=OutcomeStatus.CRASHED)
        d = tags.to_dict()
        assert d["dispatch_tags"] is None
        assert d["auto_derived"] is None
        assert d["classified"] is None
        assert d["outcome"] == "crashed"


# ─── Extraction ───────────────────────────────────────────────────────────────

class TestExtractionResult:
    def test_invalid_terminal_raises(self):
        with pytest.raises(ValueError, match="terminal"):
            ExtractionResult(
                dispatch_id="test-1", terminal="T0", track="A", gate="test"
            )

    def test_invalid_track_raises(self):
        with pytest.raises(ValueError, match="track"):
            ExtractionResult(
                dispatch_id="test-1", terminal="T1", track="D", gate="test"
            )

    def test_has_test_failures(self):
        ext = ExtractionResult(
            dispatch_id="test-1",
            terminal="T1",
            track="A",
            gate="test",
            tests=TestResults(passed=5, failed=2),
        )
        assert ext.has_test_failures is True

    def test_no_tests_no_failures(self):
        ext = ExtractionResult(
            dispatch_id="test-1", terminal="T1", track="A", gate="test"
        )
        assert ext.has_test_failures is False

    def test_syntax_check_detection(self):
        ext = ExtractionResult(
            dispatch_id="test-1",
            terminal="T1",
            track="A",
            gate="test",
            syntax_checks=(
                SyntaxCheck("a.py", "python", True),
                SyntaxCheck("b.sh", "shell", False, "syntax error near line 10"),
            ),
        )
        assert ext.has_syntax_errors is True
        assert ext.all_syntax_valid is False

    def test_round_trip(self, extraction_result):
        d = extraction_result.to_dict()
        restored = ExtractionResult.from_dict(d)
        assert restored.dispatch_id == extraction_result.dispatch_id
        assert restored.git.commit_hash == extraction_result.git.commit_hash
        assert restored.tests.passed == extraction_result.tests.passed
        assert len(restored.syntax_checks) == len(extraction_result.syntax_checks)


class TestGitProvenance:
    def test_line_delta(self, git_provenance):
        assert git_provenance.line_delta == 130  # 150 - 20

    def test_round_trip(self, git_provenance):
        d = git_provenance.to_dict()
        assert isinstance(d["files_changed"], list)
        restored = GitProvenance.from_dict(d)
        assert restored == git_provenance


class TestTestResults:
    def test_total(self, test_results):
        assert test_results.total == 11

    def test_all_passed(self, test_results):
        assert test_results.all_passed is True

    def test_failures_not_passed(self):
        t = TestResults(passed=5, failed=1)
        assert t.all_passed is False

    def test_empty_not_passed(self):
        t = TestResults()
        assert t.all_passed is False  # total == 0


# ─── Haiku Classification ────────────────────────────────────────────────────

class TestHaikuClassification:
    def test_valid_construction(self, haiku_classification):
        assert haiku_classification.quality_score == 4
        assert haiku_classification.classified_by == "haiku"

    def test_invalid_quality_score_raises(self):
        with pytest.raises(ValueError, match="quality_score"):
            HaikuClassification(
                content_type=ContentType.TEST,
                quality_score=6,
                complexity=Complexity.LOW,
                consistency_score=0.5,
                summary="test",
                classified_by="haiku",
            )

    def test_invalid_consistency_score_raises(self):
        with pytest.raises(ValueError, match="consistency_score"):
            HaikuClassification(
                content_type=ContentType.TEST,
                quality_score=3,
                complexity=Complexity.LOW,
                consistency_score=1.5,
                summary="test",
                classified_by="haiku",
            )

    def test_round_trip(self, haiku_classification):
        d = haiku_classification.to_dict()
        restored = HaikuClassification.from_dict(d)
        assert restored == haiku_classification

    def test_rule_based_fallback_implementation(self, extraction_result):
        c = HaikuClassification.rule_based(extraction_result)
        assert c.classified_by == "rule_based"
        assert c.content_type == ContentType.IMPLEMENTATION
        assert 1 <= c.quality_score <= 5

    def test_rule_based_fallback_tests_only(self):
        ext = ExtractionResult(
            dispatch_id="test-1",
            terminal="T2",
            track="B",
            gate="test",
            git=GitProvenance(
                files_changed=("tests/test_foo.py", "tests/test_bar.py"),
                insertions=50,
                deletions=10,
                commit_hash="abc123",
            ),
            tests=TestResults(passed=5),
        )
        c = HaikuClassification.rule_based(ext)
        assert c.content_type == ContentType.TEST

    def test_rule_based_fallback_docs_only(self):
        ext = ExtractionResult(
            dispatch_id="test-1",
            terminal="T1",
            track="A",
            gate="test",
            git=GitProvenance(
                files_changed=("README.md", "docs/guide.md"),
                insertions=30,
                deletions=5,
                commit_hash="abc123",
            ),
        )
        c = HaikuClassification.rule_based(ext)
        assert c.content_type == ContentType.DOCS

    def test_rule_based_high_complexity(self):
        ext = ExtractionResult(
            dispatch_id="test-1",
            terminal="T1",
            track="A",
            gate="test",
            git=GitProvenance(
                files_changed=tuple(f"file{i}.py" for i in range(10)),
                insertions=500,
                deletions=100,
            ),
        )
        c = HaikuClassification.rule_based(ext)
        assert c.complexity == Complexity.HIGH


# ─── Auto Report ──────────────────────────────────────────────────────────────

class TestAutoReport:
    def test_round_trip_json(self, full_auto_report):
        json_str = full_auto_report.to_json()
        restored = AutoReport.from_json(json_str)
        assert restored.metadata.dispatch_id == full_auto_report.metadata.dispatch_id
        assert restored.extraction.git.commit_hash == full_auto_report.extraction.git.commit_hash
        assert restored.classification.quality_score == full_auto_report.classification.quality_score
        assert restored.tags.outcome == OutcomeStatus.SUCCESS

    def test_without_classification(self, extraction_result):
        report = AutoReport(
            metadata=AutoReportMetadata(
                dispatch_id="test-1",
                pr_id="PR-0",
                terminal="T1",
                track="A",
                gate="test",
                status="success",
            ),
            extraction=extraction_result,
        )
        d = report.to_dict()
        assert d["classification"] is None
        restored = AutoReport.from_dict(d)
        assert restored.classification is None

    def test_schema_version_in_output(self, full_auto_report):
        d = full_auto_report.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION


# ─── Validation ───────────────────────────────────────────────────────────────

class TestValidation:
    def test_valid_report(self, full_auto_report):
        errors = validate_auto_report(full_auto_report)
        assert errors == []

    def test_missing_dispatch_id(self, extraction_result):
        report = AutoReport(
            metadata=AutoReportMetadata(
                dispatch_id="",
                pr_id="PR-0",
                terminal="T1",
                track="A",
                gate="test",
                status="success",
            ),
            extraction=extraction_result,
        )
        errors = validate_auto_report(report)
        assert any("dispatch_id" in e for e in errors)

    def test_mismatched_dispatch_ids(self):
        report = AutoReport(
            metadata=AutoReportMetadata(
                dispatch_id="id-1",
                pr_id="PR-0",
                terminal="T1",
                track="A",
                gate="test",
                status="success",
            ),
            extraction=ExtractionResult(
                dispatch_id="id-2",
                terminal="T1",
                track="A",
                gate="test",
            ),
        )
        errors = validate_auto_report(report)
        assert any("does not match" in e for e in errors)


# ─── Markdown Rendering ──────────────────────────────────────────────────────

class TestRenderMarkdown:
    def test_contains_required_metadata(self, full_auto_report):
        md = render_markdown(full_auto_report)
        assert "**Dispatch ID**: 20260408-110915-auto-report-C" in md
        assert "**PR**: PR-0" in md
        assert "**Track**: C" in md
        assert "**Gate**: gate_pr0_auto_report_contract" in md
        assert "**Status**: success" in md
        assert "**Auto-Generated**: true" in md

    def test_contains_files_section(self, full_auto_report):
        md = render_markdown(full_auto_report)
        assert "auto_report_contract.py" in md
        assert "+150/-20" in md

    def test_contains_test_results(self, full_auto_report):
        md = render_markdown(full_auto_report)
        assert "Passed**: 10" in md
        assert "Failed**: 0" in md

    def test_contains_tags(self, full_auto_report):
        md = render_markdown(full_auto_report)
        assert "risk=medium" in md
        assert "quality=4/5" in md

    def test_open_items_always_present(self, full_auto_report):
        md = render_markdown(full_auto_report)
        assert "## Open Items" in md

    def test_no_classification_still_renders(self, extraction_result):
        report = AutoReport(
            metadata=AutoReportMetadata(
                dispatch_id="test-1",
                pr_id="PR-0",
                terminal="T1",
                track="A",
                gate="test",
                status="success",
            ),
            extraction=extraction_result,
        )
        md = render_markdown(report)
        assert "**Dispatch ID**: test-1" in md
        assert "## Summary" in md

    def test_empty_extraction_renders(self):
        report = AutoReport(
            metadata=AutoReportMetadata(
                dispatch_id="test-empty",
                pr_id="PR-0",
                terminal="T1",
                track="A",
                gate="test",
                status="success",
            ),
            extraction=ExtractionResult(
                dispatch_id="test-empty",
                terminal="T1",
                track="A",
                gate="test",
            ),
        )
        md = render_markdown(report)
        assert "No file changes detected" in md
        assert "No test results captured" in md


# ─── Enum Completeness ───────────────────────────────────────────────────────

class TestEnumCompleteness:
    """Verify the taxonomy is closed — all values are known."""

    def test_dispatch_types(self):
        assert len(DispatchType) == 7

    def test_risk_levels(self):
        assert len(RiskLevel) == 3

    def test_scopes(self):
        assert len(Scope) == 4

    def test_content_types(self):
        assert len(ContentType) == 8

    def test_complexities(self):
        assert len(Complexity) == 3

    def test_outcome_statuses(self):
        assert len(OutcomeStatus) == 5
