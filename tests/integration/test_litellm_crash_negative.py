"""Negative integration tests for LiteLLMAdapter — crash, unreachable endpoint.

These tests use a mock runner script to simulate failure conditions without
requiring real provider credentials. They verify:
  - Unreachable/bad-endpoint -> structured error event + status=failed
  - Subprocess exits non-zero before complete -> synthetic error event
  - Credentials missing error format propagated correctly
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from adapters.litellm_adapter import LiteLLMAdapter
from canonical_event import CanonicalEvent

# ---------------------------------------------------------------------------
# Helpers — write tiny fake runner scripts into tmp_path
# ---------------------------------------------------------------------------

def _write_runner(tmp_path: Path, script: str) -> str:
    """Write a fake runner py file and return its path as str."""
    runner = tmp_path / "fake_runner.py"
    runner.write_text(textwrap.dedent(script), encoding="utf-8")
    return str(runner)


def _make_adapter(tmp_path: Path, script: str, model: str = "litellm/fake/model") -> LiteLLMAdapter:
    runner_path = _write_runner(tmp_path, script)
    a = LiteLLMAdapter("T2", litellm_model=model)
    a._test_runner_path = runner_path
    return a, runner_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLiteLLMCrashNegative:
    def test_credentials_missing_produces_error_event(self, tmp_path):
        script = """\
            import json, sys
            sys.stdout.write(json.dumps({
                "error_type": "credentials_missing",
                "message": "NoCredentialsError: Unable to locate credentials"
            }) + "\\n")
            sys.stdout.flush()
            sys.exit(1)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-creds-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        assert result.status == "failed"
        assert result.event_count > 0
        error_events = [e for e in result.events if e["event_type"] == "error"]
        assert error_events, "Expected at least one error event"
        assert error_events[0]["data"]["error_type"] == "credentials_missing"

    def test_service_unavailable_produces_error_event(self, tmp_path):
        script = """\
            import json, sys
            sys.stdout.write(json.dumps({
                "error_type": "service_unavailable",
                "message": "Connection refused to endpoint"
            }) + "\\n")
            sys.stdout.flush()
            sys.exit(2)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-svc-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        assert result.status == "failed"
        error_events = [e for e in result.events if e["event_type"] == "error"]
        assert error_events

    def test_subprocess_crash_without_output_produces_synthetic_error(self, tmp_path):
        # Runner exits non-zero immediately with no output at all
        script = """\
            import sys
            sys.exit(1)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-noout-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        assert result.status == "failed"
        # StreamingDrainerMixin emits synthetic error for non-zero exit without complete
        error_events = [e for e in result.events if e["event_type"] == "error"]
        assert error_events, "Expected synthetic error event from drainer"

    def test_malformed_json_line_becomes_error_event(self, tmp_path):
        script = """\
            import sys
            sys.stdout.write("this is not json at all\\n")
            sys.stdout.flush()
            sys.exit(0)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-malform-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        # Malformed line -> error event from _parse_line in drainer
        # Status depends on whether error event was emitted
        error_events = [e for e in result.events if e["event_type"] == "error"]
        assert error_events, "Expected error event for malformed JSON"

    def test_successful_stream_no_error_events(self, tmp_path):
        # Runner emits a valid mini stream: init + text + complete
        init_chunk = {
            "choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            "model": "fake",
        }
        text_chunk = {
            "choices": [{"delta": {"content": "hello"}, "finish_reason": None}],
            "model": "fake",
        }
        done_chunk = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "model": "fake",
        }
        lines = "\n".join(json.dumps(c) for c in [init_chunk, text_chunk, done_chunk])
        script = f"""\
            import sys
            sys.stdout.write({lines!r} + "\\n")
            sys.stdout.flush()
            sys.exit(0)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-success-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        assert result.status == "done"
        assert result.output == "hello"
        types = [e["event_type"] for e in result.events]
        assert "text" in types
        assert "complete" in types
        error_events = [e for e in result.events if e["event_type"] == "error"]
        assert not error_events, f"Unexpected error events: {error_events}"

    def test_runner_not_installed_produces_failed_result(self, tmp_path):
        # Runner immediately fails with import error
        script = """\
            import json, sys
            sys.stdout.write(json.dumps({
                "error_type": "runner_error",
                "message": "litellm not installed: No module named 'litellm'"
            }) + "\\n")
            sys.stdout.flush()
            sys.exit(2)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-nopkg-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        assert result.status == "failed"

    def test_all_error_events_are_canonical(self, tmp_path):
        script = """\
            import json, sys
            sys.stdout.write(json.dumps({
                "error_type": "credentials_missing",
                "message": "test"
            }) + "\\n")
            sys.stdout.flush()
            sys.exit(1)
        """
        a, runner = _make_adapter(tmp_path, script)
        result = a.execute(
            "Hello",
            {
                "dispatch_id": "crash-canon-001",
                "terminal_id": "T2",
                "_runner_path": runner,
            },
        )
        for event_dict in result.events:
            event = CanonicalEvent.from_dict(event_dict)
            assert event.provider == "litellm"
            assert event.terminal_id == "T2"
