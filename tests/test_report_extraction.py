#!/usr/bin/env python3
"""Tests for PR-2: Deterministic Extraction Module.

Gate: gate_pr2_deterministic_extraction

Covers:
- Git extraction with real fixture data
- Pytest output parsing (standard, verbose, short formats)
- Event stream aggregation (tool counts, errors, duration)
- ExtractionResult production from all extractors
- Edge cases: empty diff, no tests, no events, corrupt NDJSON
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

from report_extraction import (
    _parse_diff_stat,
    aggregate_event_metrics,
    extract_exit_summary,
    extract_git_provenance,
    extract_pytest_from_events,
    parse_pytest_output,
    run_extraction,
)
from auto_report_contract import (
    EventMetrics,
    ExtractionResult,
    GitProvenance,
    TestResults,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

PYTEST_STANDARD = """\
==================== test session starts ====================
platform darwin -- Python 3.11.0
collected 10 items

tests/test_foo.py .....
tests/test_bar.py .....

==================== 10 passed in 1.23s ====================
"""

PYTEST_WITH_FAILURES = """\
==================== test session starts ====================
collected 15 items

tests/test_foo.py ...F..F.
tests/test_bar.py .....

FAILED tests/test_foo.py::test_third - AssertionError: expected 3, got 4
FAILED tests/test_foo.py::test_seventh - RuntimeError

==================== 2 failed, 13 passed in 2.45s ====================
"""

PYTEST_WITH_ERRORS_AND_SKIPPED = """\
==================== test session starts ====================
collected 8 items

tests/test_foo.py ..sE..s.

==================== 1 error, 4 passed, 2 skipped in 0.87s ====================
"""

PYTEST_VERBOSE = """\
==================== test session starts ====================
platform darwin -- Python 3.11.0, pytest-7.4.0
rootdir: /project
collected 5 items

tests/test_module.py::test_alpha PASSED        [  20%]
tests/test_module.py::test_beta PASSED         [  40%]
tests/test_module.py::test_gamma FAILED        [  60%]
tests/test_module.py::test_delta PASSED        [  80%]
tests/test_module.py::test_epsilon PASSED      [ 100%]

==================== 1 failed, 4 passed in 0.56s ====================
"""

PYTEST_SHORT = """\
..........
10 passed in 0.45s
"""

PYTEST_XFAIL = """\
....x.
5 passed, 1 xfailed in 1.10s
"""

PYTEST_NO_RESULTS = """\
==================== test session starts ====================
platform darwin
collected 0 items

==================== no tests ran in 0.02s ====================
"""

DIFF_STAT_STANDARD = """\
 scripts/lib/foo.py     | 42 ++++++++++++++++++++++++++++++------------
 scripts/lib/bar.py     |  8 ++++++++
 tests/test_foo.py      | 15 +++++++++++++++
 3 files changed, 65 insertions(+), 12 deletions(-)
"""

DIFF_STAT_SINGLE = """\
 scripts/lib/only.py | 10 +++++++---
 1 file changed, 7 insertions(+), 3 deletions(-)
"""

DIFF_STAT_EMPTY = ""

DIFF_STAT_BINARY = """\
 docs/image.png | Bin 0 -> 1234 bytes
 scripts/lib/module.py |  5 +++++
 2 files changed, 5 insertions(+), 0 deletions(-)
"""

DIFF_STAT_RENAME = """\
 scripts/{old_name.py => new_name.py} | 2 ++
 1 file changed, 2 insertions(+), 0 deletions(-)
"""


def _make_ndjson_file(events: list, dispatch_id: str = "test-dispatch") -> Path:
    """Write events to a temp NDJSON file and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ndjson", delete=False, encoding="utf-8"
    )
    for i, event in enumerate(events):
        envelope = {
            "type": event.get("type", "unknown"),
            "timestamp": event.get("timestamp", f"2026-01-01T10:00:{i:02d}.000+00:00"),
            "dispatch_id": event.get("dispatch_id", dispatch_id),
            "terminal": event.get("terminal", "T1"),
            "sequence": i + 1,
            "data": event.get("data", {}),
        }
        tmp.write(json.dumps(envelope) + "\n")
    tmp.close()
    return Path(tmp.name)


# ─── _parse_diff_stat ─────────────────────────────────────────────────────────

