#!/usr/bin/env python3
"""
Tests for PR-2: Log Artifact Persistence — Contract Section 5.3.

Gate: gate_pr2_logs_and_classification
Covers:
  - Log artifacts are created with correct header/stdout/stderr/footer
  - Output artifacts are created for non-empty stdout
  - Artifact paths are durable and readable
  - Integration: adapter produces artifacts with log pointers in results

Contract reference: docs/HEADLESS_RUN_CONTRACT.md Section 5.3
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from log_artifact import write_log_artifact, write_output_artifact, HEADER_DELIM, SECTION_DELIM
from runtime_coordination import init_schema, get_connection
from headless_adapter import HeadlessAdapter


class _TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.artifact_dir = Path(self._tmpdir) / "artifacts"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ============================================================================
# LOG ARTIFACT WRITER TESTS
# ============================================================================

class TestLogArtifactCreation(_TmpDirTestCase):

    def test_creates_artifact_file(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-001",
            dispatch_id="d-001",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="Hello world",
            stderr="",
        )
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "run-001.log")

    def test_creates_artifact_directory(self):
        nested_dir = self.artifact_dir / "nested" / "deep"
        write_log_artifact(
            artifact_dir=nested_dir,
            run_id="run-002",
            dispatch_id="d-002",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="output",
            stderr="",
        )
        self.assertTrue(nested_dir.exists())


class TestLogArtifactHeader(_TmpDirTestCase):

    def test_header_contains_identity_fields(self):
        """Section 5.3: Header must include run_id, dispatch_id, target_type, started_at."""
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-hdr-1",
            dispatch_id="d-hdr-1",
            target_type="headless_codex_cli",
            started_at="2026-03-30T12:00:00.000Z",
            stdout="",
            stderr="",
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("run_id:       run-hdr-1", content)
        self.assertIn("dispatch_id:  d-hdr-1", content)
        self.assertIn("target_type:  headless_codex_cli", content)
        self.assertIn("started_at:   2026-03-30T12:00:00.000Z", content)
        self.assertIn("VNX HEADLESS RUN LOG", content)


class TestLogArtifactStdoutStderr(_TmpDirTestCase):

    def test_stdout_section_present(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-out-1",
            dispatch_id="d-out-1",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="Analysis complete: module uses layered architecture",
            stderr="",
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("STDOUT", content)
        self.assertIn("Analysis complete: module uses layered architecture", content)

    def test_stderr_section_present(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-err-1",
            dispatch_id="d-err-1",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="",
            stderr="Error: context limit exceeded",
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("STDERR", content)
        self.assertIn("Error: context limit exceeded", content)

    def test_empty_stdout_shows_placeholder(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-empty-1",
            dispatch_id="d-empty-1",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="",
            stderr="",
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("(no stdout)", content)
        self.assertIn("(no stderr)", content)

    def test_both_stdout_and_stderr_delimited(self):
        """Section 5.3: stderr must be clearly delimited from stdout."""
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-both-1",
            dispatch_id="d-both-1",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="good output",
            stderr="warning output",
        )
        content = path.read_text(encoding="utf-8")
        stdout_pos = content.index("STDOUT")
        stderr_pos = content.index("STDERR")
        self.assertGreater(stderr_pos, stdout_pos)
        # Both sections should be delimited
        self.assertGreaterEqual(content.count(SECTION_DELIM), 4)


class TestLogArtifactFooter(_TmpDirTestCase):

    def test_footer_with_success(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-foot-1",
            dispatch_id="d-foot-1",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="done",
            stderr="",
            exit_code=0,
            duration_seconds=42.5,
            completed_at="2026-03-30T10:00:42.500Z",
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("RUN OUTCOME", content)
        self.assertIn("exit_code:        0", content)
        self.assertIn("failure_class:    N/A", content)
        self.assertIn("duration_seconds: 42.5", content)
        self.assertIn("completed_at:     2026-03-30T10:00:42.500Z", content)

    def test_footer_with_failure(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-foot-2",
            dispatch_id="d-foot-2",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="",
            stderr="rate limit exceeded",
            exit_code=1,
            failure_class="TOOL_FAIL",
            duration_seconds=5.2,
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("exit_code:        1", content)
        self.assertIn("failure_class:    TOOL_FAIL", content)
        self.assertIn("duration_seconds: 5.2", content)

    def test_footer_defaults_na_for_missing(self):
        path = write_log_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-foot-3",
            dispatch_id="d-foot-3",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="",
            stderr="",
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("exit_code:        N/A", content)
        self.assertIn("failure_class:    N/A", content)
        self.assertIn("duration_seconds: N/A", content)


# ============================================================================
# OUTPUT ARTIFACT TESTS
# ============================================================================

class TestOutputArtifact(_TmpDirTestCase):

    def test_creates_output_file(self):
        path = write_output_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-oa-1",
            stdout="structured output here",
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "structured output here")

    def test_returns_none_for_empty_stdout(self):
        path = write_output_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-oa-2",
            stdout="",
        )
        self.assertIsNone(path)

    def test_returns_none_for_whitespace_only(self):
        path = write_output_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-oa-3",
            stdout="   \n  \t  ",
        )
        self.assertIsNone(path)

    def test_output_filename_pattern(self):
        path = write_output_artifact(
            artifact_dir=self.artifact_dir,
            run_id="run-oa-4",
            stdout="content",
        )
        self.assertEqual(path.name, "run-oa-4.output.txt")


# ============================================================================
# ADAPTER INTEGRATION — ARTIFACT PATHS IN RESULTS
# ============================================================================

class _DBTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()
        self.dispatch_dir = Path(self._tmpdir) / "dispatches"
        self.dispatch_dir.mkdir()
        self.artifact_dir = Path(self._tmpdir) / "artifacts"

        schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
        init_schema(self.state_dir, schemas_dir / "runtime_coordination.sql")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_bundle(self, dispatch_id, prompt="test prompt"):
        bundle_dir = self.dispatch_dir / dispatch_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle = {"dispatch_id": dispatch_id, "bundle_version": 1}
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle))
        (bundle_dir / "prompt.txt").write_text(prompt)

    def _register_dispatch(self, dispatch_id):
        with get_connection(self.state_dir) as conn:
            from runtime_coordination import register_dispatch
            register_dispatch(conn, dispatch_id=dispatch_id)
            conn.commit()


class TestAdapterArtifactIntegration(_DBTestCase):

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_success_produces_log_artifact(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-art-1")
        self._register_dispatch("d-art-1")
        adapter = HeadlessAdapter(
            self.state_dir, self.dispatch_dir, artifact_dir=self.artifact_dir,
        )
        mock_run.return_value = MagicMock(returncode=0, stdout="Analysis done", stderr="")
        result = adapter.execute(
            "d-art-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        self.assertTrue(result.success)
        self.assertIsNotNone(result.log_artifact_path)
        self.assertTrue(Path(result.log_artifact_path).exists())
        self.assertEqual(result.failure_class, "SUCCESS")

        # Log artifact contains run data
        content = Path(result.log_artifact_path).read_text(encoding="utf-8")
        self.assertIn("d-art-1", content)
        self.assertIn("Analysis done", content)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_failure_produces_log_artifact_with_classification(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-art-2")
        self._register_dispatch("d-art-2")
        adapter = HeadlessAdapter(
            self.state_dir, self.dispatch_dir, artifact_dir=self.artifact_dir,
        )
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="API error: rate limit exceeded"
        )
        result = adapter.execute(
            "d-art-2", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, "TOOL_FAIL")
        self.assertIsNotNone(result.classification_evidence)
        self.assertEqual(result.classification_evidence["failure_class"], "TOOL_FAIL")
        self.assertTrue(result.classification_evidence["retryable"])

        # Log artifact includes stderr
        content = Path(result.log_artifact_path).read_text(encoding="utf-8")
        self.assertIn("API error: rate limit exceeded", content)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_timeout_classification_in_result(self, mock_run, mock_which, mock_enabled):
        import subprocess as sp
        self._write_bundle("d-art-3")
        self._register_dispatch("d-art-3")
        adapter = HeadlessAdapter(
            self.state_dir, self.dispatch_dir, artifact_dir=self.artifact_dir,
        )
        mock_run.side_effect = sp.TimeoutExpired(cmd="claude", timeout=600)
        result = adapter.execute(
            "d-art-3", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, "TIMEOUT")
        self.assertIsNotNone(result.log_artifact_path)
        self.assertTrue(Path(result.log_artifact_path).exists())

    @patch("headless_adapter.headless_enabled", return_value=True)
    def test_missing_binary_classification(self, mock_enabled):
        self._write_bundle("d-art-4")
        self._register_dispatch("d-art-4")
        adapter = HeadlessAdapter(
            self.state_dir, self.dispatch_dir, artifact_dir=self.artifact_dir,
        )
        with patch("shutil.which", return_value=None):
            result = adapter.execute(
                "d-art-4", "headless_claude_cli_T2", "headless_claude_cli",
                task_class="research_structured",
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, "INFRA_FAIL")

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_classification_in_coordination_events(self, mock_run, mock_which, mock_enabled):
        """Receipts/linked state expose log pointers (gate criterion)."""
        self._write_bundle("d-art-5")
        self._register_dispatch("d-art-5")
        adapter = HeadlessAdapter(
            self.state_dir, self.dispatch_dir, artifact_dir=self.artifact_dir,
        )
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Permission denied"
        )
        adapter.execute(
            "d-art-5", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE entity_id = ? AND event_type = 'headless_execution_failed'",
                ("d-art-5",),
            ).fetchall()
        self.assertGreaterEqual(len(events), 1)
        metadata = json.loads(events[0]["metadata_json"])
        self.assertEqual(metadata["failure_class"], "INFRA_FAIL")
        self.assertIn("log_artifact_path", metadata)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_success_event_includes_artifact_paths(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-art-6")
        self._register_dispatch("d-art-6")
        adapter = HeadlessAdapter(
            self.state_dir, self.dispatch_dir, artifact_dir=self.artifact_dir,
        )
        mock_run.return_value = MagicMock(returncode=0, stdout="Result data", stderr="")
        adapter.execute(
            "d-art-6", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE entity_id = ? AND event_type = 'headless_execution_completed'",
                ("d-art-6",),
            ).fetchall()
        self.assertGreaterEqual(len(events), 1)
        metadata = json.loads(events[0]["metadata_json"])
        self.assertIn("log_artifact_path", metadata)
        self.assertIn("output_artifact_path", metadata)
        self.assertEqual(metadata["failure_class"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
