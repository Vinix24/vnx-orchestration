#!/usr/bin/env python3
"""Tests for PR-4: Haiku Semantic Classifier.

Gate: gate_pr4_haiku_classification

Covers:
- classify_report returns rule_based when VNX_HAIKU_CLASSIFY unset
- classify_report returns rule_based when VNX_HAIKU_CLASSIFY=0
- classify_report invokes haiku when VNX_HAIKU_CLASSIFY=1 (mocked subprocess)
- Haiku subprocess failure falls back to rule_based (no crash)
- Haiku timeout falls back to rule_based (no crash)
- Haiku non-zero exit falls back to rule_based
- Valid haiku JSON response produces HaikuClassification with classified_by="haiku"
- Invalid haiku JSON falls back to rule_based
- Partial/missing JSON fields fall back to rule_based
- quality_score out of range falls back to rule_based
- content_type invalid falls back to rule_based
- complexity invalid falls back to rule_based
- consistency_score clamped to [0.0, 1.0]
- summary truncated to 200 chars
- Markdown code-fence-wrapped JSON is parsed correctly
- classify_report never raises (all errors → fallback)
- _build_prompt includes key extraction fields
- _parse_haiku_response handles clean JSON, code-fenced JSON, invalid text
- assembler uses classify_report (classified_by reflects VNX_HAIKU_CLASSIFY state)
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from auto_report_contract import (
    Complexity,
    ContentType,
    EventMetrics,
    ExtractionResult,
    GitProvenance,
    HaikuClassification,
    TestResults,
)
from report_classifier import (
    _HAIKU_MODEL,
    _build_prompt,
    _call_haiku,
    _parse_haiku_response,
    _validate_parsed,
    classify_report,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_extraction(**overrides) -> ExtractionResult:
    defaults = dict(
        dispatch_id="20260408-110915-test-classifier-A",
        terminal="T1",
        track="A",
        gate="gate_pr4_haiku_classification",
        git=GitProvenance(
            commit_hash="abc1234",
            commit_message="feat(classifier): add haiku classification",
            branch="feature/f37-pr4",
            files_changed=("scripts/lib/report_classifier.py",),
            insertions=150,
            deletions=10,
        ),
        tests=TestResults(passed=8, failed=0, errors=0, skipped=0, duration_seconds=1.5),
        events=EventMetrics(tool_use_count=20, error_count=0, session_duration_seconds=300),
        exit_summary="Implemented haiku classifier with rule_based fallback.",
    )
    defaults.update(overrides)
    return ExtractionResult(**defaults)


def _valid_haiku_json(**overrides) -> str:
    base = {
        "content_type": "implementation",
        "quality_score": 4,
        "complexity": "medium",
        "consistency_score": 0.9,
        "summary": "Implemented haiku semantic classifier with graceful fallback.",
    }
    base.update(overrides)
    return json.dumps(base)


# ─── Env-var Gate Tests ────────────────────────────────────────────────────────

class TestEnvVarGate(unittest.TestCase):

    def setUp(self):
        os.environ.pop("VNX_HAIKU_CLASSIFY", None)

    def tearDown(self):
        os.environ.pop("VNX_HAIKU_CLASSIFY", None)

    def test_unset_returns_rule_based(self):
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    def test_zero_returns_rule_based(self):
        os.environ["VNX_HAIKU_CLASSIFY"] = "0"
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    def test_false_returns_rule_based(self):
        os.environ["VNX_HAIKU_CLASSIFY"] = "false"
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku")
    def test_one_invokes_haiku(self, mock_call):
        os.environ["VNX_HAIKU_CLASSIFY"] = "1"
        mock_call.return_value = _valid_haiku_json()
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertTrue(mock_call.called)
        self.assertEqual(result.classified_by, "haiku")

    def test_rule_based_never_crashes_on_empty_extraction(self):
        ex = _make_extraction(
            git=GitProvenance(),
            tests=None,
            events=EventMetrics(),
            exit_summary="",
        )
        result = classify_report(ex)
        self.assertIn(result.classified_by, ("rule_based", "haiku"))


# ─── Haiku Fallback Tests ─────────────────────────────────────────────────────

class TestHaikuFallback(unittest.TestCase):

    def setUp(self):
        os.environ["VNX_HAIKU_CLASSIFY"] = "1"

    def tearDown(self):
        os.environ.pop("VNX_HAIKU_CLASSIFY", None)

    @patch("report_classifier._call_haiku", return_value=None)
    def test_subprocess_failure_falls_back(self, _):
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku", return_value="not json at all")
    def test_unparseable_response_falls_back(self, _):
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku", return_value="{}")
    def test_empty_json_falls_back(self, _):
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku")
    def test_invalid_content_type_falls_back(self, mock_call):
        mock_call.return_value = _valid_haiku_json(content_type="garbage")
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku")
    def test_quality_score_out_of_range_falls_back(self, mock_call):
        mock_call.return_value = _valid_haiku_json(quality_score=6)
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku")
    def test_invalid_complexity_falls_back(self, mock_call):
        mock_call.return_value = _valid_haiku_json(complexity="extreme")
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")

    @patch("report_classifier._call_haiku")
    def test_missing_required_field_falls_back(self, mock_call):
        # Missing content_type
        mock_call.return_value = json.dumps({
            "quality_score": 3,
            "complexity": "low",
            "consistency_score": 0.8,
            "summary": "test",
        })
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "rule_based")


# ─── Valid Haiku Response Tests ───────────────────────────────────────────────

class TestValidHaikuResponse(unittest.TestCase):

    def setUp(self):
        os.environ["VNX_HAIKU_CLASSIFY"] = "1"

    def tearDown(self):
        os.environ.pop("VNX_HAIKU_CLASSIFY", None)

    @patch("report_classifier._call_haiku")
    def test_valid_response_classified_by_haiku(self, mock_call):
        mock_call.return_value = _valid_haiku_json()
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.classified_by, "haiku")

    @patch("report_classifier._call_haiku")
    def test_valid_content_type_parsed(self, mock_call):
        mock_call.return_value = _valid_haiku_json(content_type="test")
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.content_type, ContentType.TEST)

    @patch("report_classifier._call_haiku")
    def test_quality_score_preserved(self, mock_call):
        mock_call.return_value = _valid_haiku_json(quality_score=5)
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.quality_score, 5)

    @patch("report_classifier._call_haiku")
    def test_complexity_parsed(self, mock_call):
        mock_call.return_value = _valid_haiku_json(complexity="high")
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.complexity, Complexity.HIGH)

    @patch("report_classifier._call_haiku")
    def test_consistency_score_preserved(self, mock_call):
        mock_call.return_value = _valid_haiku_json(consistency_score=0.75)
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertAlmostEqual(result.consistency_score, 0.75)

    @patch("report_classifier._call_haiku")
    def test_summary_preserved(self, mock_call):
        mock_call.return_value = _valid_haiku_json(summary="Feature implemented cleanly.")
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(result.summary, "Feature implemented cleanly.")

    @patch("report_classifier._call_haiku")
    def test_summary_truncated_to_200(self, mock_call):
        long_summary = "x" * 300
        mock_call.return_value = _valid_haiku_json(summary=long_summary)
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertEqual(len(result.summary), 200)

    @patch("report_classifier._call_haiku")
    def test_consistency_score_clamped_above_1(self, mock_call):
        mock_call.return_value = _valid_haiku_json(consistency_score=1.5)
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertLessEqual(result.consistency_score, 1.0)

    @patch("report_classifier._call_haiku")
    def test_consistency_score_clamped_below_0(self, mock_call):
        mock_call.return_value = _valid_haiku_json(consistency_score=-0.5)
        ex = _make_extraction()
        result = classify_report(ex)
        self.assertGreaterEqual(result.consistency_score, 0.0)

    @patch("report_classifier._call_haiku")
    def test_all_content_types_accepted(self, mock_call):
        valid_types = [
            "implementation", "test", "refactor", "docs",
            "review", "config", "planning", "mixed",
        ]
        ex = _make_extraction()
        for ct in valid_types:
            mock_call.return_value = _valid_haiku_json(content_type=ct)
            result = classify_report(ex)
            self.assertEqual(result.classified_by, "haiku", f"Failed for content_type={ct}")

    @patch("report_classifier._call_haiku")
    def test_all_complexities_accepted(self, mock_call):
        ex = _make_extraction()
        for c in ("low", "medium", "high"):
            mock_call.return_value = _valid_haiku_json(complexity=c)
            result = classify_report(ex)
            self.assertEqual(result.classified_by, "haiku", f"Failed for complexity={c}")

    @patch("report_classifier._call_haiku")
    def test_all_quality_scores_accepted(self, mock_call):
        ex = _make_extraction()
        for q in (1, 2, 3, 4, 5):
            mock_call.return_value = _valid_haiku_json(quality_score=q)
            result = classify_report(ex)
            self.assertEqual(result.quality_score, q)


# ─── _parse_haiku_response Tests ──────────────────────────────────────────────

class TestParseHaikuResponse(unittest.TestCase):

    def test_clean_json(self):
        text = '{"content_type": "implementation", "quality_score": 4}'
        result = _parse_haiku_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["content_type"], "implementation")

    def test_markdown_code_fence(self):
        text = '```json\n{"content_type": "test", "quality_score": 3}\n```'
        result = _parse_haiku_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["content_type"], "test")

    def test_json_embedded_in_prose(self):
        text = 'Here is the classification:\n{"content_type": "docs"}\nDone.'
        result = _parse_haiku_response(text)
        self.assertIsNotNone(result)

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_haiku_response(""))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_haiku_response(None))

    def test_plain_prose_returns_none(self):
        self.assertIsNone(_parse_haiku_response("This is just text without JSON."))

    def test_incomplete_json_returns_none(self):
        self.assertIsNone(_parse_haiku_response('{"content_type": "impl"'))


# ─── _build_prompt Tests ──────────────────────────────────────────────────────

class TestBuildPrompt(unittest.TestCase):

    def test_prompt_contains_dispatch_id(self):
        ex = _make_extraction()
        prompt = _build_prompt(ex)
        self.assertIn(ex.dispatch_id, prompt)

    def test_prompt_contains_exit_summary(self):
        ex = _make_extraction()
        prompt = _build_prompt(ex)
        self.assertIn(ex.exit_summary, prompt)

    def test_prompt_contains_files_changed(self):
        ex = _make_extraction()
        prompt = _build_prompt(ex)
        self.assertIn("report_classifier.py", prompt)

    def test_prompt_contains_commit_hash(self):
        ex = _make_extraction()
        prompt = _build_prompt(ex)
        self.assertIn("abc1234", prompt)

    def test_prompt_specifies_required_json_fields(self):
        ex = _make_extraction()
        prompt = _build_prompt(ex)
        for field in ("content_type", "quality_score", "complexity", "consistency_score", "summary"):
            self.assertIn(field, prompt)


# ─── _call_haiku Tests ────────────────────────────────────────────────────────

class TestCallHaiku(unittest.TestCase):

    @patch("report_classifier.subprocess.run")
    def test_success_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  {}\n  ", stderr="")
        result = _call_haiku("test prompt")
        self.assertEqual(result, "{}")

    @patch("report_classifier.subprocess.run")
    def test_non_zero_exit_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = _call_haiku("test prompt")
        self.assertIsNone(result)

    @patch("report_classifier.subprocess.run")
    def test_empty_stdout_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="   ", stderr="")
        result = _call_haiku("test prompt")
        self.assertIsNone(result)

    @patch("report_classifier.subprocess.run", side_effect=FileNotFoundError())
    def test_claude_not_found_returns_none(self, _):
        result = _call_haiku("test prompt")
        self.assertIsNone(result)

    @patch("report_classifier.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30))
    def test_timeout_returns_none(self, _):
        result = _call_haiku("test prompt")
        self.assertIsNone(result)

    @patch("report_classifier.subprocess.run")
    def test_uses_correct_model(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        _call_haiku("test")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        model_idx = cmd.index("--model")
        self.assertEqual(cmd[model_idx + 1], _HAIKU_MODEL)


# ─── _validate_parsed Tests ───────────────────────────────────────────────────

class TestValidateParsed(unittest.TestCase):

    def _valid_dict(self, **overrides):
        base = {
            "content_type": "implementation",
            "quality_score": 3,
            "complexity": "medium",
            "consistency_score": 0.8,
            "summary": "Test summary.",
        }
        base.update(overrides)
        return base

    def test_valid_dict_passes(self):
        result = _validate_parsed(self._valid_dict())
        self.assertIsNotNone(result)

    def test_invalid_content_type_returns_none(self):
        self.assertIsNone(_validate_parsed(self._valid_dict(content_type="bogus")))

    def test_quality_score_zero_returns_none(self):
        self.assertIsNone(_validate_parsed(self._valid_dict(quality_score=0)))

    def test_quality_score_six_returns_none(self):
        self.assertIsNone(_validate_parsed(self._valid_dict(quality_score=6)))

    def test_invalid_complexity_returns_none(self):
        self.assertIsNone(_validate_parsed(self._valid_dict(complexity="extreme")))

    def test_consistency_above_1_clamped(self):
        result = _validate_parsed(self._valid_dict(consistency_score=2.0))
        self.assertIsNotNone(result)
        self.assertEqual(result["consistency_score"], 1.0)

    def test_consistency_below_0_clamped(self):
        result = _validate_parsed(self._valid_dict(consistency_score=-1.0))
        self.assertIsNotNone(result)
        self.assertEqual(result["consistency_score"], 0.0)

    def test_summary_truncated(self):
        result = _validate_parsed(self._valid_dict(summary="a" * 300))
        self.assertIsNotNone(result)
        self.assertEqual(len(result["summary"]), 200)


# ─── Integration: Assembler Uses Classifier ───────────────────────────────────

class TestAssemblerIntegration(unittest.TestCase):
    """Verify the assembler imports and routes through classify_report."""

    def setUp(self):
        os.environ.pop("VNX_HAIKU_CLASSIFY", None)

    def tearDown(self):
        os.environ.pop("VNX_HAIKU_CLASSIFY", None)

    def test_assembler_rule_based_when_disabled(self):
        # Import here to avoid circular import issues at module level
        from report_assembler import assemble

        result = assemble(
            dispatch_id="20260408-999999-test-integration-A",
            terminal="T1",
            track="A",
            gate="gate_pr4_haiku_classification",
            pr_id="PR-4",
        )
        self.assertIsNotNone(result.report.classification)
        self.assertEqual(result.report.classification.classified_by, "rule_based")

    @patch("report_classifier._call_haiku")
    def test_assembler_haiku_when_enabled(self, mock_call):
        os.environ["VNX_HAIKU_CLASSIFY"] = "1"
        mock_call.return_value = _valid_haiku_json()

        from report_assembler import assemble

        result = assemble(
            dispatch_id="20260408-999999-test-integration-haiku-A",
            terminal="T1",
            track="A",
            gate="gate_pr4_haiku_classification",
            pr_id="PR-4",
        )
        self.assertIsNotNone(result.report.classification)
        self.assertEqual(result.report.classification.classified_by, "haiku")

    @patch("report_classifier._call_haiku", return_value=None)
    def test_assembler_falls_back_when_haiku_fails(self, _):
        os.environ["VNX_HAIKU_CLASSIFY"] = "1"

        from report_assembler import assemble

        result = assemble(
            dispatch_id="20260408-999999-test-fallback-A",
            terminal="T1",
            track="A",
            gate="gate_pr4_haiku_classification",
            pr_id="PR-4",
        )
        self.assertIsNotNone(result.report.classification)
        self.assertEqual(result.report.classification.classified_by, "rule_based")


if __name__ == "__main__":
    unittest.main(verbosity=2)