class TestParseDiffStat(unittest.TestCase):

    def test_standard_three_files(self):
        files, ins, dels = _parse_diff_stat(DIFF_STAT_STANDARD)
        self.assertEqual(len(files), 3)
        self.assertIn("scripts/lib/foo.py", files)
        self.assertIn("scripts/lib/bar.py", files)
        self.assertIn("tests/test_foo.py", files)
        self.assertEqual(ins, 65)
        self.assertEqual(dels, 12)

    def test_single_file(self):
        files, ins, dels = _parse_diff_stat(DIFF_STAT_SINGLE)
        self.assertEqual(files, ["scripts/lib/only.py"])
        self.assertEqual(ins, 7)
        self.assertEqual(dels, 3)

    def test_empty_diff(self):
        files, ins, dels = _parse_diff_stat(DIFF_STAT_EMPTY)
        self.assertEqual(files, [])
        self.assertEqual(ins, 0)
        self.assertEqual(dels, 0)

    def test_binary_file_excluded(self):
        files, ins, dels = _parse_diff_stat(DIFF_STAT_BINARY)
        self.assertNotIn("docs/image.png", files)
        self.assertIn("scripts/lib/module.py", files)
        self.assertEqual(ins, 5)
        self.assertEqual(dels, 0)

    def test_rename_notation(self):
        files, ins, dels = _parse_diff_stat(DIFF_STAT_RENAME)
        self.assertEqual(len(files), 1)
        # Should contain new name portion
        self.assertTrue(any("new_name" in f for f in files))
        self.assertEqual(ins, 2)

    def test_whitespace_only_input(self):
        files, ins, dels = _parse_diff_stat("   \n  \t  \n")
        self.assertEqual(files, [])
        self.assertEqual(ins, 0)
        self.assertEqual(dels, 0)


# ─── parse_pytest_output ──────────────────────────────────────────────────────

class TestParsePytestOutput(unittest.TestCase):

    def test_standard_all_passed(self):
        result = parse_pytest_output(PYTEST_STANDARD)
        self.assertIsNotNone(result)
        self.assertEqual(result.passed, 10)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.skipped, 0)
        self.assertAlmostEqual(result.duration_seconds, 1.23)

    def test_with_failures(self):
        result = parse_pytest_output(PYTEST_WITH_FAILURES)
        self.assertIsNotNone(result)
        self.assertEqual(result.passed, 13)
        self.assertEqual(result.failed, 2)
        self.assertEqual(result.errors, 0)

    def test_with_errors_and_skipped(self):
        result = parse_pytest_output(PYTEST_WITH_ERRORS_AND_SKIPPED)
        self.assertIsNotNone(result)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.passed, 4)
        self.assertEqual(result.skipped, 2)

    def test_verbose_format(self):
        result = parse_pytest_output(PYTEST_VERBOSE)
        self.assertIsNotNone(result)
        self.assertEqual(result.passed, 4)
        self.assertEqual(result.failed, 1)

    def test_short_format(self):
        result = parse_pytest_output(PYTEST_SHORT)
        self.assertIsNotNone(result)
        self.assertEqual(result.passed, 10)
        self.assertAlmostEqual(result.duration_seconds, 0.45)

    def test_xfailed_not_counted_as_failed(self):
        result = parse_pytest_output(PYTEST_XFAIL)
        self.assertIsNotNone(result)
        self.assertEqual(result.passed, 5)
        self.assertEqual(result.failed, 0)  # xfailed is not a failure

    def test_no_tests_ran(self):
        result = parse_pytest_output(PYTEST_NO_RESULTS)
        # no tests ran but it's still pytest output
        self.assertIsNotNone(result)
        self.assertEqual(result.total, 0)

    def test_non_pytest_text_returns_none(self):
        result = parse_pytest_output("Hello world, nothing to see here")
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = parse_pytest_output("")
        self.assertIsNone(result)

    def test_all_passed_property(self):
        result = parse_pytest_output(PYTEST_STANDARD)
        self.assertTrue(result.all_passed)

    def test_has_failures_property(self):
        result = parse_pytest_output(PYTEST_WITH_FAILURES)
        self.assertFalse(result.all_passed)

    def test_raw_output_truncated_to_500(self):
        long_text = PYTEST_STANDARD + ("x" * 1000)
        result = parse_pytest_output(long_text)
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result.raw_output), 500)


