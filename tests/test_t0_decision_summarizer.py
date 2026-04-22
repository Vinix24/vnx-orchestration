#!/usr/bin/env python3
"""Tests for t0_decision_summarizer.py

Covers:
- Event extraction from NDJSON
- Decision log append with file locking
- Dry-run mode
- Empty events handling
- Malformed haiku output handling (graceful fallback)
"""

from __future__ import annotations

import json
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from t0_decision_summarizer import (
    DEFAULT_DECISION_LOG,
    DEFAULT_EVENTS_FILE,
    _ROTATION_BYTES,
    _build_fallback,
    _parse_haiku_output,
    _rotate_if_needed,
    append_decision_record,
    extract_text_content,
    load_events,
    main,
    summarize_with_haiku,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {"type": "init", "sequence": 1, "data": {"session_id": "abc123", "model": "claude-sonnet-4-6"}},
    {"type": "thinking", "sequence": 2, "data": {"thinking": "Let me check the queue."}},
    {"type": "tool_use", "sequence": 3, "data": {"name": "Bash", "input": {"command": "ls"}, "id": "t1"}},
    {"type": "tool_result", "sequence": 4, "data": {"tool_use_id": "t1", "content": "file.txt"}},
    {"type": "text", "sequence": 5, "data": {"text": "Track A receipt approved. Dispatching Track B."}},
    {"type": "result", "sequence": 6, "data": {"text": "Track A receipt approved. Dispatching Track B.", "subtype": "success", "session_id": "abc123"}},
]

EMPTY_EVENTS: list = []

EVENTS_NO_TEXT = [
    {"type": "init", "sequence": 1, "data": {}},
    {"type": "tool_use", "sequence": 2, "data": {"name": "Bash", "id": "t1"}},
]

