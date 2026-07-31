"""Tests for kimi token capture — post-run wire.jsonl harvest (dispatch 20260731).

Measured on kimi-cli 1.46.0: `--output-format stream-json` never carries token
accounting, but the session's `wire.jsonl` (via `kimi export <session_id>`)
records every `StatusUpdate.token_usage` as clean JSON. These tests cover:

  * `_extract_session_id`       — the resume-line regex on stderr.
  * `_parse_wire_token_usage`   — aggregating StatusUpdate.token_usage.
  * `_harvest_session_token_usage` — export + zip read, fail-open.
  * `spawn_kimi` wiring         — a clean run fills token_usage / measured=True;
    harvest failure or a failed dispatch leaves tokens honestly unavailable.

All new functions exist only on this branch — on origin/main the module import
fails, so this file is red there and green here (the red-green contract).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make sure scripts/lib is on the path
_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from provider_spawns import kimi_spawn  # noqa: E402
from provider_spawns.kimi_spawn import (  # noqa: E402
    _extract_session_id,
    _harvest_session_token_usage,
    _parse_wire_token_usage,
    spawn_kimi,
)

# Verbatim StatusUpdate message shape from a real kimi-cli 1.46.0 export.
_STREAM_JSON_SESSION_ID = "c2f1ae72-7d30-46e0-8b41-ca8938a13b89"


def _wire_line(msg_type: str, payload: dict) -> str:
    return json.dumps({
        "timestamp": 1785515300.0,
        "message": {"type": msg_type, "payload": payload},
    })


def _status_update(input_other: int, output: int, cache_read: int, cache_creation: int = 0) -> str:
    return _wire_line("StatusUpdate", {
        "context_usage": 0.067,
        "context_tokens": 17665,
        "max_context_tokens": 262144,
        "token_usage": {
            "input_other": input_other,
            "output": output,
            "input_cache_read": cache_read,
            "input_cache_creation": cache_creation,
        },
        "message_id": "chatcmpl-test",
        "plan_mode": False,
        "mcp_status": None,
    })


class TestExtractSessionId(unittest.TestCase):
    def test_extracts_id_from_resume_line(self) -> None:
        stderr = "\nTo resume this session: kimi -r %s\n" % _STREAM_JSON_SESSION_ID
        self.assertEqual(_extract_session_id(stderr), _STREAM_JSON_SESSION_ID)

    def test_accepts_long_resume_alias(self) -> None:
        stderr = "To resume this session: kimi --resume %s" % _STREAM_JSON_SESSION_ID
        self.assertEqual(_extract_session_id(stderr), _STREAM_JSON_SESSION_ID)

    def test_returns_none_without_resume_line(self) -> None:
        self.assertIsNone(_extract_session_id("kimi exited with code 1"))

    def test_returns_none_on_none_and_empty(self) -> None:
        self.assertIsNone(_extract_session_id(None))
        self.assertIsNone(_extract_session_id(""))
        self.assertIsNone(_extract_session_id("   \n  "))

    def test_returns_none_on_malformed_id(self) -> None:
        self.assertIsNone(_extract_session_id("kimi -r not-a-valid-id-!!"))


class TestParseWireTokenUsage(unittest.TestCase):
    def test_multi_step_run_sums_input_output_takes_last_cache(self) -> None:
        wire = "\n".join([
            _status_update(input_other=8449, output=38, cache_read=9216),
            _status_update(input_other=71, output=33, cache_read=17664),
        ])
        usage = _parse_wire_token_usage(wire)
        self.assertIsNotNone(usage)
        # input_other and output are per-call NEW tokens -> summed for the run.
        self.assertEqual(usage["input_tokens"], 8449 + 71)
        self.assertEqual(usage["output_tokens"], 38 + 33)
        # cache_read is the cumulative context-cache read -> last wins.
        self.assertEqual(usage["cache_read_tokens"], 17664)
        self.assertEqual(usage["cache_creation_tokens"], 0)

    def test_single_step_run(self) -> None:
        wire = _status_update(input_other=8438, output=31, cache_read=9216)
        usage = _parse_wire_token_usage(wire)
        self.assertEqual(usage["input_tokens"], 8438)
        self.assertEqual(usage["output_tokens"], 31)
        self.assertEqual(usage["cache_read_tokens"], 9216)

    def test_returns_none_when_no_status_update(self) -> None:
        wire = "\n".join([
            _wire_line("TurnBegin", {"user_input": "hi"}),
            _wire_line("TextPart", {"type": "text", "text": "okay"}),
            _wire_line("TurnEnd", {}),
        ])
        self.assertIsNone(_parse_wire_token_usage(wire))

    def test_skips_malformed_and_non_message_lines(self) -> None:
        wire = "\n".join([
            "not json at all",
            '{"type": "metadata", "protocol_version": "1.10"}',
            _wire_line("StepBegin", {"n": 1}),
            _status_update(input_other=10, output=5, cache_read=3),
        ])
        usage = _parse_wire_token_usage(wire)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)

    def test_returns_none_on_empty_input(self) -> None:
        self.assertIsNone(_parse_wire_token_usage(""))
        self.assertIsNone(_parse_wire_token_usage(None))

    def test_handles_missing_token_usage_key(self) -> None:
        wire = _wire_line("StatusUpdate", {"context_tokens": 10})
        self.assertIsNone(_parse_wire_token_usage(wire))

    def test_zero_token_usage_is_still_a_measured_event(self) -> None:
        # A StatusUpdate with an explicit all-zero token_usage is a real event;
        # the caller decides availability via the non-None return, not the zeros.
        wire = _status_update(input_other=0, output=0, cache_read=0)
        usage = _parse_wire_token_usage(wire)
        self.assertIsNotNone(usage)
        self.assertEqual(usage["input_tokens"], 0)


class TestHarvestSessionTokenUsage(unittest.TestCase):
    """`kimi export` + zip read, with the subprocess mocked (hermetic tests)."""

    def _fake_export_writer(self, wire_content: str):
        def _fake_export(cmd, **kwargs):
            out_idx = cmd.index("-o") + 1
            out_path = cmd[out_idx]
            with zipfile.ZipFile(out_path, "w") as zf:
                zf.writestr("wire.jsonl", wire_content)
            return MagicMock(returncode=0)
        return _fake_export

    def test_happy_path_returns_aggregated_usage(self) -> None:
        wire = "\n".join([
            _status_update(input_other=8449, output=38, cache_read=9216),
            _status_update(input_other=71, output=33, cache_read=17664),
        ])
        with patch.object(kimi_spawn.subprocess, "run",
                          side_effect=self._fake_export_writer(wire)):
            usage = _harvest_session_token_usage(_STREAM_JSON_SESSION_ID, env={})
        self.assertEqual(usage["input_tokens"], 8520)
        self.assertEqual(usage["output_tokens"], 71)
        self.assertEqual(usage["cache_read_tokens"], 17664)

    def test_export_failure_returns_none(self) -> None:
        with patch.object(kimi_spawn.subprocess, "run",
                          return_value=MagicMock(returncode=1)):
            usage = _harvest_session_token_usage(_STREAM_JSON_SESSION_ID, env={})
        self.assertIsNone(usage)

    def test_missing_wire_jsonl_returns_none(self) -> None:
        def _fake_export(cmd, **kwargs):
            out_idx = cmd.index("-o") + 1
            with zipfile.ZipFile(cmd[out_idx], "w") as zf:
                zf.writestr("manifest.json", "{}")
            return MagicMock(returncode=0)

        with patch.object(kimi_spawn.subprocess, "run", side_effect=_fake_export):
            usage = _harvest_session_token_usage(_STREAM_JSON_SESSION_ID, env={})
        self.assertIsNone(usage)

    def test_no_status_update_returns_none(self) -> None:
        wire = _wire_line("TurnEnd", {})
        with patch.object(kimi_spawn.subprocess, "run",
                          side_effect=self._fake_export_writer(wire)):
            usage = _harvest_session_token_usage(_STREAM_JSON_SESSION_ID, env={})
        self.assertIsNone(usage)

    def test_cleanup_removes_temp_dir(self) -> None:
        wire = _status_update(input_other=1, output=1, cache_read=0)
        with patch.object(kimi_spawn.subprocess, "run",
                          side_effect=self._fake_export_writer(wire)):
            _harvest_session_token_usage(_STREAM_JSON_SESSION_ID, env={})
        # The export dir lives under the system temp root; assert the mkdtemp
        # prefix produces no leftover dirs after the harvest.
        tmp_root = tempfile.gettempdir()
        leftovers = [
            p for p in Path(tmp_root).iterdir()
            if p.is_dir() and p.name.startswith("kimi-wire-")
        ]
        self.assertEqual(leftovers, [], "temp export dirs must be cleaned up")

    def test_builds_expected_export_argv(self) -> None:
        captured: dict = {}

        def _fake_export(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            out_idx = cmd.index("-o") + 1
            with zipfile.ZipFile(cmd[out_idx], "w") as zf:
                zf.writestr("wire.jsonl", _status_update(input_other=1, output=1, cache_read=0))
            return MagicMock(returncode=0)

        with patch.object(kimi_spawn.subprocess, "run", side_effect=_fake_export):
            _harvest_session_token_usage(_STREAM_JSON_SESSION_ID, env={"PATH": "/x"})
        self.assertEqual(captured["cmd"][:2], ["kimi", "export"])
        self.assertIn(_STREAM_JSON_SESSION_ID, captured["cmd"])
        self.assertIn("--yes", captured["cmd"])
        # The env must be passed through to the export subprocess.
        self.assertEqual(captured["env"], {"PATH": "/x"})


def _make_proc_with_stderr(events: list, returncode: int = 0, stderr_text: str = "") -> MagicMock:
    """Fake Popen with real-pipe stdout (drain_stream needs fileno()) and a
    BytesIO stderr carrying the kimi resume line."""
    data = b"".join((json.dumps(e) + "\n").encode() for e in events)
    read_fd, write_fd = os.pipe()

    def _writer():
        try:
            os.write(write_fd, data)
        finally:
            os.close(write_fd)

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    fake_proc = MagicMock()
    fake_proc.returncode = returncode
    fake_proc.poll.return_value = returncode
    fake_proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
    fake_proc.stderr = io.BytesIO(stderr_text.encode())
    fake_proc.wait = MagicMock(return_value=returncode)
    fake_proc.kill = MagicMock()
    fake_proc._writer_thread = writer_thread
    return fake_proc


class TestSpawnKimiTokenHarvestWiring(unittest.TestCase):
    """End-to-end through spawn_kimi: a clean run harvests tokens; harvest
    failure or a failed dispatch leaves tokens honestly unavailable."""

    _ANSWER_EVENTS = [
        {"role": "assistant", "content": [{"type": "text", "text": "okay"}]},
    ]

    def _run(self, events, returncode=0, stderr_text="", harvest_return=None):
        fake_proc = _make_proc_with_stderr(events, returncode=returncode, stderr_text=stderr_text)
        try:
            with patch("provider_spawns.kimi_spawn._start_kimi_subprocess") as mock_start, \
                    patch("provider_spawns.kimi_spawn._harvest_session_token_usage",
                          return_value=harvest_return) as mock_harvest:
                mock_start.return_value = (fake_proc, None)
                result = spawn_kimi("prompt", dispatch_id="d-harvest", terminal_id="T1")
        finally:
            fake_proc._writer_thread.join(timeout=5)
        return result, mock_harvest

    def test_clean_run_with_resume_line_harvests_tokens(self) -> None:
        stderr = "\nTo resume this session: kimi -r %s\n" % _STREAM_JSON_SESSION_ID
        harvested = {"input_tokens": 8520, "output_tokens": 71, "cache_read_tokens": 17664}
        result, mock_harvest = self._run(
            self._ANSWER_EVENTS, returncode=0, stderr_text=stderr, harvest_return=harvested,
        )
        self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.token_usage, harvested)
        self.assertTrue(result.token_usage_measured)
        # The harvest is keyed by the session id extracted from stderr.
        mock_harvest.assert_called_once()
        args, kwargs = mock_harvest.call_args
        self.assertEqual(args[0], _STREAM_JSON_SESSION_ID)
        # frontmatter_fields() must surface the measured numbers, not zeros.
        fm = result.frontmatter_fields()
        self.assertEqual(fm["token_usage"]["input"], 8520)
        self.assertEqual(fm["token_usage"]["output"], 71)
        self.assertEqual(fm["token_usage"]["cache_read"], 17664)

    def test_harvest_none_leaves_tokens_unavailable(self) -> None:
        stderr = "\nTo resume this session: kimi -r %s\n" % _STREAM_JSON_SESSION_ID
        result, mock_harvest = self._run(
            self._ANSWER_EVENTS, returncode=0, stderr_text=stderr, harvest_return=None,
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.token_usage)
        self.assertFalse(result.token_usage_measured)
        fm = result.frontmatter_fields()
        self.assertEqual(fm["token_usage"], {"input": 0, "output": 0, "cache_read": 0})

    def test_no_resume_line_skips_harvest(self) -> None:
        result, mock_harvest = self._run(
            self._ANSWER_EVENTS, returncode=0, stderr_text="kimi exited normally",
            harvest_return=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.token_usage)
        mock_harvest.assert_not_called()

    def test_failed_dispatch_skips_harvest(self) -> None:
        # An error event forces rc=1 even though the process exits 0.
        events = [{"event_type": "error", "message": "rate limit exceeded"}]
        result, mock_harvest = self._run(events, returncode=0, stderr_text="",
                                         harvest_return=None)
        self.assertIsNotNone(result.error)
        self.assertNotEqual(result.returncode, 0)
        mock_harvest.assert_not_called()

    def test_nonzero_exit_skips_harvest(self) -> None:
        result, mock_harvest = self._run(
            self._ANSWER_EVENTS, returncode=1, stderr_text="", harvest_return=None,
        )
        self.assertNotEqual(result.returncode, 0)
        mock_harvest.assert_not_called()

    def test_stderr_read_failure_still_succeeds(self) -> None:
        """A broken stderr read must not break the dispatch — fail-open."""
        fake_proc = _make_proc_with_stderr(self._ANSWER_EVENTS, returncode=0,
                                           stderr_text="")
        fake_proc.stderr.read = MagicMock(side_effect=OSError("boom"))
        try:
            with patch("provider_spawns.kimi_spawn._start_kimi_subprocess") as mock_start, \
                    patch("provider_spawns.kimi_spawn._harvest_session_token_usage",
                          return_value=None) as mock_harvest:
                mock_start.return_value = (fake_proc, None)
                result = spawn_kimi("prompt", dispatch_id="d-harvest", terminal_id="T1")
        finally:
            fake_proc._writer_thread.join(timeout=5)
        self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.token_usage)
        mock_harvest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
