"""test_toolcall_signals.py — receipt-quality PR-B2 tool-call signal tests.

Covers the record+aggregate round-trip (scripts/lib/toolcall_signals.py): the
per-dispatch NDJSON log a PreToolUse hook writes to, and the aggregation
governance_emit/_emit_governance folds into ReceiptV2.tool_call_count /
tool_call_failures / tool_call_retries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from toolcall_signals import aggregate_toolcall_signals, record_toolcall_event  # noqa: E402


def test_aggregate_missing_signal_dir_returns_none(tmp_path):
    assert aggregate_toolcall_signals(tmp_path / "does-not-exist") is None


def test_aggregate_empty_signal_file_returns_none(tmp_path):
    (tmp_path / "toolcalls.ndjson").write_text("", encoding="utf-8")
    assert aggregate_toolcall_signals(tmp_path) is None


def test_record_then_aggregate_counts_total_calls(tmp_path):
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "pwd"}}, blocked=False)
    record_toolcall_event(tmp_path, {"tool_name": "Task", "tool_input": {"prompt": "x"}}, blocked=True)

    signals = aggregate_toolcall_signals(tmp_path)
    assert signals == {"tool_call_count": 3, "tool_call_failures": 1, "tool_call_retries": 0}


def test_blocked_call_counted_as_failure(tmp_path):
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "claude -p x"}}, blocked=True)
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)

    signals = aggregate_toolcall_signals(tmp_path)
    assert signals["tool_call_count"] == 2
    assert signals["tool_call_failures"] == 1


def test_repeated_identical_call_counts_as_retry(tmp_path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "pytest tests/foo.py"}}
    record_toolcall_event(tmp_path, payload, blocked=False)
    record_toolcall_event(tmp_path, payload, blocked=False)
    record_toolcall_event(tmp_path, payload, blocked=False)

    signals = aggregate_toolcall_signals(tmp_path)
    assert signals["tool_call_count"] == 3
    # 3 identical (tool_name, signature) occurrences -> 2 retries (first is the original call).
    assert signals["tool_call_retries"] == 2


def test_same_tool_different_input_is_not_a_retry(tmp_path):
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls a"}}, blocked=False)
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls b"}}, blocked=False)

    signals = aggregate_toolcall_signals(tmp_path)
    assert signals["tool_call_count"] == 2
    assert signals["tool_call_retries"] == 0


def test_key_order_independent_input_is_a_retry_match(tmp_path):
    """Equivalent dict key ordering must not spuriously break a retry match."""
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"a": 1, "b": 2}}, blocked=False)
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"b": 2, "a": 1}}, blocked=False)

    signals = aggregate_toolcall_signals(tmp_path)
    assert signals["tool_call_count"] == 2
    assert signals["tool_call_retries"] == 1


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "toolcalls.ndjson"
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {}}, blocked=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not valid json\n")
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {}}, blocked=False)

    signals = aggregate_toolcall_signals(tmp_path)
    # Two well-formed lines with identical input -> 1 retry; the garbage line
    # in between is silently skipped rather than aborting aggregation.
    assert signals["tool_call_count"] == 2
    assert signals["tool_call_retries"] == 1


def test_record_creates_signal_dir_when_absent(tmp_path):
    signal_dir = tmp_path / "nested" / "scratch"
    record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {}}, blocked=False)
    assert (signal_dir / "toolcalls.ndjson").is_file()


def test_record_never_raises_on_unwritable_dir(tmp_path):
    """A file where a directory is expected must not raise -- record is
    called from a hook context where a failure must never break the tool call."""
    blocked_path = tmp_path / "not-a-dir"
    blocked_path.write_text("x", encoding="utf-8")
    record_toolcall_event(blocked_path, {"tool_name": "Bash", "tool_input": {}}, blocked=False)  # must not raise


def test_recorded_entry_shape(tmp_path):
    record_toolcall_event(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=True)
    line = (tmp_path / "toolcalls.ndjson").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["tool_name"] == "Bash"
    assert entry["blocked"] is True
    assert "signature" in entry
    assert "timestamp" in entry