HAIKU_DECISION_JSON = {
    "timestamp": "2026-04-07T12:00:00Z",
    "session_summary_at": "2026-04-07T12:00:00Z",
    "action": "approve",
    "dispatch_id": "20260407-050001-f36-A",
    "track": "A",
    "reasoning": "Track A receipt verified. All tests pass.",
    "open_items_actions": [],
    "next_expected": "Track B dispatch completion receipt",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_events_file(tmp_path: Path, events: list) -> Path:
    f = tmp_path / "T0.ndjson"
    f.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return f


def make_haiku_response(record: dict) -> str:
    """Simulate claude --output-format json wrapping the decision JSON."""
    return json.dumps({"result": json.dumps(record), "session_id": "haiku-session"})


# ---------------------------------------------------------------------------
# load_events
# ---------------------------------------------------------------------------

class TestLoadEvents:
    def test_loads_all_events(self, tmp_path):
        f = make_events_file(tmp_path, SAMPLE_EVENTS)
        events = load_events(f)
        assert len(events) == len(SAMPLE_EVENTS)

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_events(tmp_path / "nonexistent.ndjson")

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "bad.ndjson"
        f.write_text('{"type":"init"}\nNOT_JSON\n{"type":"text","data":{"text":"hi"}}\n')
        events = load_events(f)
        assert len(events) == 2

    def test_empty_file_returns_empty_list(self, tmp_path):
        f = tmp_path / "empty.ndjson"
        f.write_text("")
        events = load_events(f)
        assert events == []

    def test_blank_lines_skipped(self, tmp_path):
        f = tmp_path / "blanks.ndjson"
        f.write_text('\n{"type":"init"}\n\n{"type":"text","data":{"text":"x"}}\n\n')
        events = load_events(f)
        assert len(events) == 2


# ---------------------------------------------------------------------------
# extract_text_content
# ---------------------------------------------------------------------------

class TestExtractTextContent:
    def test_extracts_text_and_result_events(self):
        content = extract_text_content(SAMPLE_EVENTS)
        assert "Track A receipt approved" in content

    def test_deduplicates_joined_with_double_newline(self):
        events = [
            {"type": "text", "data": {"text": "Part one"}},
            {"type": "result", "data": {"text": "Part two"}},
        ]
        content = extract_text_content(events)
        assert content == "Part one\n\nPart two"

    def test_ignores_non_text_events(self):
        content = extract_text_content(EVENTS_NO_TEXT)
        assert content == ""

    def test_empty_events(self):
        content = extract_text_content(EMPTY_EVENTS)
        assert content == ""

    def test_events_with_empty_text_field_skipped(self):
        events = [{"type": "text", "data": {"text": ""}}, {"type": "text", "data": {"text": "hello"}}]
        content = extract_text_content(events)
        assert content == "hello"


# ---------------------------------------------------------------------------
# _parse_haiku_output
# ---------------------------------------------------------------------------

class TestParseHaikuOutput:
    def test_parses_valid_decision_json(self):
        stdout = make_haiku_response(HAIKU_DECISION_JSON)
        record = _parse_haiku_output(stdout)
        assert record["action"] == "approve"
        assert record["dispatch_id"] == "20260407-050001-f36-A"
        assert record["track"] == "A"

    def test_falls_back_on_invalid_outer_json(self):
        record = _parse_haiku_output("NOT_JSON")
        assert record["action"] == "wait"
        assert "Haiku summarization failed" in record["reasoning"]

    def test_falls_back_on_empty_result(self):
        record = _parse_haiku_output(json.dumps({"result": ""}))
        assert record["action"] == "wait"

    def test_falls_back_on_invalid_inner_json(self):
        stdout = json.dumps({"result": "not json at all"})
        record = _parse_haiku_output(stdout)
        assert record["action"] == "wait"

    def test_strips_markdown_fences(self):
        inner = json.dumps(HAIKU_DECISION_JSON)
        wrapped = f"```json\n{inner}\n```"
        stdout = json.dumps({"result": wrapped})
        record = _parse_haiku_output(stdout)
        assert record["action"] == "approve"

    def test_defaults_missing_fields(self):
        partial = {"action": "dispatch", "reasoning": "Sending T1"}
        stdout = make_haiku_response(partial)
        record = _parse_haiku_output(stdout)
        assert record["dispatch_id"] is None
        assert record["open_items_actions"] == []
        assert record["next_expected"] == ""
        assert "timestamp" in record


# ---------------------------------------------------------------------------
# summarize_with_haiku
# ---------------------------------------------------------------------------

class TestSummarizeWithHaiku:
    def _make_popen_mock(self, stdout: str, returncode: int = 0):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout, "")
        mock_proc.returncode = returncode
        return mock_proc

    def test_returns_parsed_decision_on_success(self):
        stdout = make_haiku_response(HAIKU_DECISION_JSON)
        mock_proc = self._make_popen_mock(stdout)
        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc):
            record = summarize_with_haiku("Track A approved.")
        assert record["action"] == "approve"

    def test_falls_back_on_nonzero_returncode(self):
        mock_proc = self._make_popen_mock("", returncode=1)
        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc):
            record = summarize_with_haiku("some output")
        assert record["action"] == "wait"

    def test_falls_back_when_claude_not_found(self):
        with patch("t0_decision_summarizer.subprocess.Popen", side_effect=FileNotFoundError):
            record = summarize_with_haiku("some output")
        assert record["action"] == "wait"

    def test_falls_back_on_timeout(self):
        import subprocess as real_subprocess
        mock_proc = self._make_popen_mock("")
        # First communicate() (with timeout) raises; second (cleanup) returns normally
        mock_proc.communicate.side_effect = [
            real_subprocess.TimeoutExpired(cmd="claude", timeout=120),
            ("", ""),
        ]
        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc):
            record = summarize_with_haiku("some output")
        assert record["action"] == "wait"

    def test_uses_dangerously_skip_permissions_flag(self):
        stdout = make_haiku_response(HAIKU_DECISION_JSON)
        mock_proc = self._make_popen_mock(stdout)
        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc) as mock_popen:
            summarize_with_haiku("text")
        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    def test_uses_haiku_model(self):
        stdout = make_haiku_response(HAIKU_DECISION_JSON)
        mock_proc = self._make_popen_mock(stdout)
        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc) as mock_popen:
            summarize_with_haiku("text")
        cmd = mock_popen.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "haiku"


# ---------------------------------------------------------------------------
# append_decision_record
# ---------------------------------------------------------------------------