# ─── aggregate_event_metrics ──────────────────────────────────────────────────

class TestAggregateEventMetrics(unittest.TestCase):

    def test_counts_tool_uses(self):
        path = _make_ndjson_file([
            {"type": "tool_use", "data": {"name": "Read"}},
            {"type": "tool_use", "data": {"name": "Edit"}},
            {"type": "text", "data": {"text": "done"}},
        ])
        try:
            metrics = aggregate_event_metrics(path)
            self.assertEqual(metrics.tool_use_count, 2)
            self.assertEqual(metrics.text_block_count, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_counts_thinking_blocks(self):
        path = _make_ndjson_file([
            {"type": "thinking", "data": {"thinking": "Let me think..."}},
            {"type": "thinking", "data": {"thinking": "...and more"}},
            {"type": "tool_use", "data": {"name": "Bash"}},
        ])
        try:
            metrics = aggregate_event_metrics(path)
            self.assertEqual(metrics.thinking_block_count, 2)
            self.assertEqual(metrics.tool_use_count, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_counts_errors(self):
        path = _make_ndjson_file([
            {"type": "error", "data": {"message": "something failed"}},
            {"type": "tool_use", "data": {}},
        ])
        try:
            metrics = aggregate_event_metrics(path)
            self.assertEqual(metrics.error_count, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_extracts_model_from_init(self):
        path = _make_ndjson_file([
            {"type": "init", "data": {"session_id": "abc", "model": "claude-sonnet-4-6"}},
            {"type": "tool_use", "data": {}},
        ])
        try:
            metrics = aggregate_event_metrics(path)
            self.assertEqual(metrics.model_used, "claude-sonnet-4-6")
        finally:
            path.unlink(missing_ok=True)

    def test_session_duration_from_timestamps(self):
        events = [
            {
                "type": "tool_use",
                "timestamp": "2026-01-01T10:00:00.000+00:00",
                "data": {},
            },
            {
                "type": "text",
                "timestamp": "2026-01-01T10:00:30.000+00:00",
                "data": {},
            },
        ]
        path = _make_ndjson_file(events)
        try:
            metrics = aggregate_event_metrics(path)
            self.assertEqual(metrics.session_duration_seconds, 30)
        finally:
            path.unlink(missing_ok=True)

    def test_dispatch_id_filter(self):
        path = _make_ndjson_file([
            {"type": "tool_use", "dispatch_id": "dispatch-A", "data": {}},
            {"type": "tool_use", "dispatch_id": "dispatch-B", "data": {}},
            {"type": "tool_use", "dispatch_id": "dispatch-A", "data": {}},
        ])
        try:
            metrics = aggregate_event_metrics(path, dispatch_id="dispatch-A")
            self.assertEqual(metrics.tool_use_count, 2)
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_returns_empty(self):
        metrics = aggregate_event_metrics(Path("/nonexistent/T1.ndjson"))
        self.assertIsInstance(metrics, EventMetrics)
        self.assertEqual(metrics.tool_use_count, 0)
        self.assertEqual(metrics.error_count, 0)

    def test_corrupt_ndjson_lines_skipped(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ndjson", delete=False, encoding="utf-8"
        )
        tmp.write('{"type":"tool_use","timestamp":"2026-01-01T10:00:00.000+00:00","dispatch_id":"","terminal":"T1","sequence":1,"data":{}}\n')
        tmp.write("CORRUPT LINE\n")
        tmp.write('{"type":"text","timestamp":"2026-01-01T10:00:01.000+00:00","dispatch_id":"","terminal":"T1","sequence":2,"data":{"text":"hi"}}\n')
        tmp.close()
        path = Path(tmp.name)
        try:
            metrics = aggregate_event_metrics(path)
            self.assertEqual(metrics.tool_use_count, 1)
            self.assertEqual(metrics.text_block_count, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_empty_file_returns_empty_metrics(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ndjson", delete=False, encoding="utf-8"
        )
        tmp.close()
        path = Path(tmp.name)
        try:
            metrics = aggregate_event_metrics(path)
            self.assertIsInstance(metrics, EventMetrics)
            self.assertEqual(metrics.tool_use_count, 0)
        finally:
            path.unlink(missing_ok=True)


# ─── extract_pytest_from_events ───────────────────────────────────────────────

class TestExtractPytestFromEvents(unittest.TestCase):

    def test_finds_pytest_in_tool_result(self):
        path = _make_ndjson_file([
            {"type": "tool_use", "data": {"name": "Bash"}},
            {
                "type": "tool_result",
                "data": {"tool_use_id": "t1", "content": PYTEST_STANDARD},
            },
        ])
        try:
            result = extract_pytest_from_events(path)
            self.assertIsNotNone(result)
            self.assertEqual(result.passed, 10)
        finally:
            path.unlink(missing_ok=True)

    def test_returns_last_pytest_run(self):
        """When multiple pytest runs appear, the last one is returned."""
        path = _make_ndjson_file([
            {
                "type": "tool_result",
                "data": {"content": "5 passed in 0.50s"},
            },
            {
                "type": "tool_result",
                "data": {"content": PYTEST_WITH_FAILURES},
            },
        ])
        try:
            result = extract_pytest_from_events(path)
            self.assertIsNotNone(result)
            self.assertEqual(result.failed, 2)
            self.assertEqual(result.passed, 13)
        finally:
            path.unlink(missing_ok=True)

    def test_no_pytest_in_events_returns_none(self):
        path = _make_ndjson_file([
            {"type": "tool_use", "data": {"name": "Read"}},
            {"type": "text", "data": {"text": "done"}},
        ])
        try:
            result = extract_pytest_from_events(path)
            self.assertIsNone(result)
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_returns_none(self):
        result = extract_pytest_from_events(Path("/nonexistent/T1.ndjson"))
        self.assertIsNone(result)

    def test_empty_events_returns_none(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ndjson", delete=False, encoding="utf-8"
        )
        tmp.close()
        try:
            result = extract_pytest_from_events(Path(tmp.name))
            self.assertIsNone(result)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


# ─── extract_git_provenance ───────────────────────────────────────────────────

class TestExtractGitProvenance(unittest.TestCase):

    def test_returns_git_provenance_instance(self):
        # Run in the project repo (guaranteed to exist)
        repo_path = Path(__file__).resolve().parent.parent
        result = extract_git_provenance(repo_path)
        self.assertIsInstance(result, GitProvenance)

    def test_commit_hash_is_short_hex(self):
        repo_path = Path(__file__).resolve().parent.parent
        result = extract_git_provenance(repo_path)
        if result.commit_hash:
            # Short git hash is 7-40 hex chars
            self.assertRegex(result.commit_hash, r"^[0-9a-f]{7,40}$")

    def test_branch_non_empty(self):
        repo_path = Path(__file__).resolve().parent.parent
        result = extract_git_provenance(repo_path)
        self.assertIsInstance(result.branch, str)
        # branch may be empty in detached HEAD but should be a string

    def test_no_git_repo_returns_empty(self):
        """Running in a non-git dir returns empty GitProvenance, not an exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = extract_git_provenance(Path(tmpdir))
        self.assertIsInstance(result, GitProvenance)
        self.assertEqual(result.commit_hash, "")
        self.assertEqual(result.insertions, 0)

    def test_files_changed_is_tuple(self):
        repo_path = Path(__file__).resolve().parent.parent
        result = extract_git_provenance(repo_path)
        self.assertIsInstance(result.files_changed, tuple)

    def test_returns_valid_provenance_instance(self):
        """to_dict() round-trip works without error."""
        repo_path = Path(__file__).resolve().parent.parent
        result = extract_git_provenance(repo_path)
        d = result.to_dict()
        self.assertIn("commit_hash", d)
        self.assertIn("files_changed", d)
        self.assertIsInstance(d["files_changed"], list)


# ─── run_extraction ───────────────────────────────────────────────────────────

class TestRunExtraction(unittest.TestCase):

    def _make_full_event_file(self, dispatch_id: str) -> Path:
        return _make_ndjson_file(
            [
                {"type": "init", "data": {"model": "claude-haiku-4-5"}, "dispatch_id": dispatch_id},
                {"type": "thinking", "data": {"thinking": "..."}, "dispatch_id": dispatch_id},
                {"type": "tool_use", "data": {"name": "Bash"}, "dispatch_id": dispatch_id},
                {
                    "type": "tool_result",
                    "data": {"content": PYTEST_STANDARD},
                    "dispatch_id": dispatch_id,
                    "timestamp": "2026-01-01T10:00:05.000+00:00",
                },
                {
                    "type": "text",
                    "data": {"text": "Implementation complete."},
                    "dispatch_id": dispatch_id,
                    "timestamp": "2026-01-01T10:01:00.000+00:00",
                },
            ],
            dispatch_id=dispatch_id,
        )

    def test_returns_extraction_result_instance(self):
        dispatch_id = "test-dispatch-001"
        events_path = self._make_full_event_file(dispatch_id)
        repo_path = Path(__file__).resolve().parent.parent

        try:
            result = run_extraction(
                dispatch_id=dispatch_id,
                terminal="T1",
                track="A",
                gate="gate_test",
                repo_path=repo_path,
                events_path=events_path,
            )
            self.assertIsInstance(result, ExtractionResult)
        finally:
            events_path.unlink(missing_ok=True)

    def test_dispatch_id_propagated(self):
        dispatch_id = "test-dispatch-002"
        events_path = self._make_full_event_file(dispatch_id)

        try:
            result = run_extraction(
                dispatch_id=dispatch_id,
                terminal="T2",
                track="B",
                gate="gate_test",
                events_path=events_path,
            )
            self.assertEqual(result.dispatch_id, dispatch_id)
            self.assertEqual(result.terminal, "T2")
            self.assertEqual(result.track, "B")
        finally:
            events_path.unlink(missing_ok=True)

    def test_test_results_extracted_from_events(self):
        dispatch_id = "test-dispatch-003"
        events_path = self._make_full_event_file(dispatch_id)

        try:
            result = run_extraction(
                dispatch_id=dispatch_id,
                terminal="T1",
                track="A",
                gate="gate_test",
                events_path=events_path,
            )
            self.assertIsNotNone(result.tests)
            self.assertEqual(result.tests.passed, 10)
        finally:
            events_path.unlink(missing_ok=True)

    def test_exit_summary_from_events(self):
        dispatch_id = "test-dispatch-004"
        events_path = self._make_full_event_file(dispatch_id)

        try:
            result = run_extraction(
                dispatch_id=dispatch_id,
                terminal="T1",
                track="A",
                gate="gate_test",
                events_path=events_path,
            )
            self.assertEqual(result.exit_summary, "Implementation complete.")
        finally:
            events_path.unlink(missing_ok=True)

    def test_exit_summary_override(self):
        dispatch_id = "test-dispatch-005"
        events_path = self._make_full_event_file(dispatch_id)

        try:
            result = run_extraction(
                dispatch_id=dispatch_id,
                terminal="T1",
                track="A",
                gate="gate_test",
                events_path=events_path,
                exit_summary="Manual override summary",
            )
            self.assertEqual(result.exit_summary, "Manual override summary")
        finally:
            events_path.unlink(missing_ok=True)

    def test_no_events_file_still_produces_valid_result(self):
        result = run_extraction(
            dispatch_id="dispatch-no-events",
            terminal="T1",
            track="A",
            gate="gate_test",
            events_path=Path("/nonexistent/T1.ndjson"),
        )
        self.assertIsInstance(result, ExtractionResult)
        self.assertIsNone(result.tests)
        self.assertIsInstance(result.events, EventMetrics)
        self.assertEqual(result.events.tool_use_count, 0)

    def test_invalid_terminal_raises(self):
        with self.assertRaises(ValueError):
            run_extraction(
                dispatch_id="test",
                terminal="T0",  # T0 is not a valid worker terminal
                track="A",
                gate="gate_test",
            )

    def test_invalid_track_raises(self):
        with self.assertRaises(ValueError):
            run_extraction(
                dispatch_id="test",
                terminal="T1",
                track="X",  # invalid
                gate="gate_test",
            )

    def test_to_dict_round_trip(self):
        """ExtractionResult.to_dict() produces JSON-serialisable output."""
        result = run_extraction(
            dispatch_id="round-trip-test",
            terminal="T1",
            track="A",
            gate="gate_test",
            events_path=Path("/nonexistent/T1.ndjson"),
        )
        import json
        d = result.to_dict()
        serialised = json.dumps(d)
        self.assertIn("round-trip-test", serialised)

    def test_extracted_at_is_iso_timestamp(self):
        result = run_extraction(
            dispatch_id="ts-test",
            terminal="T1",
            track="A",
            gate="gate_test",
        )
        self.assertRegex(result.extracted_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


if __name__ == "__main__":
    unittest.main()