class TestAppendDecisionRecord:
    def test_appends_valid_jsonl_line(self, tmp_path):
        log_file = tmp_path / "decisions.jsonl"
        record = {"action": "approve", "reasoning": "ok"}
        append_decision_record(record, log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "approve"

    def test_appends_multiple_records(self, tmp_path):
        log_file = tmp_path / "decisions.jsonl"
        for i in range(3):
            append_decision_record({"action": "wait", "index": i}, log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_creates_parent_directory(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "decisions.jsonl"
        append_decision_record({"action": "wait"}, log_file)
        assert log_file.exists()

    def test_appends_to_existing_file(self, tmp_path):
        log_file = tmp_path / "decisions.jsonl"
        log_file.write_text('{"action":"prior"}\n')
        append_decision_record({"action": "new"}, log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["action"] == "prior"
        assert json.loads(lines[1])["action"] == "new"


# ---------------------------------------------------------------------------
# _rotate_if_needed — lock-safe rotation
# ---------------------------------------------------------------------------

class TestRotateIfNeeded:
    def _large_content(self) -> str:
        return '{"action":"wait"}\n' * ((_ROTATION_BYTES // 18) + 1)

    def test_below_threshold_leaves_file_unchanged(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        log_file.write_text('{"action":"wait"}\n')
        original = log_file.read_text()
        _rotate_if_needed(log_file)
        assert log_file.read_text() == original

    def test_nonexistent_file_does_nothing(self, tmp_path):
        _rotate_if_needed(tmp_path / "nonexistent.jsonl")

    def test_rotation_truncates_original(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        log_file.write_text(self._large_content())
        _rotate_if_needed(log_file)
        assert log_file.stat().st_size == 0

    def test_rotation_archives_original_content(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        content = self._large_content()
        log_file.write_text(content)
        _rotate_if_needed(log_file)
        archives = list((tmp_path / "archive").glob("*.jsonl"))
        assert len(archives) == 1
        assert archives[0].read_text() == content

    def test_rotation_creates_archive_directory(self, tmp_path):
        log_file = tmp_path / "state" / "test.jsonl"
        log_file.parent.mkdir(parents=True)
        log_file.write_text(self._large_content())
        _rotate_if_needed(log_file)
        assert (tmp_path / "state" / "archive").is_dir()

    def test_subsequent_append_after_rotation_succeeds(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        log_file.write_text(self._large_content())
        _rotate_if_needed(log_file)
        append_decision_record({"action": "dispatch"}, log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "dispatch"


# ---------------------------------------------------------------------------
# main() integration (dry-run + happy path)
# ---------------------------------------------------------------------------

class TestMain:
    def _make_popen_mock(self, stdout: str, returncode: int = 0):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout, "")
        mock_proc.returncode = returncode
        return mock_proc

    def test_dry_run_prints_and_does_not_write(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, SAMPLE_EVENTS)
        log_file = tmp_path / "decisions.jsonl"
        stdout_response = make_haiku_response(HAIKU_DECISION_JSON)
        mock_proc = self._make_popen_mock(stdout_response)

        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc):
            rc = main([
                "--events-file", str(events_file),
                "--decision-log", str(log_file),
                "--dry-run",
            ])

        assert rc == 0
        assert not log_file.exists()
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["action"] == "approve"

    def test_appends_to_log_on_success(self, tmp_path):
        events_file = make_events_file(tmp_path, SAMPLE_EVENTS)
        log_file = tmp_path / "decisions.jsonl"
        stdout_response = make_haiku_response(HAIKU_DECISION_JSON)
        mock_proc = self._make_popen_mock(stdout_response)

        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc):
            rc = main([
                "--events-file", str(events_file),
                "--decision-log", str(log_file),
            ])

        assert rc == 0
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "approve"

    def test_returns_1_if_events_file_not_found(self, tmp_path):
        rc = main([
            "--events-file", str(tmp_path / "nonexistent.ndjson"),
        ])
        assert rc == 1

    def test_returns_0_on_empty_events_file(self, tmp_path):
        events_file = tmp_path / "empty.ndjson"
        events_file.write_text("")
        rc = main(["--events-file", str(events_file)])
        assert rc == 0

    def test_returns_0_when_no_text_events(self, tmp_path):
        events_file = make_events_file(tmp_path, EVENTS_NO_TEXT)
        rc = main(["--events-file", str(events_file)])
        assert rc == 0

    def test_fallback_record_appended_on_haiku_failure(self, tmp_path):
        events_file = make_events_file(tmp_path, SAMPLE_EVENTS)
        log_file = tmp_path / "decisions.jsonl"
        mock_proc = self._make_popen_mock("", returncode=1)

        with patch("t0_decision_summarizer.subprocess.Popen", return_value=mock_proc):
            rc = main([
                "--events-file", str(events_file),
                "--decision-log", str(log_file),
            ])

        assert rc == 0
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["action"] == "wait"
        assert "Haiku summarization failed" in record["reasoning"]
